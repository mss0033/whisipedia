"""``wsp collections``: dry runs and polite fetches of curated document collections (D11, D13, D14).

``dry-run`` reads what each issuer lists (metadata only: index pages, APIs, listings), applies the
document-level policy and prints, per source, what would be fetched, the filter decisions and the
sizes; with ``--max-pages`` it samples large listings and extrapolates from the upstream totals.

``fetch`` downloads the allowed documents of one source into ``<vault>/_incoming/<source>/<cycle>/``
at most one request per host per second (more where ``robots.txt`` asks), screens every file's
front matter (distribution gate, content screen), deletes restricted files at once, quarantines
held ones under ``_incoming/_quarantine/<source>/`` with one review proposal per batch, and writes
the catalog ``meta/documents.jsonl`` and the ingest plan ``_incoming/_plans/targets.d/<source>-<cycle>.json``.
``wsp ingest --source <id>`` then seals the generation. Git repositories are fetched as a single
``git bundle`` of a mirror clone.

Partitioned sources (``access.partitioned``, ADR-0010) are fetched one ``--partition`` per run into
the cycle ``<YYYY-MM-DD>.<partition>``; the catalog carried over is that partition's current one
(``CURRENT.d/<partition>.txt``), never another partition's, so ``withdrawn-from-index`` labels and
skipped duplicates stay within the partition.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast

import yaml

from wsp_adapters.doccollection.adapter import (
    CATALOG,
    DocCollectionAdapter,
    Survey,
    screen_file,
    well_formed,
)
from wsp_adapters.doccollection.catalog import FetchedDoc, build_catalog, read_catalog_file, write_catalog
from wsp_adapters.doccollection.discovery import Throttle, origin_ok
from wsp_adapters.doccollection.model import Candidate
from wsp_adapters.doccollection.policy import Rules, Verdict
from wsp_cli import bootstrap
from wsp_core.errors import FetchError
from wsp_core.model import RemoteFile, SourceSpec, SourceStatus
from wsp_core.partitions import PartitionError, check_partition, partition_generation_id, partition_of
from wsp_core.ports import Ctx, Fetcher, SupportsHead

# Research doc §6, in order. Git-hosted and file-set sources are surveyed through the same path.
TOP_PICKS: tuple[str, ...] = (
    "nist-csrc",
    "nist-oscal-content",
    "frus",
    "nga-nautical-pubs",
    "jts-clinical-practice-guidelines",
    "cia-csi",
    "fema-independent-study",
    "uk-resilience-guidance",
    "noaa-nautical-charts",
    "faa-handbooks",
    "usda-home-canning",
    "usda-farmers-bulletins",
    "army-pubs-public",
    "cisa-protective-security",
    "who-iris",
    "ncvec-question-pools",
    "cisa-safecom-field-guides",
    "doe-fundamentals-handbooks",
    "neets",
)
COLLECTION_ADAPTERS = ("doc-collection", "git-mirror")
PLANS_SUBDIR = "targets.d"


def collection_spec(spec: SourceSpec) -> SourceSpec | None:
    """The source as a document collection: itself, or a git-mirror source as one git ``bundle``."""
    if spec.adapter == "doc-collection":
        return spec
    if spec.adapter == "git-mirror":
        acc = cast(Mapping[str, Any], spec.raw.get("access") or {})
        repos = [str(u) for u in cast(list[object], acc.get("endpoints") or []) if "github.com" in str(u)]
        if not repos:
            return None
        raw = dict(spec.raw)
        raw["access"] = {
            **acc,
            "issuer": str(spec.raw.get("publisher") or spec.name),
            "origins": ["github.com", "api.github.com"],
            "discovery": [{"kind": "git", "repos": repos}],
            "filter": {"allow_all": True},
        }
        return dataclasses.replace(spec, raw=raw)
    return None


def api_keys(cfg: Mapping[str, Any]) -> dict[str, str]:
    """``[curator.api_keys]`` in local.toml: ``govinfo_key``, ``congress_key`` (api.data.gov keys)."""
    raw = cast(Mapping[str, Any], bootstrap.section(cfg, "curator").get("api_keys") or {})
    out: dict[str, str] = {}
    for name, value in raw.items():
        if isinstance(value, str) and value:
            out[name.removesuffix("_key")] = value
    for env_name, value in os.environ.items():  # WSP_API_KEY_GOVINFO=... for one-off runs
        if env_name.startswith("WSP_API_KEY_") and value:
            out[env_name.removeprefix("WSP_API_KEY_").lower()] = value
    return out


# --- dry run -----------------------------------------------------------------------------------------


@dataclass
class SourceDryRun:
    source_id: str
    adapter: str
    status: str
    discovered: int = 0
    upstream_total: int | None = None
    allowed: int = 0
    denied: int = 0
    review: int = 0
    documents: int = 0  # allowed, resolved to document URLs
    unresolved: int = 0
    known_bytes: int = 0
    sized: int = 0
    estimated_bytes: int = 0
    extrapolated: bool = False
    requests: dict[str, int] = field(default_factory=dict[str, int])
    top_rules: dict[str, int] = field(default_factory=dict[str, int])
    examples: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    notes: list[str] = field(default_factory=list[str])
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def summarize(spec: SourceSpec, survey: Survey, *, head_sizes: Sequence[int] = ()) -> SourceDryRun:
    out = SourceDryRun(spec.id, spec.adapter or "", spec.status.value)
    out.discovered = len(survey.decisions)
    out.allowed = survey.count(Verdict.ALLOW)
    out.denied = survey.count(Verdict.DENY)
    out.review = survey.count(Verdict.REVIEW)
    out.documents = len(survey.allowed)
    out.unresolved = survey.unresolved
    out.requests = survey.requests
    out.notes = survey.notes[:8]
    if survey.totals:
        out.upstream_total = sum(survey.totals.values())
    rules: dict[str, int] = {}
    examples: dict[str, list[str]] = {"deny": [], "review": []}
    for cand, decision in survey.decisions:
        rules[str(decision)] = rules.get(str(decision), 0) + 1
        bucket = examples.get(decision.verdict.value)
        if bucket is not None and len(bucket) < 4:
            bucket.append(f"{cand.pub_number or cand.key} | {cand.title[:70]} [{decision.rule}]")
    out.top_rules = dict(sorted(rules.items(), key=lambda kv: -kv[1])[:8])
    out.examples = {k: v for k, v in examples.items() if v}
    resolved = {c.key for c in survey.allowed}
    # Allowed landing pages not resolved in this sample still carry a listing size (archive.org
    # item_size includes derivatives, so this errs high).
    sizes = [c.size for c in survey.allowed if c.size] + [
        c.size
        for c, d in survey.decisions
        if d.verdict is Verdict.ALLOW and c.resolve and c.key not in resolved and c.size
    ]
    out.known_bytes = sum(sizes)
    out.sized = len(sizes)
    per_doc = statistics.median([*sizes, *head_sizes]) if (sizes or head_sizes) else 0
    allowed_docs = max(out.documents + out.unresolved, out.allowed)
    estimate = out.known_bytes + int(per_doc * max(0, allowed_docs - out.sized))
    if out.upstream_total and out.discovered and out.upstream_total > out.discovered:
        factor = out.upstream_total / out.discovered
        estimate = int(estimate * factor)
        out.extrapolated = True
    elif survey.scale > 1.0:  # an index crawl stopped early: scale by the share of pages seen
        estimate = int(estimate * survey.scale)
        out.upstream_total = int(out.discovered * survey.scale)
        out.extrapolated = True
    out.estimated_bytes = estimate
    return out


def head_sample(fetcher: Fetcher, docs: Sequence[Candidate], n: int) -> list[int]:
    """Sizes of up to ``n`` documents without a known size (HEAD; metadata only)."""
    if n <= 0 or not isinstance(fetcher, SupportsHead):
        return []
    sizes: list[int] = []
    for cand in [c for c in docs if not c.size and c.resolve != "git"][:n]:
        try:
            head = fetcher.head(cand.url)
        except FetchError:
            continue
        if head.size:
            sizes.append(head.size)
    return sizes


def dry_run(
    specs: Sequence[SourceSpec],
    fetcher: Fetcher,
    *,
    keys: Mapping[str, str],
    workdir: Path,
    user_agent: str,
    max_pages: int | None,
    resolve_sample: int,
    head_samples: int,
    allow_demo_key: bool = False,
    out: TextIO | None = None,
) -> list[SourceDryRun]:
    adapter = DocCollectionAdapter(keys=keys, allow_demo_key=allow_demo_key)
    results: list[SourceDryRun] = []
    for spec in specs:
        coll = collection_spec(spec)
        if coll is None:
            results.append(
                SourceDryRun(spec.id, spec.adapter or "", spec.status.value, error="not a collection")
            )
            continue
        if out is not None:
            out.write(f"surveying {spec.id} ...\n")
            out.flush()
        ctx = Ctx(source=coll, fetcher=fetcher, workdir=workdir / spec.id, user_agent=user_agent)
        try:
            env = adapter.env(ctx, max_pages=max_pages)
            survey = adapter.survey(ctx, max_pages=max_pages, resolve_limit=resolve_sample, env=env)
            sizes = head_sample(env.fetcher, survey.allowed, head_samples)
            if isinstance(env.fetcher, Throttle):
                survey.requests = dict(env.fetcher.requests)
            results.append(summarize(spec, survey, head_sizes=sizes))
        except Exception as exc:
            results.append(
                SourceDryRun(
                    spec.id, spec.adapter or "", spec.status.value, error=f"{type(exc).__name__}: {exc}"
                )
            )
    return results


def gb(n: int) -> str:
    return f"{n / 1e9:,.2f}"


def render_dry_run(results: Sequence[SourceDryRun], out: TextIO) -> None:
    out.write(
        f"{'source':<34} {'listed':>8} {'upstream':>9} {'allow':>6} {'deny':>5} {'review':>6} "
        f"{'docs':>6} {'known GB':>9} {'est. GB':>9} {'req':>5}\n"
    )
    totals = [0, 0, 0, 0, 0, 0, 0]
    for r in results:
        if r.error:
            out.write(f"{r.source_id:<34} ERROR {r.error}\n")
            continue
        est = gb(r.estimated_bytes) + ("*" if r.extrapolated else " ")
        upstream = "-" if r.upstream_total is None else f"{r.upstream_total:,}"
        out.write(
            f"{r.source_id:<34} {r.discovered:>8,} {upstream:>9} {r.allowed:>6,} {r.denied:>5,} "
            f"{r.review:>6,} {r.documents:>6,} {gb(r.known_bytes):>9} {est:>9} "
            f"{sum(r.requests.values()):>5}\n"
        )
        for i, v in enumerate(
            (r.discovered, r.allowed, r.denied, r.review, r.documents, r.known_bytes, r.estimated_bytes)
        ):
            totals[i] += v
    out.write(
        f"{'TOTAL':<34} {totals[0]:>8,} {'':>9} {totals[1]:>6,} {totals[2]:>5,} {totals[3]:>6,} "
        f"{totals[4]:>6,} {gb(totals[5]):>9} {gb(totals[6]):>9}\n"
    )
    out.write("* extrapolated from a sample of the listing to the upstream total\n")
    for r in results:
        if r.error:
            continue
        details = [f"  {k}: {n}" for k, n in r.top_rules.items()]
        for verdict, items in r.examples.items():
            details += [f"  e.g. {verdict}: {e}" for e in items]
        details += [f"  note: {n}" for n in r.notes]
        if details:
            out.write(f"\n{r.source_id}\n" + "\n".join(details) + "\n")


def _selected(args: argparse.Namespace, specs: Sequence[SourceSpec]) -> list[SourceSpec]:
    by_id = {s.id: s for s in specs}
    wanted: list[str] = list(args.source or [])
    if args.top:
        wanted += [s for s in TOP_PICKS if s not in wanted]
    if not wanted:
        wanted = [s.id for s in specs if s.adapter in COLLECTION_ADAPTERS]
    missing = [w for w in wanted if w not in by_id]
    if missing:
        raise bootstrap.ConfigError(f"unknown source(s): {', '.join(missing)}")
    return [by_id[w] for w in wanted if by_id[w].status is not SourceStatus.EXCLUDED]


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "whisipedia" / "collections"


def cmd_dry_run(args: argparse.Namespace, out: TextIO) -> int:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    specs = bootstrap.load_sources(cfg, lay)
    selected = _selected(args, specs)
    fetcher = bootstrap.build_fetcher(cfg)
    with tempfile.TemporaryDirectory(prefix="wsp-collections-") as tmp:
        results = dry_run(
            selected,
            fetcher,
            keys=api_keys(cfg),
            workdir=Path(tmp),
            user_agent=bootstrap.user_agent(cfg),
            max_pages=args.max_pages or None,
            resolve_sample=args.resolve_sample,
            head_samples=args.head_sample,
            allow_demo_key=args.demo_key,
            out=None if args.json else sys.stderr,
        )
    report = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "max_pages": args.max_pages,
        "sources": [r.to_json() for r in results],
    }
    path = Path(args.out) if args.out else state_dir() / f"dry-run-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    if args.json:
        out.write(json.dumps(report, indent=1) + "\n")
    else:
        render_dry_run(results, out)
        out.write(f"\nreport: {path}\n")
    return 0


# --- fetch -------------------------------------------------------------------------------------------


@dataclass
class FetchReport:
    source_id: str
    version: str
    fetched: list[FetchedDoc] = field(default_factory=list[FetchedDoc])
    rejected: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])  # (key, rule): deleted
    held: list[dict[str, str]] = field(default_factory=list[dict[str, str]])  # quarantined for review
    review: list[dict[str, str]] = field(default_factory=list[dict[str, str]])  # metadata-stage review
    failed: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    skipped: int = 0
    alerts: list[str] = field(default_factory=list[str])


class CollectionFetcher:
    """Downloads one collection's allowed documents into ``_incoming`` and screens them."""

    def __init__(
        self,
        vault_root: Path,
        fetcher: Fetcher,
        *,
        keys: Mapping[str, str],
        user_agent: str,
        workdir: Path,
        out: TextIO | None = None,
        git: str | None = None,
        throttle: bool = True,
    ) -> None:
        self.vault_root = vault_root
        self.incoming = vault_root / "_incoming"
        self.fetcher = fetcher
        self.adapter = DocCollectionAdapter(keys=keys, throttle=throttle)
        self.user_agent = user_agent
        self.workdir = workdir
        self.out = out
        self.git = git or shutil.which("git")

    def _say(self, text: str) -> None:
        if self.out is not None:
            self.out.write(text + "\n")
            self.out.flush()

    def previous_catalog(
        self, spec: SourceSpec, partition: str | None = None
    ) -> tuple[str | None, list[Any]]:
        """The current generation's id and catalog; for a partition, that partition's (ADR-0010)."""
        lib = self.vault_root / "library" / spec.vault_domain / spec.id
        gen = _pointer(lib / "CURRENT.txt")
        if partition is not None:
            own = _pointer(lib / "CURRENT.d" / f"{partition}.txt")
            gen = (
                own
                if own is not None
                else (gen if gen is not None and partition_of(gen) == partition else None)
            )
        if gen is None:
            return None, []
        return gen, read_catalog_file(lib / gen / CATALOG)

    def run(
        self,
        spec: SourceSpec,
        *,
        version: str,
        limit: int | None = None,
        max_pages: int | None = None,
        dry_run: bool = False,
        partition: str | None = None,
    ) -> FetchReport:
        coll = collection_spec(spec)
        if coll is None:
            raise bootstrap.ConfigError(f"{spec.id} is not a document collection")
        report = FetchReport(spec.id, version)
        ctx = Ctx(
            source=coll, fetcher=self.fetcher, workdir=self.workdir / spec.id, user_agent=self.user_agent
        )
        env = self.adapter.env(ctx, max_pages=max_pages, partition=partition)
        survey = self.adapter.survey(ctx, max_pages=max_pages, env=env)
        cfg = self.adapter.config(coll)
        for cand, decision in survey.decisions:
            if decision.verdict is Verdict.REVIEW:
                report.review.append(_review_row(cand, str(decision)))
        prev_gen, prev_catalog = self.previous_catalog(spec, partition if spec.partitioned else None)
        held_sha = {m.doc_key: m.sha256 for m in prev_catalog}
        dest_dir = self.incoming / spec.id / version
        docs = survey.allowed[:limit] if limit else survey.allowed
        self._say(
            f"{spec.id}: {len(survey.decisions)} listed, {len(docs)} to fetch, "
            f"{survey.count(Verdict.DENY)} denied, {survey.count(Verdict.REVIEW)} for review"
        )
        if dry_run:
            return report
        throttle = env.fetcher
        for cand in docs:
            if cand.key in held_sha and not cand.size:
                report.skipped += 1  # same key held by an earlier generation; size unknown, keep it
                continue
            try:
                if cand.resolve == "git":
                    path = self._git_bundle(cand, dest_dir)
                else:
                    path = self._download(throttle, cand, dest_dir, cfg.origins)
            except (FetchError, OSError, ValueError, subprocess.CalledProcessError) as exc:
                report.failed.append((cand.key, f"{type(exc).__name__}: {exc}"))
                continue
            self._screen(cand, path, cfg.rules, report, held_sha)
        self._finish(spec, coll, report, prev_gen, prev_catalog, survey, dest_dir)
        return report

    def _download(self, fetcher: Fetcher, cand: Candidate, dest_dir: Path, origins: Sequence[str]) -> Path:
        if not all(origin_ok(u, origins) for u in (cand.url, *cand.mirrors)):
            raise ValueError(f"{cand.url} is not an allowed HTTPS origin")
        dest = dest_dir / cand.file_name
        result = fetcher.download(
            RemoteFile(url=cand.url, name=cand.file_name, size=cand.size, mirrors=cand.mirrors), dest
        )
        return result.path

    def _git_bundle(self, cand: Candidate, dest_dir: Path) -> Path:
        if self.git is None:
            raise OSError("git is not installed")
        dest_dir.mkdir(parents=True, exist_ok=True)
        mirror = self.workdir / "git" / cand.key.replace(":", "_")
        if mirror.is_dir():
            subprocess.run([self.git, "-C", str(mirror), "remote", "update", "--prune"], check=True)  # noqa: S603
        else:
            mirror.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([self.git, "clone", "--mirror", cand.url, str(mirror)], check=True)  # noqa: S603
        bundle = dest_dir / cand.file_name.rsplit(".", 1)[0]
        bundle = bundle.with_name(bundle.name + ".bundle")
        partial = bundle.with_name(bundle.name + ".partial")
        subprocess.run([self.git, "-C", str(mirror), "bundle", "create", str(partial), "--all"], check=True)  # noqa: S603
        partial.replace(bundle)
        return bundle

    def _screen(
        self,
        cand: Candidate,
        path: Path,
        rules: Rules,
        report: FetchReport,
        held_sha: Mapping[str, str],
    ) -> None:
        import hashlib

        problem = None if path.suffix == ".bundle" else well_formed(path)
        if problem:
            report.failed.append((cand.key, problem))
            path.unlink(missing_ok=True)
            return
        screen = screen_file(path, rules) if path.suffix != ".bundle" else None
        if screen is not None and screen.decision.verdict is Verdict.DENY:
            path.unlink(missing_ok=True)  # restricted or out-of-scope bytes are not kept anywhere
            report.rejected.append((cand.key, str(screen.decision)))
            if screen.decision.alert:
                report.alerts.append(f"{cand.key}: {screen.decision.rule} ({cand.url})")
            return
        if screen is not None and screen.decision.verdict is Verdict.REVIEW:
            qdir = self.incoming / "_quarantine" / report.source_id
            qdir.mkdir(parents=True, exist_ok=True)
            target = qdir / path.name
            path.replace(target)
            row = _review_row(cand, str(screen.decision))
            row["distribution_text"] = screen.marking.text
            row["quarantined"] = str(target)
            report.held.append(row)
            return
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            while chunk := fh.read(8 << 20):
                digest.update(chunk)
        sha = digest.hexdigest()
        if held_sha.get(cand.key) == sha:
            path.unlink()  # byte-identical to the stored edition: nothing new
            report.skipped += 1
            return
        marking = screen.marking if screen is not None else None
        report.fetched.append(
            FetchedDoc(
                cand=cand,
                file=f"upstream/{path.name}",
                sha256=sha,
                bytes=path.stat().st_size,
                decision=str(screen.decision) if screen is not None else "allow:git",
                distribution=(marking.distribution if marking else "") or _rights_basis(cand),
                distribution_text=marking.text if marking else "",
                fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
                license=cand.rights.removeprefix("license:") if cand.rights.startswith("license:") else "",
            )
        )

    def _finish(
        self,
        spec: SourceSpec,
        coll: SourceSpec,
        report: FetchReport,
        prev_gen: str | None,
        prev_catalog: Sequence[Any],
        survey: Survey,
        dest_dir: Path,
    ) -> None:
        cfg = self.adapter.config(coll)
        listed = {c.key for c in survey.allowed}
        catalog = build_catalog(prev_catalog, prev_gen, report.fetched, cfg.currency, listed=listed)
        if not report.fetched:
            self._say(f"{spec.id}: nothing new to ingest")
            return
        catalog_path = dest_dir / CATALOG
        sha = write_catalog(catalog_path, catalog)
        targets = [
            {
                "source_id": spec.id,
                "version": report.version,
                "name": doc.file.removeprefix("upstream/"),
                "uris": [doc.cand.url],
                "lane": "collections",
                "checksum": ["sha-256", doc.sha256],
                "size": doc.bytes,
                "meta": {"doc_key": doc.cand.key},
            }
            for doc in report.fetched
        ]
        targets.append(
            {
                "source_id": spec.id,
                "version": report.version,
                "name": CATALOG,
                "uris": [],
                "lane": "collections",
                "checksum": ["sha-256", sha],
                "size": catalog_path.stat().st_size,
                "meta": {"relpath": CATALOG},
            }
        )
        plan = self.incoming / "_plans" / PLANS_SUBDIR / f"{spec.id}-{report.version}.json"
        plan.parent.mkdir(parents=True, exist_ok=True)
        tmp = plan.with_name(plan.name + ".partial")
        tmp.write_text(json.dumps(targets, indent=1), encoding="utf-8")
        tmp.replace(plan)
        self._say(f"{spec.id}: {len(report.fetched)} documents ready; plan {plan}")


def _pointer(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value or None


def _rights_basis(cand: Candidate) -> str:
    return cand.rights if cand.rights.startswith("license:") else ""


def _review_row(cand: Candidate, rule: str) -> dict[str, str]:
    return {
        "key": cand.key,
        "pub_number": cand.pub_number,
        "title": cand.title[:200],
        "url": cand.url,
        "rule": rule,
    }


def withheld_review_row(name: str, url: str, rule: str) -> dict[str, str]:
    """Review row for a file the release pipeline held (ADR-0010); the key is read from the file name
    (``<key with ':' as '__'>-<hash8>.<ext>``, see :attr:`Candidate.file_name`)."""
    stem = name.rsplit(".", 1)[0]
    key = stem.rsplit("-", 1)[0].replace("__", ":")
    return {"key": key, "pub_number": "", "title": key, "url": url, "rule": rule.removeprefix("policy ")}


def write_review_proposal(
    cfg: Mapping[str, Any], spec: SourceSpec, version: str, rows: Sequence[Mapping[str, str]]
) -> Path | None:
    """One owner-review proposal for a batch of documents (research doc §5.5)."""
    if not rows:
        return None
    from wsp_orchestrator.curator.proposals import ProposalFiles, ProposalRecord
    from wsp_orchestrator.policy import Candidate as PolicyCandidate
    from wsp_orchestrator.policy import CandidateKind, Decision, Proposal
    from wsp_orchestrator.store import StateStore

    paths = bootstrap.paths(cfg)
    key = f"doc-review:{spec.id}:{version}"
    body = "\n".join(
        [f"{len(rows)} document(s) of `{spec.id}` need owner review (research doc §5.5).", ""]
        + [f"- `{r.get('pub_number') or r['key']}` {r['title']} ({r['rule']})" for r in rows[:200]]
    )
    proposal = Proposal(
        candidate=PolicyCandidate(
            key=key,
            name=f"{spec.id}: {len(rows)} documents need review",
            kind=CandidateKind.REGISTRY_FIX,
            license=spec.license,
            domain=spec.domain,
            found_by="collections",
            existing_source_id=spec.id,
        ),
        decision=Decision.REVIEW,
        score=None,
        breakdown={},
        checks=(),
        title=f"{spec.id}: {len(rows)} documents need review",
        body=body,
    )
    now = datetime.now(UTC)
    state_db = Path(str(bootstrap.section(cfg, "orchestrator").get("state_db") or ""))
    if not state_db.is_file():
        return None
    store = StateStore(state_db, check_local=False)
    try:
        pid = store.add_proposal(key, Decision.REVIEW.value, proposal.to_json(), now)
    finally:
        store.close()
    files = ProposalFiles(paths.proposals)
    path = files.write(ProposalRecord(pid, "open", now, proposal))
    doc = cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    doc["doc_review"] = {"source_id": spec.id, "version": version, "documents": [dict(r) for r in rows]}
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    tmp.replace(path)
    return path


def apply_review_decision(repo_root: Path, doc: Mapping[str, Any], *, accept: bool) -> Path | None:
    """Record an owner decision in ``registry/filters/<source>.yaml`` so it is never asked again."""
    review = cast(Mapping[str, Any], doc.get("doc_review") or {})
    source_id = str(review.get("source_id") or "")
    if not source_id:
        return None
    rows = cast(list[Mapping[str, str]], review.get("documents") or [])
    keys = sorted(
        {str(r.get("pub_number") or r.get("key")) for r in rows if r.get("pub_number") or r.get("key")}
    )
    path = repo_root / "registry" / "filters" / f"{source_id}.yaml"
    data = cast(
        dict[str, Any], (yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None) or {}
    )
    field_name = "allow_documents" if accept else "deny_documents"
    current = [str(x) for x in cast(list[object], data.get(field_name) or [])]
    data[field_name] = sorted(set(current) | set(keys))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    tmp.replace(path)
    return path


def cmd_fetch(args: argparse.Namespace, out: TextIO) -> int:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    specs = {s.id: s for s in bootstrap.load_sources(cfg, lay)}
    spec = specs.get(args.source)
    if spec is None:
        raise bootstrap.ConfigError(f"unknown source {args.source!r}")
    if spec.status is SourceStatus.EXCLUDED or spec.raw.get("legal_basis") == "none":
        print(f"error: {spec.id} is excluded or has no legal basis", file=sys.stderr)
        return 2
    if spec.partitioned and not args.partition and not args.dry_run:
        print(f"error: {spec.id} is fetched one partition per run: add --partition", file=sys.stderr)
        return 2
    if args.partition and not spec.partitioned:
        print(f"error: {spec.id} is not partitioned (access.partitioned): drop --partition", file=sys.stderr)
        return 2
    try:
        partition = check_partition(args.partition.lower()) if args.partition else None
    except PartitionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    paths = bootstrap.paths(cfg)
    vault_root = paths.require_vault()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    cycle = args.version or today
    version = partition_generation_id(cycle, partition) if partition else cycle
    workdir = (paths.workdir or state_dir()) / "collections"
    runner = CollectionFetcher(
        vault_root,
        bootstrap.build_fetcher(cfg),
        keys=api_keys(cfg),
        user_agent=bootstrap.user_agent(cfg),
        workdir=workdir,
        out=out,
    )
    report = runner.run(
        spec,
        version=version,
        limit=args.limit,
        max_pages=args.max_pages,
        dry_run=args.dry_run,
        partition=partition,
    )
    rows = [*report.review, *report.held]
    proposal = None if args.dry_run else write_review_proposal(cfg, spec, version, rows)
    out.write(
        f"{spec.id}/{version}: fetched {len(report.fetched)}, skipped {report.skipped}, "
        f"rejected {len(report.rejected)}, held {len(report.held)}, review {len(report.review)}, "
        f"failed {len(report.failed)}\n"
    )
    for alert in report.alerts:
        out.write(f"ALERT restricted document listed by the issuer: {alert}\n")
    for key, why in report.failed[:20]:
        out.write(f"failed: {key}: {why}\n")
    if proposal is not None:
        out.write(f"review proposal: {proposal} (wsp proposals list)\n")
    elif rows and not args.dry_run:
        side = state_dir() / f"review-{spec.id}-{version}.json"
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        out.write(f"review list (no orchestrator state DB for proposals): {side}\n")
    if report.fetched:
        out.write(f"next: wsp ingest --source {spec.id}\n")
    return 1 if report.alerts else 0


def register(sub: Any, add: Any) -> None:
    """Add ``wsp collections ...`` to the CLI parser (called from :func:`wsp_cli.main.build_parser`)."""
    co = add("collections", _usage, "Document collections: dry run and polite fetch (D11, D13, D14).")
    co_sub = co.add_subparsers(dest="collections_command", required=True, metavar="COMMAND")
    s = add(
        "dry-run",
        cmd_dry_run,
        "List what would be fetched per source, with sizes and filter decisions (metadata only).",
        co_sub,
    )
    s.add_argument("--source", action="append", default=None, help="source id (repeatable)")
    s.add_argument("--top", action="store_true", help="the research doc's top picks (§6)")
    s.add_argument("--max-pages", type=int, default=3, help="listing pages per discoverer (sample); 0 = all")
    s.add_argument("--resolve-sample", type=int, default=5, help="landing pages to resolve per source")
    s.add_argument("--head-sample", type=int, default=3, help="HEAD requests per source for sizes")
    s.add_argument("--demo-key", action="store_true", help="use api.data.gov DEMO_KEY (tiny smoke checks)")
    s.add_argument(
        "--out", default=None, help="report path (default: ~/.local/state/whisipedia/collections/)"
    )
    s.add_argument("--json", action="store_true")
    s = add(
        "fetch", cmd_fetch, "Fetch one collection politely into <vault>/_incoming (then: wsp ingest).", co_sub
    )
    s.add_argument("--source", required=True)
    s.add_argument("--version", default=None, help="cycle label (default: today, YYYY-MM-DD)")
    s.add_argument("--limit", type=int, default=None, help="at most this many documents")
    s.add_argument("--max-pages", type=int, default=None, help="listing pages per discoverer")
    s.add_argument("--dry-run", action="store_true", help="discover and classify only")
    s.add_argument(
        "--partition",
        default=None,
        help="partitioned sets: one state (usgs-historical-topo) or batch b0001 (cycle <date>.<partition>)",
    )


def _usage(_: argparse.Namespace, __: TextIO) -> int:  # pragma: no cover - argparse requires a subcommand
    return 2


__all__ = ["TOP_PICKS", "dry_run", "register", "render_dry_run"]
