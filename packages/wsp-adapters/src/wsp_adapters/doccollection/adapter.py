"""``doc-collection`` adapter: curated document sets with per-document policy (PLAN §6.2, D11, D13).

Registry configuration (``access``; see research doc §5.2 and ``docs/components/adapters.md``)::

    access:
      adapter: doc-collection
      issuer: Department of the Army          # written into every document record
      key_prefix: army                         # document keys: army:FM-5-0:2022-11-04
      origins: [armypubs.army.mil]             # HTTPS hosts allowed to serve document bytes
      discovery: [{kind: index-page, ...}]     # see wsp_adapters.doccollection.discovery
      filter: {rules: registry/filters/army-pubs-public.yaml, distribution: {require: [A]}, default: review}
      currency: {collection: historical} | {historical_before: "1990-01-01"}
      rate: {requests_per_second: 1, per_host: {www.cia.gov: 10}}
      mirrors: [{channel: ..., verified: 2026-10-10, ...}]   # D11 provenance notes (not used here)

**Generations.** A cycle's generation holds the documents that are new or changed in that cycle
under ``upstream/`` plus ``meta/documents.jsonl``: the *whole* collection as of the cycle, one line
per document version, with ``generation`` naming the generation that holds the bytes when it is an
earlier one. Superseded editions therefore stay where they were sealed and remain reachable;
:meth:`documents` reads the catalog of the generation it is given and resolves files across the
source's generations.

**Policy.** :meth:`discover` applies the metadata stage of :mod:`~wsp_adapters.doccollection.policy`
(denied documents are never fetched; review documents are left out of the release) and writes an
audit file of every decision to ``ctx.workdir``. :meth:`verify` runs the distribution gate and the
content screen on each fetched file's front matter. In the orchestrator pipeline a rejected or held
document fails the release (a ``mismatch`` verdict, which also auto-pauses the source and alerts);
``wsp collections fetch`` is the acquisition path that deletes restricted files, quarantines held
ones and files proposals per document.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from wsp_adapters._common import access, upstream_dir, verify_file
from wsp_adapters.doccollection.discovery import (
    DiscoverEnv,
    Throttle,
    discover_all,
    origin_ok,
    resolve,
)
from wsp_adapters.doccollection.model import Candidate, DocumentMeta, title_from_url
from wsp_adapters.doccollection.policy import (
    CurrencyRules,
    Decision,
    Marking,
    Rules,
    Stage,
    Verdict,
    check_distribution,
    classify_content,
    classify_metadata,
    load_rules,
    parse_markings,
)
from wsp_adapters.html_text import html_to_text
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import (
    Checksum,
    DocRecord,
    FetchPlan,
    FetchResult,
    Generation,
    ReleaseRef,
    RemoteFile,
    SourceSpec,
    VerdictStatus,
)
from wsp_core.model import (
    Verdict as FileVerdict,
)
from wsp_core.ports import Ctx

log = logging.getLogger(__name__)

CATALOG = "meta/documents.jsonl"
FORMAT = "doc-collection/1"
FRONT_MATTER_PAGES = 4
DEFAULT_PDF_MAX_PAGES = 400
REPO_ROOT = Path(__file__).resolve().parents[5]


@dataclass(frozen=True, slots=True)
class CollectionConfig:
    issuer: str
    prefix: str
    origins: tuple[str, ...]
    discovery: tuple[Mapping[str, Any], ...]
    rules: Rules
    currency: CurrencyRules
    interval: float = 1.0
    per_host: Mapping[str, float] = field(default_factory=dict[str, float])
    pdf_max_pages: int = DEFAULT_PDF_MAX_PAGES
    keep_superseded: bool = True

    @staticmethod
    def from_spec(spec: SourceSpec, repo_root: Path = REPO_ROOT) -> CollectionConfig:
        acc = access(spec)
        rate = cast(Mapping[str, Any], acc.get("rate") or {})
        rps = float(rate.get("requests_per_second", 1.0) or 1.0)
        discovery = cast(list[Mapping[str, Any]], acc.get("discovery") or [])
        origins: object = acc.get("origins") or []
        return CollectionConfig(
            issuer=str(acc.get("issuer") or spec.raw.get("publisher") or spec.name),
            prefix=str(acc.get("key_prefix") or spec.id),
            origins=tuple(str(o).lower() for o in cast(list[object], origins)),
            discovery=tuple(discovery),
            rules=load_rules(cast(Mapping[str, Any], acc.get("filter") or {}), repo_root=repo_root),
            currency=CurrencyRules.from_access(acc),
            interval=max(1.0, 1.0 / rps),  # never faster than 1 request/s per host
            per_host={
                str(k).lower(): float(v)
                for k, v in cast(Mapping[str, Any], rate.get("per_host") or {}).items()
            },
            pdf_max_pages=int(str(acc.get("pdf_max_pages", DEFAULT_PDF_MAX_PAGES))),
            keep_superseded=bool(acc.get("keep_superseded", True)),
        )


@dataclass
class Survey:
    """Everything one discovery pass learned (the dry run prints it; :meth:`discover` keeps the allowed)."""

    source_id: str
    decisions: list[tuple[Candidate, Decision]] = field(default_factory=list[tuple[Candidate, Decision]])
    allowed: list[Candidate] = field(default_factory=list[Candidate])  # resolved to document URLs
    unresolved: int = 0  # allowed candidates whose landing page was not (or could not be) resolved
    totals: dict[str, int] = field(default_factory=dict[str, int])
    scale: float = 1.0  # > 1 when an index crawl stopped early (sample)
    notes: list[str] = field(default_factory=list[str])
    requests: dict[str, int] = field(default_factory=dict[str, int])

    def count(self, verdict: Verdict) -> int:
        return sum(d.verdict is verdict for _c, d in self.decisions)


@dataclass(frozen=True, slots=True)
class Screen:
    """Outcome of the front-matter checks on one fetched file."""

    decision: Decision
    marking: Marking
    text_found: bool


class DocCollectionAdapter:
    """``SourceAdapter`` for curated document collections."""

    id = "doc-collection"

    def __init__(
        self,
        *,
        keys: Mapping[str, str] | None = None,
        repo_root: Path = REPO_ROOT,
        allow_demo_key: bool = False,
        throttle: bool = True,
    ) -> None:
        self.keys = dict(keys or {})
        self.repo_root = repo_root
        self.allow_demo_key = allow_demo_key
        self.throttle = throttle

    def config(self, spec: SourceSpec) -> CollectionConfig:
        return CollectionConfig.from_spec(spec, self.repo_root)

    def env(self, ctx: Ctx, *, max_pages: int | None = None, partition: str | None = None) -> DiscoverEnv:
        cfg = self.config(ctx.source)
        fetcher = (
            Throttle(ctx.fetcher, interval=cfg.interval, per_host=cfg.per_host)
            if self.throttle
            else ctx.fetcher
        )
        return DiscoverEnv(
            fetcher=fetcher,
            issuer=cfg.issuer,
            prefix=cfg.prefix,
            origins=cfg.origins,
            keys=self.keys,
            rules=cfg.rules,
            max_pages=max_pages,
            partition=partition,
            allow_demo_key=self.allow_demo_key,
        )

    # --- discovery ---------------------------------------------------------------------------------

    def survey(
        self,
        ctx: Ctx,
        *,
        max_pages: int | None = None,
        resolve_limit: int | None = None,
        env: DiscoverEnv | None = None,
    ) -> Survey:
        """Discover, classify (metadata stage) and resolve allowed candidates to document URLs."""
        cfg = self.config(ctx.source)
        env = env or self.env(ctx, max_pages=max_pages)
        out = Survey(ctx.source.id)
        resolved = 0
        for cand in discover_all(cfg.discovery, env):
            decision = classify_metadata(cand.facts, cfg.rules)
            out.decisions.append((cand, decision))
            if decision.verdict is not Verdict.ALLOW:
                continue
            if (
                cand.resolve
                and cand.resolve != "git"
                and resolve_limit is not None
                and resolved >= resolve_limit
            ):
                out.unresolved += 1
                continue
            try:
                docs = resolve(cand, env)
            except Exception as exc:
                out.notes.append(f"resolve {cand.key}: {type(exc).__name__}: {exc}")
                out.unresolved += 1
                continue
            resolved += 1 if cand.resolve else 0
            out.allowed.extend(d for d in docs if origin_ok(d.url, cfg.origins) or d.resolve == "git")
        out.totals = dict(env.totals)
        out.scale = max(env.scale.values(), default=1.0)
        out.notes.extend(env.notes)
        if isinstance(env.fetcher, Throttle):
            out.requests = dict(env.fetcher.requests)
        return out

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        survey = self.survey(ctx)
        version = datetime.now(UTC).strftime("%Y-%m-%d")
        write_audit(ctx.workdir / "doc-collection" / version / "candidates.jsonl", survey.decisions)
        files: list[RemoteFile] = []
        names: set[str] = set()
        for cand in survey.allowed:
            if cand.resolve == "git" or cand.file_name in names:
                continue  # git repositories are bundled by `wsp collections fetch`
            names.add(cand.file_name)
            files.append(remote_for(cand))
        if not files:
            return []
        digest = hashlib.sha256("\n".join(sorted(f.url for f in files)).encode()).hexdigest()
        meta = {
            "candidates": str(len(survey.decisions)),
            "allowed": str(survey.count(Verdict.ALLOW)),
            "denied": str(survey.count(Verdict.DENY)),
            "review": str(survey.count(Verdict.REVIEW)),
            "candidates_sha256": digest,
        }
        return [
            ReleaseRef(
                source_id=ctx.source.id,
                version=version,
                published=None,
                files=tuple(files),
                meta=meta,
                format=FORMAT,
            )
        ]

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        """Fetch documents the previous generation does not already hold (same name and size)."""
        held: dict[str, int] = {}
        if have is not None:
            held = {a.relpath.name: a.size for a in have.artifacts}
        needed = [
            f for f in want.files if f.name not in held or (f.size is not None and f.size != held[f.name])
        ]
        return FetchPlan(
            release=want,
            files=tuple(needed),
            previous_generation=have.generation_id if have is not None else None,
        )

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        cfg = self.config(ctx.source)
        fetcher = (
            Throttle(ctx.fetcher, interval=cfg.interval, per_host=cfg.per_host)
            if self.throttle
            else ctx.fetcher
        )
        ctx.workdir.mkdir(parents=True, exist_ok=True)
        for remote in plan.files:
            for url in (remote.url, *remote.mirrors):
                if not origin_ok(url, cfg.origins):
                    raise ValueError(f"{ctx.source.id}: {url} is not an allowed HTTPS origin {cfg.origins}")
            yield fetcher.download(remote, ctx.workdir / remote.name)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> FileVerdict:
        base = verify_file(remote, local)
        if base.status in (VerdictStatus.MISSING, VerdictStatus.MISMATCH):
            return base
        problem = well_formed(local)
        if problem:
            return FileVerdict(VerdictStatus.MISMATCH, problem)
        screen = screen_file(local, self.config(ctx.source).rules)
        if screen.decision.verdict is not Verdict.ALLOW:
            return FileVerdict(VerdictStatus.MISMATCH, f"policy {screen.decision}")
        return base

    # --- documents ---------------------------------------------------------------------------------

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        spec = ctx.source
        cfg = self.config(spec)
        for meta in read_catalog(generation_dir):
            path = resolve_file(generation_dir, meta)
            if path is None:
                log.warning("%s: %s not found in any generation", spec.id, meta.file)
                continue
            body = document_text(path, max_pages=cfg.pdf_max_pages)
            text = catalog_header(meta) + ("\n\n" + body if body else "")
            license_ = meta.license or spec.license
            yield DocRecord(
                doc_id=make_doc_id(spec.id, meta.doc_key),
                source_id=spec.id,
                native_id=meta.doc_key,
                title=meta.title or meta.pub_number or meta.doc_key,
                lang=meta.language or "en",
                license=license_,
                text=text,
                revision=meta.edition_date or None,
                url_native=meta.origin_url or None,
                html_ref=f"file:{meta.generation or generation_dir.name}/{meta.file}",
                content_hash=content_hash(text),
                aliases=tuple(a for a in (meta.pub_number,) if a and a != meta.title),
                meta=meta.record_meta(),
            )


# --- helpers ---------------------------------------------------------------------------------------


def remote_for(cand: Candidate) -> RemoteFile:
    checksums: list[Checksum] = []
    if cand.sha256:
        checksums.append(Checksum("sha256", cand.sha256))
    if cand.md5:
        checksums.append(Checksum("md5", cand.md5))
    return RemoteFile(
        url=cand.url, name=cand.file_name, size=cand.size, checksums=tuple(checksums), mirrors=cand.mirrors
    )


def write_audit(path: Path, decisions: Sequence[tuple[Candidate, Decision]]) -> None:
    """Every candidate with its decision (local disk): why each document is or is not in the library."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    with tmp.open("w", encoding="utf-8") as fh:
        for cand, decision in decisions:
            row = {**cand.to_json(), "decision": str(decision), "stage": decision.stage.value}
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    tmp.replace(path)


def well_formed(path: Path) -> str | None:
    """A cheap format check: PDFs must start with ``%PDF-``; HTML must look like HTML."""
    with path.open("rb") as fh:
        head = fh.read(1024)
    name = path.name.lower()
    if name.endswith(".pdf") and not head.lstrip().startswith(b"%PDF-"):
        return "not a PDF (missing %PDF- header)"
    if name.endswith((".html", ".htm")) and b"<" not in head:
        return "not HTML"
    return None


def pdf_text(path: Path, max_pages: int) -> str:
    """Text of the first ``max_pages`` pages (pypdfium2, one page in memory at a time)."""
    import pypdfium2  # imported on use: only PDF documents need it

    pdf = pypdfium2.PdfDocument(str(path))
    try:
        out: list[str] = []
        for i in range(min(len(pdf), max_pages)):
            page = pdf[i]
            try:
                textpage = page.get_textpage()
                out.append(textpage.get_text_range())
                textpage.close()
            finally:
                page.close()
        return "\n".join(out)
    finally:
        pdf.close()


def document_text(path: Path, *, max_pages: int) -> str:
    with path.open("rb") as fh:
        head = fh.read(512)
    try:
        if head.lstrip().startswith(b"%PDF-"):
            return pdf_text(path, max_pages)
        lowered = head.lower()
        if b"<html" in lowered or b"<!doctype html" in lowered or path.suffix.lower() in (".html", ".htm"):
            return html_to_text(path.read_bytes()[: 16 * 1024 * 1024])
        if path.suffix.lower() in (".txt", ".xml", ".json", ".md"):
            return path.read_bytes()[: 16 * 1024 * 1024].decode("utf-8", "replace")
    except Exception as exc:
        log.warning("text extraction failed for %s: %s", path.name, exc)
    return ""


def front_matter(path: Path, pages: int = FRONT_MATTER_PAGES) -> str:
    """Title page, contents and preface: the only part of a document the gate reads."""
    return document_text(path, max_pages=pages)[:60_000]


def screen_file(path: Path, rules: Rules) -> Screen:
    """Distribution gate, then content screen, on a fetched file's front matter (research doc §5.3)."""
    text = front_matter(path)
    marking = parse_markings(text)
    gate = check_distribution(marking, rules.require, declassified_ok=rules.declassified_ok)
    if gate.verdict is Verdict.DENY:
        return Screen(gate, marking, bool(text.strip()))
    content = classify_content(text, rules)
    if content.verdict is Verdict.DENY:
        return Screen(content, marking, bool(text.strip()))
    if gate.verdict is Verdict.REVIEW:
        return Screen(gate, marking, bool(text.strip()))
    if content.verdict is Verdict.REVIEW:
        return Screen(content, marking, bool(text.strip()))
    return Screen(Decision(Verdict.ALLOW, gate.rule, Stage.DISTRIBUTION), marking, bool(text.strip()))


def read_catalog(generation_dir: Path) -> Iterator[DocumentMeta]:
    """``meta/documents.jsonl`` if present, else one record per file under ``upstream/``."""
    catalog = generation_dir / CATALOG
    if catalog.is_file():
        with catalog.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield DocumentMeta.from_line(line)
        return
    up = upstream_dir(generation_dir)
    for path in sorted(p for p in up.rglob("*") if p.is_file()):
        rel = path.relative_to(generation_dir).as_posix()
        stem = path.stem.rsplit("-", 1)[0]
        yield DocumentMeta(
            doc_key=stem.replace("__", ":"), file=rel, issuer="", title=title_from_url(path.name)
        )


def resolve_file(generation_dir: Path, meta: DocumentMeta) -> Path | None:
    """The document's bytes: in this generation, or in the earlier one the catalog names."""
    gen = meta.generation or generation_dir.name
    for root in (generation_dir.parent / gen, generation_dir):
        path = root / meta.file
        if path.is_file():
            return path
    return None


def catalog_header(meta: DocumentMeta) -> str:
    """A short, human-readable header so search and readers see what the document is."""
    lines = [meta.title or meta.doc_key]
    facts = [
        f"Issuer: {meta.issuer}" if meta.issuer else "",
        f"Publication: {meta.pub_number}" if meta.pub_number else "",
        f"Edition: {meta.edition_date}" if meta.edition_date else "",
        f"Status: {meta.currency}" if meta.currency and meta.currency != "current" else "",
    ]
    lines += [f for f in facts if f]
    if meta.banner:
        lines.append(meta.banner)
    return "\n".join(lines)
