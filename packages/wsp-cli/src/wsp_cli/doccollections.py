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
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from collections import deque
from collections.abc import Collection, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO, cast
from urllib.parse import urlsplit

import yaml

from wsp_adapters.doccollection.adapter import (
    CATALOG,
    DocCollectionAdapter,
    Extracted,
    Screen,
    Survey,
    WrongBodyError,
    checked_front_matter,
    download_from_origins,
    fetched_doc,
    screen_document,
    screen_text,
    true_suffix,
)
from wsp_adapters.doccollection.catalog import (
    FetchedDoc,
    build_catalog,
    catalog_bytes,
    read_catalog_file,
    write_catalog,
)
from wsp_adapters.doccollection.discovery import Throttle
from wsp_adapters.doccollection.model import Candidate, DocumentMeta
from wsp_adapters.doccollection.policy import (
    DECISION_KEYS,
    Decision,
    Marking,
    Rules,
    Stage,
    Verdict,
    decisions_path,
)
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


# `wsp collections fetch` exit codes: one per outcome, so a lane can tell "this cycle is complete" from
# everything else (audit 2026-10-12, C1; docs/runbooks/document-collections.md, "Exit codes"). An
# uncaught error exits 1 (Python's traceback exit) and a kill exits 128+signal (137, 143): neither is
# ever complete. Only 0, 7 and 8 are complete; a lane marks an item done only after one of them AND a
# successful `wsp ingest --source S --version V` (where, after 7 or 8, "nothing planned" is expected).
EXIT_COMPLETE = 0  # every listed document fetched, unchanged, rejected or held; the plan is written
EXIT_REFUSED = 2  # not fetched at all: excluded source, no legal basis, wrong or missing --partition
EXIT_LISTING_INCOMPLETE = 4  # a listing failed; nothing fetched, retry later (same cycle)
EXIT_INGEST_PENDING = 5  # refused: an earlier fetch of the source awaits `wsp ingest`; nothing fetched
EXIT_PARTIAL = 6  # some or all downloads failed (or another fetch held them); the rest is planned
EXIT_ALERT = 7  # complete, but the issuer lists restricted documents (deleted, never kept): read them
EXIT_NOTHING_NEW = 8  # complete, and nothing changed upstream: no plan, nothing to ingest


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


def summarize(
    spec: SourceSpec, survey: Survey, *, head_sizes: Sequence[int] = (), sampled: bool = True
) -> SourceDryRun:
    """``sampled``: the listing was cut short (``--max-pages``), so totals are extrapolated; a complete
    listing that is smaller than ``upstream`` is a filtered one (a package prefix), never scaled up."""
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
    if sampled and out.upstream_total and out.discovered and out.upstream_total > out.discovered:
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
            results.append(summarize(spec, survey, head_sizes=sizes, sampled=max_pages is not None))
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
    incomplete: list[str] = field(default_factory=list[str])  # the listing failed: nothing was fetched
    skipped: int = 0
    alerts: list[str] = field(default_factory=list[str])
    pending: list[str] = field(default_factory=list[str])  # earlier versions not yet ingested: refused
    planned: bool = False  # an ingest plan was written (new documents, or a changed catalog)


def fetch_exit_code(report: FetchReport) -> int:
    """The exit code of a finished (not dry-run) fetch: the worst outcome wins (4, 6, 7, then 8 or 0)."""
    if report.incomplete:
        return EXIT_LISTING_INCOMPLETE
    if report.failed:
        return EXIT_PARTIAL
    if report.alerts:
        return EXIT_ALERT
    return EXIT_COMPLETE if report.fetched or report.planned else EXIT_NOTHING_NEW


_NO_CONNECTION = re.compile(r"\bConnect(?:Timeout|Error)\b")


class HostBreaker:
    """Hosts that never answer this run. A host whose connections failed :attr:`AFTER` times in a row,
    with no answer from it this run, is not tried again until the next run: its documents fail at once.
    Borden Institute's 315 documents on an army.mil host that refuses this connection held the retry
    lane for hours, each through six connect timeouts (2026-10-21). Any answer, a 404 too, resets it."""

    AFTER = 5

    def __init__(self) -> None:
        self._failures: dict[str, int] = {}
        self._answered: set[str] = set()

    @staticmethod
    def _host(url: str) -> str:
        return (urlsplit(url).hostname or "").lower()

    def answered(self, url: str) -> None:
        host = self._host(url)
        self._answered.add(host)
        self._failures.pop(host, None)

    def failed(self, url: str, why: str) -> None:
        if _NO_CONNECTION.search(why) is None:
            self.answered(url)  # it answered, with an error
            return
        host = self._host(url)
        self._failures[host] = self._failures.get(host, 0) + 1

    def down(self, url: str) -> str | None:
        """Why ``url`` is not tried this run, or ``None``."""
        host = self._host(url)
        n = self._failures.get(host, 0)
        if host in self._answered or n < self.AFTER:
            return None
        return f"HostUnreachable: {host} took no connection {n} times in a row; left for the next run"


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
        refetch: Collection[str] = (),
    ) -> FetchReport:
        """``refetch``: document keys downloaded again even when the listing says the held bytes are
        current (repair of a record whose metadata outran its file)."""
        coll = collection_spec(spec)
        if coll is None:
            raise bootstrap.ConfigError(f"{spec.id} is not a document collection")
        own_partition = partition if spec.partitioned else None
        if not dry_run:
            version = self.free_version(spec, version, own_partition)
        report = FetchReport(spec.id, version)
        # Another version of this source (this partition) still awaiting ingest blocks the fetch; the
        # same version does not: fetching it again is how an interrupted ingest is repaired (audit H2).
        pending = [] if dry_run else [v for v in self.pending_plans(spec, own_partition) if v != version]
        if pending:
            report.pending = pending  # a fetch now would build its catalog without their documents
            return report
        ctx = Ctx(
            source=coll, fetcher=self.fetcher, workdir=self.workdir / spec.id, user_agent=self.user_agent
        )
        env = self.adapter.env(ctx, max_pages=max_pages, partition=partition)
        survey = self.adapter.survey(ctx, max_pages=max_pages, env=env, progress=self._say)
        cfg = self.adapter.config(coll)
        for cand, decision in survey.decisions:
            if decision.verdict is Verdict.REVIEW:
                report.review.append(_review_row(cand, str(decision)))
        prev_gen, prev_catalog = self.previous_catalog(spec, own_partition)
        held_sha = {m.doc_key: m.sha256 for m in prev_catalog}
        held_meta = {m.doc_key: m for m in prev_catalog}
        dest_dir = self.incoming / spec.id / version
        docs = survey.allowed[:limit] if limit else survey.allowed
        self._say(
            f"{spec.id}: {len(survey.decisions)} listed, {len(docs)} to fetch, "
            f"{survey.count(Verdict.DENY)} denied, {survey.count(Verdict.REVIEW)} for review"
        )
        if dry_run:
            return report
        if survey.incomplete:
            # A partial listing must never become a generation: documents it misses would look withdrawn
            # (and a partitioned batch would be marked done with most of its items missing).
            report.incomplete = list(survey.incomplete)
            return report
        throttle = env.fetcher
        repos = registry_repos(coll)
        # Downloads overlap when the source allows it (``rate.concurrency``); the throttle still spaces
        # their starts. Screening and the report stay on this thread, in completion order.
        pool = (
            ThreadPoolExecutor(cfg.concurrency, thread_name_prefix="fetch") if cfg.concurrency > 1 else None
        )
        inflight: deque[tuple[Candidate, Future[Path]]] = deque()
        fetch_errors = (FetchError, OSError, ValueError, subprocess.CalledProcessError)
        hosts = HostBreaker()

        def failed(cand: Candidate, exc: BaseException) -> None:
            why = f"{type(exc).__name__}: {exc}"
            hosts.failed(cand.url, why)
            report.failed.append((cand.key, why))

        def drain(keep: int) -> None:
            while len(inflight) > keep:
                cand, fut = inflight.popleft()
                try:
                    path = fut.result()
                except fetch_errors as exc:
                    failed(cand, exc)
                    continue
                hosts.answered(cand.url)
                self._screen(cand, path, cfg.rules, report, held_sha, screen=None)

        try:
            for cand in docs:
                if cand.key not in refetch and unchanged(cand, held_meta.get(cand.key)):
                    report.skipped += 1  # held, and the listing says the same bytes: not downloaded again
                    continue
                if (down := hosts.down(cand.url)) is not None:
                    report.failed.append((cand.key, down))
                    continue
                if pool is not None and cand.resolve != "git":
                    inflight.append(
                        (cand, pool.submit(self._download, throttle, cand, dest_dir, cfg.origins))
                    )
                    drain(2 * cfg.concurrency)
                    continue
                try:
                    if cand.resolve == "git":
                        path, screen = self._git_bundle(cand, dest_dir, cfg.rules, repos)
                    else:
                        path = self._download(throttle, cand, dest_dir, cfg.origins)
                        screen = None
                except fetch_errors as exc:
                    failed(cand, exc)
                    continue
                hosts.answered(cand.url)
                self._screen(cand, path, cfg.rules, report, held_sha, screen=screen)
            drain(0)
        finally:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
        self._finish(spec, coll, report, prev_gen, prev_catalog, survey, dest_dir)
        return report

    def _download(self, fetcher: Fetcher, cand: Candidate, dest_dir: Path, origins: Sequence[str]) -> Path:
        """Download within ``origins``; every redirect hop and the final URL are checked too.

        A run interrupted after some downloads (a reboot, a restarted lane) keeps them: a complete file
        already in ``dest_dir`` whose MD5 matches the one the issuer's listing published is used as is.
        Without a published checksum the file is fetched again (its origin cannot be re-checked).
        """
        done = dest_dir / cand.file_name
        if cand.md5 and done.is_file() and (cand.size is None or done.stat().st_size == cand.size):
            digest = hashlib.md5(usedforsecurity=False)
            with done.open("rb") as fh:
                while chunk := fh.read(8 << 20):
                    digest.update(chunk)
            if digest.hexdigest() == cand.md5.lower():
                return done
        remote = RemoteFile(url=cand.url, name=cand.file_name, size=cand.size, mirrors=cand.mirrors)
        return Path(download_from_origins(fetcher, remote, dest_dir / cand.file_name, origins).path)

    def _git(self, *args: str, cwd: Path | None = None, capture: bool = False) -> str:
        """``git`` over HTTPS only, no redirects, no prompts, no hooks from the remote."""
        assert self.git is not None
        cmd = [self.git, *GIT_SAFE_CONFIG]
        if cwd is not None:
            cmd += ["-C", str(cwd)]
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GIT_CONFIG_NOSYSTEM": "1"}
        done = subprocess.run(  # noqa: S603
            [*cmd, *args], check=True, env=env, stdout=subprocess.PIPE if capture else None
        )
        return done.stdout.decode("utf-8", "replace") if capture else ""

    def _git_bundle(
        self, cand: Candidate, dest_dir: Path, rules: Rules, repos: frozenset[str]
    ) -> tuple[Path, Screen]:
        """Mirror-clone a registry-listed GitHub repository into one ``git bundle`` and screen it.

        Only repositories named in the source's registry ``endpoints`` are cloned (the allow-list),
        over HTTPS from ``github.com`` with redirects refused. The screen reads the default
        branch's README and its file paths (:meth:`_repo_screen_text`); file contents beyond the
        README are not screened.
        """
        if self.git is None:
            raise OSError("git is not installed")
        repo = github_repo(cand.url)
        if repo is None or not (repo in repos or f"{repo.split('/')[0]}/*" in repos):
            raise ValueError(f"{cand.url} is not a repository the registry lists ({sorted(repos)})")
        dest_dir.mkdir(parents=True, exist_ok=True)
        if cand.extra.get("snapshot"):
            return self._git_snapshot(cand, repo, dest_dir, rules)
        mirror = self.workdir / "git" / cand.key.replace(":", "_")
        if mirror.is_dir():
            self._git("remote", "update", "--prune", cwd=mirror)
        else:
            mirror.parent.mkdir(parents=True, exist_ok=True)
            self._git("clone", "--mirror", "--no-local", f"https://github.com/{repo}.git", str(mirror))
        screen = screen_text(self._repo_screen_text(mirror), rules, keys=(cand.key, cand.pub_number))
        bundle = dest_dir / cand.file_name.rsplit(".", 1)[0]
        bundle = bundle.with_name(bundle.name + ".bundle")
        partial = bundle.with_name(bundle.name + ".partial")
        self._git("bundle", "create", str(partial), "--all", cwd=mirror)
        partial.replace(bundle)
        return bundle, screen

    def _git_snapshot(self, cand: Candidate, repo: str, dest_dir: Path, rules: Rules) -> tuple[Path, Screen]:
        """The default branch's files as one zip (``git archive``), from a shallow clone kept up to date
        with a shallow fetch, screened like a bundle (its README and file paths)."""
        branch = str(cand.extra.get("default_branch") or "HEAD")
        clone = self.workdir / "git" / (cand.key.replace(":", "_") + ".shallow")
        if clone.is_dir():
            self._git("fetch", "--depth", "1", "origin", f"+{branch}:{branch}", cwd=clone)
        else:
            clone.parent.mkdir(parents=True, exist_ok=True)
            args = ["clone", "--bare", "--depth", "1", "--single-branch", "--no-local"]
            if branch != "HEAD":
                args += ["--branch", branch]
            self._git(*args, f"https://github.com/{repo}.git", str(clone))
        screen = screen_text(self._repo_screen_text(clone), rules, keys=(cand.key, cand.pub_number))
        snapshot = dest_dir / (cand.file_name.rsplit(".", 1)[0] + ".zip")
        partial = snapshot.with_name(snapshot.name + ".partial")
        self._git("archive", "--format=zip", "-o", str(partial), branch, cwd=clone)
        partial.replace(snapshot)
        return snapshot, screen

    def _repo_screen_text(self, mirror: Path) -> Extracted:
        """What the gate reads of a repository: the default branch's README, then its file paths.

        A path list names a repository's contents the way a table of contents names a book's
        (``exploit-development/``, ``shaped-charges.md``), so the content screen's title-page and
        three-phrase rules apply to it. File contents other than the README are not read.
        """
        try:
            names = self._git("ls-tree", "-r", "--name-only", "HEAD", cwd=mirror, capture=True)
        except subprocess.CalledProcessError:
            return Extracted("", "extraction-error")
        readme = next(
            (n for n in names.splitlines() if "/" not in n and n.lower().startswith("readme")),
            None,
        )
        text = ""
        if readme is not None:
            try:
                text = self._git("show", f"HEAD:{readme}", cwd=mirror, capture=True)[:50_000]
            except subprocess.CalledProcessError:
                return Extracted("", "extraction-error")
        paths = names[:GIT_SCREEN_PATHS_CHARS].replace("/", " / ").replace("_", " ")
        return checked_front_matter(f"{text}\n\n{paths}", "")

    def _screen(
        self,
        cand: Candidate,
        path: Path,
        rules: Rules,
        report: FetchReport,
        held_sha: Mapping[str, str],
        *,
        screen: Screen | None = None,
    ) -> None:
        if screen is None and path.suffix != ".bundle":
            try:
                # A name guessed ".pdf" gets its real office/zip/text suffix; an error or landing page
                # in its place is a failed download, retried next run (CA-2).
                path = true_suffix(path)
                screen = screen_document(path, rules, keys=(cand.key, cand.pub_number))
            except WrongBodyError as exc:
                report.failed.append((cand.key, str(exc)))
                path.unlink(missing_ok=True)
                return
        elif screen is None:  # a bundle is never kept unscreened
            screen = Screen(Decision(Verdict.REVIEW, "git=unscreened", Stage.CONTENT), Marking(), False)
        if screen.decision.verdict is Verdict.DENY:
            path.unlink(missing_ok=True)  # restricted or out-of-scope bytes are not kept anywhere
            report.rejected.append((cand.key, str(screen.decision)))
            if screen.decision.alert:
                report.alerts.append(f"{cand.key}: {screen.decision.rule} ({cand.url})")
            return
        if screen.decision.verdict is Verdict.REVIEW:
            qdir = self.incoming / "_quarantine" / report.source_id
            qdir.mkdir(parents=True, exist_ok=True)
            target = qdir / path.name
            path.replace(target)
            row = _review_row(cand, str(screen.decision))
            row["distribution_text"] = screen.marking.text
            row["quarantined"] = str(target)
            report.held.append(row)
            return
        doc = fetched_doc(cand, path, screen, file=f"upstream/{path.name}")
        if held_sha.get(cand.key) == doc.sha256:
            path.unlink()  # byte-identical to the stored edition: nothing new
            report.skipped += 1
            return
        report.fetched.append(doc)

    def _finish(
        self,
        spec: SourceSpec,
        coll: SourceSpec,
        report: FetchReport,
        prev_gen: str | None,
        prev_catalog: Sequence[DocumentMeta],
        survey: Survey,
        dest_dir: Path,
    ) -> None:
        cfg = self.adapter.config(coll)
        # Withdrawal is inferred only from a complete listing, and never for an item whose documents are
        # unknown this run (its resolve failed) or that is listed but held (CA-9, CA-10).
        listed = (
            {c.key for c in survey.allowed} | {c.key for c, _d in survey.decisions}
            if survey.listing_complete
            else None
        )
        catalog = build_catalog(
            prev_catalog,
            prev_gen,
            report.fetched,
            cfg.currency,
            listed=listed,
            protected=survey.protected_keys(),
            listing={c.key: c for c in survey.allowed},
            policy=cfg.rules,
            formats=cfg.formats,
        )
        if not report.fetched:
            before = catalog_bytes(
                replace(m, generation=m.generation or (prev_gen or "")) for m in prev_catalog
            )
            if catalog_bytes(catalog) == before:
                self._say(f"{spec.id}: nothing new to ingest")
                return
            # Nothing new to fetch, but labels changed (a withdrawal, a rescission, an owner deny, an
            # edition relabelled): a catalog-only generation records them (CA-14).
            self._say(f"{spec.id}: no new documents; the catalog changed")
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
        plan = self.plan_path(spec.id, report.version)
        plan.parent.mkdir(parents=True, exist_ok=True)
        tmp = plan.with_name(plan.name + ".partial")
        tmp.write_text(json.dumps(targets, indent=1), encoding="utf-8")
        tmp.replace(plan)
        report.planned = True
        self._say(f"{spec.id}: {len(report.fetched)} documents ready; plan {plan}")

    # --- versions (CA-15) ------------------------------------------------------------------------

    def plan_path(self, source_id: str, version: str) -> Path:
        return self.incoming / "_plans" / PLANS_SUBDIR / f"{source_id}-{version}.json"

    def sealed(self, spec: SourceSpec, version: str) -> bool:
        return (
            self.vault_root / "library" / spec.vault_domain / spec.id / version / "MANIFEST.json"
        ).is_file()

    def free_version(self, spec: SourceSpec, version: str, partition: str | None) -> str:
        """``version``, or the next ``-rN`` cycle when that generation is already sealed: a second fetch
        on the same day must not overwrite the plan of a sealed generation, whose ingest would then
        report "already sealed" and drop the new documents silently."""
        cycle = version[: -len(partition) - 1] if partition and version.endswith(f".{partition}") else version
        # "<cycle>rN", as the lanes name a retry: "-rN" sorts *before* the cycle once a partition suffix
        # follows ("2026-10-12-r2.ak" < "2026-10-12.ak"), so a newer re-fetch would never be promoted.
        m = re.fullmatch(r"(.+?)-?r(\d+)", cycle)
        root, n = (m.group(1), int(m.group(2))) if m else (cycle, 1)
        current = version
        while self.sealed(spec, current):
            n += 1
            nxt = f"{root}r{n}"
            current = partition_generation_id(nxt, partition) if partition else nxt
        return current

    def pending_plans(self, spec: SourceSpec, partition: str | None) -> list[str]:
        """Versions of this source (this partition) fetched but not yet sealed. A new fetch builds its
        catalog from the current generation, so it must wait for them: it would leave their documents
        out of the newer catalog."""
        plans = self.incoming / "_plans" / PLANS_SUBDIR
        if not plans.is_dir():
            return []
        pending: list[str] = []
        for path in sorted(plans.glob(f"{spec.id}-*.json")):
            try:
                targets = cast(list[Mapping[str, Any]], json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
            versions = {str(t.get("version")) for t in targets if t.get("source_id") == spec.id}
            for version in sorted(versions):
                if partition is not None and partition_of(version) != partition:
                    continue
                if not self.sealed(spec, version):
                    pending.append(version)
        return pending


# Clone and fetch over HTTPS only, refuse redirects (the origin is the URL the registry names), and
# never let the remote pick a protocol or credentials helper.
GIT_SAFE_CONFIG: tuple[str, ...] = (
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "http.followRedirects=false",
    "-c",
    "credential.helper=",
    "-c",
    "core.hooksPath=/dev/null",
)
_GITHUB_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
GIT_SCREEN_PATHS_CHARS = 200_000


def github_repo(url: str) -> str | None:
    """``owner/name`` of an ``https://github.com/owner/name[.git]`` URL, else ``None``."""
    m = _GITHUB_RE.match(url.strip())
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None


def registry_repos(spec: SourceSpec) -> frozenset[str]:
    """The repositories a git-mirror source's registry ``endpoints`` name: its git allow-list. A git
    discovery of an organisation (``{kind: git, org: standardebooks}``) allows that organisation's
    repositories (``owner/*``): Standard Ebooks lists 1,533 books there, none named one by one, and
    every one was refused (2026-10-19)."""
    acc = cast(Mapping[str, Any], spec.raw.get("access") or {})
    named = (github_repo(str(u)) for u in cast(list[object], acc.get("endpoints") or []))
    orgs = (
        f"{str(d.get('org')).lower()}/*"
        for d in cast(list[Mapping[str, Any]], acc.get("discovery") or [])
        if d.get("kind") == "git" and d.get("org")
    )
    return frozenset([*(r for r in named if r), *orgs])


def _pointer(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value or None


REFETCH_AFTER = timedelta(days=180)  # a held document with no listed size or checksum is re-checked


def _same_place(url: str) -> str:
    """A URL without its scheme and with its host lower-cased: an http to https move is the same file."""
    rest = url.split("://", 1)[-1]
    host, sep, path = rest.partition("/")
    return host.lower() + sep + path


def unchanged(cand: Candidate, held: DocumentMeta | None, *, now: datetime | None = None) -> bool:
    """The listing says ``held`` already has this candidate's bytes, so it is not downloaded again
    (2026-10-12 audit, CA-13): the listing's MD5 matches the stored bytes' MD5, else its size matches.
    A listing that gives neither is trusted for :data:`REFETCH_AFTER` after the last fetch, then the
    document is fetched again, so an issuer's quiet correction is not missed forever. The catalog
    refreshes a skipped document's metadata from the listing either way."""
    if held is None or cand.resolve == "git" or not held.sha256:
        return False
    if held.origin_url and _same_place(cand.url) != _same_place(held.origin_url):
        # The key now lists another file (field exam 2026-10-14: the Pilot's Handbook key moved from
        # its addendum to the full handbook and kept the addendum's bytes). Fetch it; byte-identical
        # bytes are still recognised after the download and kept as they are.
        return False
    if cand.md5 and held.md5:
        return cand.md5.lower() == held.md5.lower() and (cand.size is None or cand.size == held.bytes)
    if cand.size is not None:
        return cand.size == held.bytes
    try:
        fetched = datetime.fromisoformat(held.fetched_at)
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) - fetched < REFETCH_AFTER


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


def unproposed_rows(
    cfg: Mapping[str, Any], source_id: str, rows: Sequence[Mapping[str, str]]
) -> list[dict[str, str]]:
    """``rows`` without repeats and without documents an open review proposal of the source already
    lists (2026-10-12 audit, CA-16): a held document is proposed once, not again every run."""
    from wsp_orchestrator.curator.proposals import ProposalFiles

    try:
        root: Path | None = bootstrap.paths(cfg).proposals
    except bootstrap.ConfigError:  # no index root: no proposal files either
        root = None
    listed: set[str] = set()
    if root is not None and root.is_dir():
        for doc in ProposalFiles(root).list(status="open"):
            review = cast(Mapping[str, Any], doc.get("doc_review") or {})
            if review.get("source_id") == source_id:
                listed |= {
                    str(r.get("key")) for r in cast(list[Mapping[str, Any]], review.get("documents") or [])
                }
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        if row["key"] not in listed:
            out.setdefault(row["key"], dict(row))
    return list(out.values())


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
    more = (
        ["", f"... and {len(rows) - 200} more (every row is under `doc_review`)."] if len(rows) > 200 else []
    )
    body = "\n".join(
        [f"{len(rows)} document(s) of `{spec.id}` need owner review (research doc §5.5).", ""]
        + [f"- `{r['key']}` {r['title']} ({r['rule']})" for r in rows[:200]]
        + more
        + [
            "",
            "Decide per row: `wsp proposals accept ID [--only KEY ...]` allows rows; "
            "`wsp proposals reject ID [--deny KEY ...]` closes the proposal and denies only the named rows.",
        ]
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


class ReviewDecisionError(ValueError):
    """A review decision that cannot be recorded (no rules file, unknown rows)."""


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    path: Path  # the machine-owned decisions file written
    keys: tuple[str, ...]  # document keys decided


DECISIONS_HEADER = """\
# Owner review decisions for {source} (machine-owned; written by `wsp proposals accept|reject`).
# One entry per document the owner decided, keyed by document key (one edition). The hand-written
# rules, with their comments, are in {rules}; this file is loaded next to it (policy.load_rules).
# Remove an entry to undo a decision; anything else is rewritten by the next decision.
"""


def apply_review_decision(
    repo_root: Path,
    specs: Sequence[SourceSpec],
    doc: Mapping[str, Any],
    *,
    allow: bool,
    keys: Sequence[str] | None = None,
    proposal: int | None = None,
    at: datetime | None = None,
) -> ReviewDecision:
    """Record the owner's decision on rows of a document-review proposal (2026-10-12 audit, CA-1).

    ``allow``: the rows named by ``keys`` (every row when ``None``) are allowed. Otherwise only the
    rows named by ``keys`` are denied; denying needs explicit keys, so closing a proposal never
    denies a batch. Decisions are keyed by document key (one edition, CA-16): an allowed FM 5-0 does
    not allow its next edition. A row the content screen held (``review:safety=...``) is released
    with ``allow_content_review``; nothing here lifts a deny.

    Decisions go to the machine-owned :func:`~wsp_adapters.doccollection.policy.decisions_path` of
    the source's ``filter.rules`` file, never into the hand-written rules file, and a source without
    a rules file is refused: a decision written where nothing loads it would wait to deny or allow
    documents the day someone wires it.
    """
    review = cast(Mapping[str, Any], doc.get("doc_review") or {})
    source_id = str(review.get("source_id") or "")
    spec = next((s for s in specs if s.id == source_id), None)
    if spec is None:
        raise ReviewDecisionError(f"source {source_id!r} of this review is not in the registry")
    acc = cast(Mapping[str, Any], spec.raw.get("access") or {})
    rules_name = cast(Mapping[str, Any], acc.get("filter") or {}).get("rules")
    if not rules_name:
        raise ReviewDecisionError(
            f"{source_id} has no filter.rules file in the registry: a decision would not be loaded. "
            f"Add `rules: registry/filters/{source_id}.yaml` to its filter first."
        )
    rules_file = Path(str(rules_name))
    rules_file = rules_file if rules_file.is_absolute() else repo_root / rules_file
    if not rules_file.is_file():
        raise ReviewDecisionError(f"{rules_file} (filter.rules of {source_id}) does not exist")
    rows = {
        str(r.get("key")): r
        for r in cast(list[Mapping[str, str]], review.get("documents") or [])
        if r.get("key")
    }
    if keys is None:
        if not allow:
            raise ReviewDecisionError("name the rows to deny (--deny KEY); a rejection alone denies nothing")
        chosen = list(rows)
    else:
        unknown = sorted(set(keys) - set(rows))
        if unknown:
            raise ReviewDecisionError(f"not rows of this review: {', '.join(unknown)}")
        chosen = list(dict.fromkeys(keys))
    if not chosen:
        raise ReviewDecisionError("the review lists no documents")
    path = decisions_path(rules_file)
    data: dict[str, Any] = {}
    if path.is_file():
        data = dict(cast(Mapping[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")) or {}))
    lists = {k: [str(x) for x in cast(list[object], data.get(k) or [])] for k in DECISION_KEYS}
    log = list(cast(list[Mapping[str, Any]], data.get("log") or []))
    when = (at or datetime.now(UTC)).isoformat(timespec="seconds")
    for key in chosen:
        rule = str(rows[key].get("rule") or "")
        content_hold = rule.startswith(
            "review:safety="
        )  # the content screen's hold, not the metadata stage's
        wanted = (
            {"allow_documents", *(("allow_content_review",) if content_hold else ())}
            if allow
            else {"deny_documents"}
        )
        for name in DECISION_KEYS:  # a later decision on a document replaces the earlier one
            present = key in lists[name]
            if name in wanted and not present:
                lists[name].append(key)
            elif name not in wanted and present:
                lists[name].remove(key)
        entry = {"key": key, "decision": "allow" if allow else "deny", "rule": rule, "at": when}
        log.append({**entry, **({"proposal": proposal} if proposal is not None else {})})
    body: dict[str, Any] = {k: sorted(v) for k, v in lists.items()}
    body["log"] = log
    path.parent.mkdir(parents=True, exist_ok=True)
    header = DECISIONS_HEADER.format(
        source=source_id,
        rules=rules_file.relative_to(repo_root).as_posix()
        if rules_file.is_relative_to(repo_root)
        else str(rules_file),
    )
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(
        header + yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8"
    )
    tmp.replace(path)
    return ReviewDecision(path, tuple(chosen))


def cmd_fetch(args: argparse.Namespace, out: TextIO) -> int:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    specs = {s.id: s for s in bootstrap.load_sources(cfg, lay)}
    spec = specs.get(args.source)
    if spec is None:
        raise bootstrap.ConfigError(f"unknown source {args.source!r}")
    if spec.status is SourceStatus.EXCLUDED or spec.raw.get("legal_basis") == "none":
        print(f"error: {spec.id} is excluded or has no legal basis", file=sys.stderr)
        return EXIT_REFUSED
    if spec.status is SourceStatus.PAUSED:
        # What is held stays served; nothing new is fetched until the owner says (army-pubs-public:
        # its robots.txt disallows every crawler, 2026-10-21).
        print(f"error: {spec.id} is paused (see its registry entry)", file=sys.stderr)
        return EXIT_REFUSED
    if spec.partitioned and not args.partition and not args.dry_run:
        print(f"error: {spec.id} is fetched one partition per run: add --partition", file=sys.stderr)
        return EXIT_REFUSED
    if args.partition and not spec.partitioned:
        print(f"error: {spec.id} is not partitioned (access.partitioned): drop --partition", file=sys.stderr)
        return EXIT_REFUSED
    try:
        partition = check_partition(args.partition.lower()) if args.partition else None
    except PartitionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    paths = bootstrap.paths(cfg)
    vault_root = paths.require_vault()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    cycle = args.version or today
    version = partition_generation_id(cycle, partition) if partition else cycle
    workdir = (paths.workdir or state_dir()) / "collections"
    runner = CollectionFetcher(
        vault_root,
        # Downloads land on the NAS (_incoming): the library's shared, reader-yielding write budget.
        bootstrap.build_fetcher(cfg, pace=bootstrap.ingest_pace(cfg)),
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
        refetch=frozenset(getattr(args, "refetch", None) or ()),
    )
    if report.pending:
        out.write(
            f"{spec.id}: fetched but not ingested yet: {', '.join(report.pending)}; "
            f"run `wsp ingest --source {spec.id}` first (nothing fetched)\n"
        )
        return EXIT_INGEST_PENDING
    if report.version != version:
        out.write(f"{spec.id}: {version} is already sealed; fetching as {report.version}\n")
        version = report.version
    if report.incomplete:
        out.write(f"{spec.id}/{version}: the listing is incomplete; nothing fetched (try again later):\n")
        for why in report.incomplete[:20]:
            out.write(f"  {why}\n")
        return EXIT_LISTING_INCOMPLETE
    rows = (
        [*report.review, *report.held]
        if args.dry_run
        else unproposed_rows(cfg, spec.id, [*report.review, *report.held])
    )
    proposal = None if args.dry_run else write_review_proposal(cfg, spec, version, rows)
    out.write(
        f"{spec.id}/{version}: fetched {len(report.fetched)}, skipped {report.skipped}, "
        f"rejected {len(report.rejected)}, held {len(report.held)}, review {len(report.review)}, "
        f"failed {len(report.failed)}\n"
    )
    for alert in report.alerts:
        out.write(f"ALERT restricted document listed by the issuer: {alert}\n")
    for key, why in report.rejected:  # every exclusion is written down with its rule
        out.write(f"rejected: {key}: {why}\n")
    for key, why in report.failed[:20]:
        out.write(f"failed: {key}: {why}\n")
    if proposal is not None:
        out.write(f"review proposal: {proposal} (wsp proposals list)\n")
    elif rows and not args.dry_run:
        side = state_dir() / f"review-{spec.id}-{version}.json"
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        out.write(f"review list (no orchestrator state DB for proposals): {side}\n")
    if args.dry_run:
        return EXIT_COMPLETE
    code = fetch_exit_code(report)
    outcome = {
        EXIT_COMPLETE: "complete",
        EXIT_PARTIAL: "partial (fetch again later for the failed documents)",
        EXIT_ALERT: "complete, with alerts",
        EXIT_NOTHING_NEW: "complete, nothing new",
    }[code]
    out.write(f"outcome: {outcome} (exit {code})\n")
    if report.planned:
        out.write(f"next: wsp ingest --source {spec.id} --version {version}\n")
    return code


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
        "fetch",
        cmd_fetch,
        "Fetch one collection politely into <vault>/_incoming (then: wsp ingest --source S --version V). "
        "Exit codes: 0 complete (plan written); 1 crashed; 2 refused; 4 listing incomplete, nothing "
        "fetched (retry the same cycle later); 6 partial, some downloads failed (ingest what came, "
        "fetch again later); 7 complete with alerts (restricted documents listed, deleted); 8 complete, "
        "nothing new (no plan). Only 0, 7 and 8 are complete.",
        co_sub,
    )
    s.add_argument("--source", required=True)
    s.add_argument("--version", default=None, help="cycle label (default: today, YYYY-MM-DD)")
    s.add_argument("--limit", type=int, default=None, help="at most this many documents")
    s.add_argument("--max-pages", type=int, default=None, help="listing pages per discoverer")
    s.add_argument("--dry-run", action="store_true", help="discover and classify only")
    s.add_argument(
        "--refetch",
        action="append",
        metavar="KEY",
        help="download this document key again even if the held copy looks current (repeatable)",
    )
    s.add_argument(
        "--partition",
        default=None,
        help="partitioned sets: one state (usgs-historical-topo) or batch b0001 (cycle <date>.<partition>)",
    )


def _usage(_: argparse.Namespace, __: TextIO) -> int:  # pragma: no cover - argparse requires a subcommand
    return 2


__all__ = ["TOP_PICKS", "dry_run", "register", "render_dry_run"]
