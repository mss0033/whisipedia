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
source's generations, and :meth:`depends_on` names those earlier generations so retention keeps them.

**Partitions** (ADR-0010, ``partitioned: true``): very large sets are fetched one partition per run
(a US state, a batch of 10,000 items) into generations ``<cycle>.<partition>``. A partition's catalog
lists that partition only and carries over the partition's own earlier generation; the index serves
the union of every partition's current generation.

**Policy.** :meth:`discover` applies the metadata stage of :mod:`~wsp_adapters.doccollection.policy`
(denied documents are never fetched; review documents are left out of the release) and writes an
audit file of every decision to ``ctx.workdir``. :meth:`verify` runs the distribution gate and the
content screen on each fetched file's front matter. A rejected document gets a ``rejected`` verdict
and a held one a ``held`` verdict: the orchestrator pipeline drops that file (deletes it, or moves it
to its quarantine), alerts, files a review proposal and goes on with the rest of the release
(ADR-0010). ``wsp collections fetch`` does the same per document on the acquisition path. A file that
is not what it claims to be (an HTML error page instead of a PDF) is still a ``mismatch``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, cast

from wsp_adapters._common import access, upstream_dir, verify_file
from wsp_adapters.doccollection.discovery import (
    DiscoverEnv,
    Throttle,
    discover_all,
    off_origin_hops,
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
    normalize_pub_number,
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
from wsp_core.ports import Ctx, Fetcher

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
            yield download_from_origins(fetcher, remote, ctx.workdir / remote.name, cfg.origins)

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> FileVerdict:
        """Checksum and size (:func:`verify_file`), then the document-policy :meth:`screen`."""
        base = verify_file(remote, local)
        if base.status in (VerdictStatus.MISSING, VerdictStatus.MISMATCH):
            return base
        screened = self.screen(ctx, remote, local)
        return base if screened.status is VerdictStatus.OK else screened

    def screen(self, ctx: Ctx, remote: RemoteFile, local: Path) -> FileVerdict:
        """The document-policy screen alone (``DocumentScreen``; no checksum work).

        ``mismatch`` when the file is not what it claims (an HTML page named ``.pdf``), ``rejected``
        when a marking or the safety list denies it, ``held`` when it needs the owner (including a
        file with no text to screen), else ``ok``.
        """
        if not local.is_file():
            return FileVerdict(VerdictStatus.MISSING, f"{local.name} not found")
        problem = well_formed(local)
        if problem:
            return FileVerdict(VerdictStatus.MISMATCH, problem)
        screen = screen_file(local, self.config(ctx.source).rules, keys=(key_of_file_name(remote.name),))
        if screen.decision.verdict is Verdict.DENY:
            return FileVerdict(VerdictStatus.REJECTED, f"policy {screen.decision}")
        if screen.decision.verdict is Verdict.REVIEW:
            return FileVerdict(VerdictStatus.HELD, f"policy {screen.decision}")
        return FileVerdict(VerdictStatus.OK, f"policy {screen.decision}")

    def depends_on(self, ctx: Ctx, generation_dir: Path) -> frozenset[str]:
        """Earlier generations whose files this generation's catalog lists (``GenerationDependencies``)."""
        catalog = generation_dir / CATALOG
        if not catalog.is_file():
            return frozenset()
        return frozenset(m.generation for m in read_catalog(generation_dir) if m.generation) - {
            generation_dir.name
        }

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


def key_of_file_name(name: str) -> str:
    """The document key a stored file name was made from (:attr:`Candidate.file_name`; best effort:
    characters outside ``[A-Za-z0-9._-]`` were flattened to ``_``)."""
    stem = name.rsplit(".", 1)[0]
    return stem.rsplit("-", 1)[0].replace("__", ":")


class OriginError(ValueError):
    """A document URL, mirror, redirect hop or final URL is not an allowed HTTPS origin."""


def download_from_origins(
    fetcher: Fetcher, remote: RemoteFile, dest: Path, origins: Sequence[str]
) -> FetchResult:
    """``fetcher.download`` restricted to ``origins`` on every URL the download touched.

    The configured URL and mirrors are checked before the request; every redirect hop and final URL
    the fetcher recorded (``FetchReport.provenance``) after it. Off-origin or unverifiable bytes are
    deleted at once and :class:`OriginError` is raised. (The fetcher follows redirects itself, so the
    bytes do reach local disk first; they never reach the vault.)
    """
    for url in (remote.url, *remote.mirrors):
        if not origin_ok(url, origins):
            raise OriginError(f"{url} is not an allowed HTTPS origin {tuple(origins)}")
    result = fetcher.download(remote, dest)
    bad = off_origin_hops(result, origins)
    if bad is None or bad:
        Path(result.path).unlink(missing_ok=True)
        why = "no record of redirect hops" if bad is None else f"redirected to {', '.join(bad[:3])}"
        raise OriginError(f"{remote.url}: {why}; allowed origins {tuple(origins)}")
    return result


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


class Extracted(NamedTuple):
    """Text of a document, or why there is none (``problem``: see :data:`NO_TEXT`)."""

    text: str
    problem: str = ""  # "" | "unsupported-format" | "extraction-error" | "no-text-layer"


# Fewer letters than this in the front matter is no text layer: a scan, or only page numbers.
MIN_FRONT_MATTER_LETTERS = 64
_LETTER_RE = re.compile(r"[^\W\d_]")
TEXT_SUFFIXES = (".txt", ".xml", ".json", ".md")


def extract_text(path: Path, *, max_pages: int) -> Extracted:
    with path.open("rb") as fh:
        head = fh.read(512)
    lowered = head.lower()
    suffix = path.suffix.lower()
    try:
        if head.lstrip().startswith(b"%PDF-"):
            return Extracted(pdf_text(path, max_pages))
        if b"<html" in lowered or b"<!doctype html" in lowered or suffix in (".html", ".htm"):
            return Extracted(html_to_text(path.read_bytes()[: 16 * 1024 * 1024]))
        if suffix in TEXT_SUFFIXES:
            return Extracted(path.read_bytes()[: 16 * 1024 * 1024].decode("utf-8", "replace"))
    except Exception as exc:
        log.warning("text extraction failed for %s: %s", path.name, exc)
        return Extracted("", "extraction-error")
    return Extracted("", "unsupported-format")


def document_text(path: Path, *, max_pages: int) -> str:
    return extract_text(path, max_pages=max_pages).text


def front_matter(path: Path, pages: int = FRONT_MATTER_PAGES) -> Extracted:
    """Title page, contents and preface: the only part of a document the gate reads."""
    got = extract_text(path, max_pages=pages)
    return checked_front_matter(got.text, got.problem)


def checked_front_matter(text: str, problem: str = "") -> Extracted:
    """At most 60,000 characters; too few letters to screen anything is ``no-text-layer``."""
    text = text[:60_000]
    if not problem and len(_LETTER_RE.findall(text)) < MIN_FRONT_MATTER_LETTERS:
        return Extracted(text, "no-text-layer")
    return Extracted(text, problem)


def screen_file(path: Path, rules: Rules, *, keys: Iterable[str] = ()) -> Screen:
    """Distribution gate, then content screen, on a fetched file's front matter (research doc §5.3).

    Fails closed: a file whose front matter cannot be read (unsupported format, extraction error,
    no text layer) is held for review, because neither screen saw anything. A scanned PDF passes
    that hold only where the registry sets ``filter.allow_image_only``. ``keys`` (document key and
    publication number) let an owner ``allow_documents`` decision release a document from the two
    holds that are about missing evidence (no text, a required marking not found); it never
    overrides a deny or a content review.
    """
    return screen_text(front_matter(path), rules, keys=keys)


def screen_text(got: Extracted, rules: Rules, *, keys: Iterable[str] = ()) -> Screen:
    """:func:`screen_file` on already extracted front matter (also used for git bundles)."""
    text_found = not got.problem
    # allow_documents holds normalized numbers and keys (Rules.merged), so normalize to compare.
    owner_allowed = bool({normalize_pub_number(k) for k in keys if k} & rules.allow_documents)
    marking = parse_markings(got.text)
    gate = check_distribution(marking, rules.require, declassified_ok=rules.declassified_ok)
    if gate.verdict is Verdict.DENY:
        return Screen(gate, marking, text_found)
    content = classify_content(got.text, rules)
    if content.verdict is Verdict.DENY:
        return Screen(content, marking, text_found)
    if gate.verdict is Verdict.REVIEW and not owner_allowed:
        return Screen(gate, marking, text_found)
    image_only_ok = got.problem == "no-text-layer" and rules.allow_image_only
    if not text_found and not owner_allowed and not image_only_ok:
        return Screen(Decision(Verdict.REVIEW, f"text={got.problem}", Stage.CONTENT), marking, text_found)
    if content.verdict is Verdict.REVIEW:
        return Screen(content, marking, text_found)
    rule = gate.rule
    if gate.verdict is Verdict.REVIEW:
        rule = f"owner=allow+{gate.rule}"
    if not text_found:
        rule += f"+text={got.problem}" + ("" if image_only_ok else "+owner=allow")
    return Screen(Decision(Verdict.ALLOW, rule, Stage.DISTRIBUTION), marking, text_found)


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
