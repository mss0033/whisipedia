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
import sqlite3
import time
import zipfile
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, cast
from urllib.parse import unquote, urlsplit

from wsp_adapters._common import access, upstream_dir, verify_file
from wsp_adapters.doccollection import structured, tei
from wsp_adapters.doccollection.catalog import FetchedDoc, build_catalog, excluded_by, write_catalog
from wsp_adapters.doccollection.discovery import (
    DiscoverEnv,
    NoDocuments,
    Throttle,
    discover_all,
    off_origin_hops,
    origin_ok,
    resolve,
)
from wsp_adapters.doccollection.model import (
    Candidate,
    DocumentMeta,
    clean_title,
    display_title,
    is_filename_title,
    is_fragment,
    title_from_url,
)
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
from wsp_adapters.pdftext import Section, outline_marks, page_text, plan_sections
from wsp_core.banners import banner_for
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.memory import drop_file_cache
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
    # ``access.index_text``: false indexes each document by its catalog record alone, never reading the
    # file. A scanned map's "text" is stray labels, and reading 60,000 map PDFs over the NAS held the
    # heavy lock for days (USGS topo, 2026-10-19); the sheet is found by its name, state, year, scale.
    index_text: bool = True
    keep_superseded: bool = True
    # Requests in flight at once (``rate.concurrency``, 1-8). Starts stay ``interval`` apart per host:
    # this only stops a 2-3 s round trip from cutting a 1 request/s budget to 0.3/s.
    concurrency: int = 1
    # File formats the source takes (``access.formats``, extensions such as ``[pdf]``); empty: any.
    # Documents in other formats are not fetched, and stored ones leave the index (CA-11).
    formats: tuple[str, ...] = ()
    decode_url_keys: bool = False  # access.decode_url_keys (see DiscoverEnv)
    # ``access.reader``: how a repository snapshot's files become documents (wsp_adapters.doccollection.tei)
    reader: str = ""

    @staticmethod
    def from_spec(spec: SourceSpec, repo_root: Path = REPO_ROOT) -> CollectionConfig:
        acc = access(spec)
        rate = cast(Mapping[str, Any], acc.get("rate") or {})
        rps = float(rate.get("requests_per_second", 1.0) or 1.0)
        discovery = cast(list[Mapping[str, Any]], acc.get("discovery") or [])
        origins: object = acc.get("origins") or []
        formats: object = acc.get("formats") or []
        if not isinstance(formats, list):
            raise ValueError(f"{spec.id}: access.formats must be a list of file extensions, got {formats!r}")
        reader = str(acc.get("reader") or "")
        known = {**tei.READERS, **structured.READERS, **structured.ARCHIVE_READERS}
        if reader and reader not in known:
            raise ValueError(f"{spec.id}: access.reader must be one of {sorted(known)}, got {reader!r}")
        return CollectionConfig(
            reader=reader,
            formats=tuple(str(f).lower().lstrip(".") for f in cast(list[object], formats)),
            decode_url_keys=bool(acc.get("decode_url_keys", False)),
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
            index_text=bool(acc.get("index_text", True)),
            concurrency=min(8, max(1, int(str(rate.get("concurrency", 1))))),
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
    incomplete: list[str] = field(default_factory=list[str])  # see DiscoverEnv.incomplete; + failed resolves
    sampled: list[str] = field(default_factory=list[str])  # see DiscoverEnv.sampled (--max-pages)
    # Listing keys of allowed items whose resolve failed or was skipped: their documents' keys are
    # unknown this run, so none of them may be inferred withdrawn (CA-10).
    unresolved_keys: list[str] = field(default_factory=list[str])
    skipped_formats: int = 0  # resolved documents in a format outside ``access.formats``

    def count(self, verdict: Verdict) -> int:
        return sum(d.verdict is verdict for _c, d in self.decisions)

    @property
    def listing_complete(self) -> bool:
        """Every page of every listing was read: absence from it means something."""
        return not self.incomplete and not self.sampled

    def protected_keys(self) -> frozenset[str]:
        """Listing keys whose documents must not be inferred withdrawn this run: items not resolved,
        and items listed but not allowed (held for review or denied: still listed by the issuer)."""
        held = {c.key for c, d in self.decisions if d.verdict is not Verdict.ALLOW}
        return frozenset(self.unresolved_keys) | held


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
            cache_dir=ctx.workdir,
            decode_url_keys=cfg.decode_url_keys,
        )

    # --- discovery ---------------------------------------------------------------------------------

    def survey(
        self,
        ctx: Ctx,
        *,
        max_pages: int | None = None,
        resolve_limit: int | None = None,
        env: DiscoverEnv | None = None,
        progress: Callable[[str], None] | None = None,
        progress_every: int = 500,
    ) -> Survey:
        """Discover, classify (metadata stage) and resolve allowed candidates to document URLs.

        ``progress`` hears a line every ``progress_every`` resolved candidates: resolving is one request
        per item at the source's polite rate, so a large partition is silent for hours without it.

        A resolve that raises, or returns no document without saying so (:class:`NoDocuments`), leaves
        its item *unresolved*: noted, never cached, and its documents are never inferred withdrawn.
        """
        cfg = self.config(ctx.source)
        env = env or self.env(ctx, max_pages=max_pages)
        out = Survey(ctx.source.id)
        resolved = 0
        cache = ResolveCache.open(ctx.workdir)
        pool = (
            ThreadPoolExecutor(cfg.concurrency, thread_name_prefix="resolve") if cfg.concurrency > 1 else None
        )
        inflight: deque[tuple[Candidate, Future[list[Candidate]]]] = deque()

        def unresolved(cand: Candidate, why: str) -> None:
            out.notes.append(f"resolve {cand.key}: {why}")
            out.unresolved += 1
            out.unresolved_keys.append(cand.key)

        def accept(cand: Candidate, docs: list[Candidate], *, fresh: bool, none: str = "") -> None:
            nonlocal resolved
            if cand.resolve and cand.resolve != "git" and not docs and not none:
                unresolved(cand, "no document found (an empty answer is retried, never cached)")
                return
            if fresh and cache is not None and cand.resolve and cand.resolve != "git":
                cache.put(cand, docs, none=none)  # the cache is only touched from this thread
            if none:
                out.notes.append(f"resolve {cand.key}: no documents ({none})")
            resolved += 1 if cand.resolve else 0
            if progress is not None and cand.resolve and resolved % progress_every == 0:
                progress(f"{ctx.source.id}: {resolved:,} resolved, {len(out.decisions):,} listed so far")
            for d in docs:
                if not (origin_ok(d.url, cfg.origins) or d.resolve == "git"):
                    continue
                if (
                    cfg.formats
                    and d.resolve != "git"
                    and d.file_name.rsplit(".", 1)[-1].lower() not in cfg.formats
                ):
                    out.skipped_formats += 1  # access.formats: a format the source does not take
                    continue
                out.allowed.append(d)

        def settle(cand: Candidate, run: Callable[[], list[Candidate]]) -> None:
            try:
                docs = run()
            except NoDocuments as exc:
                accept(cand, [], fresh=True, none=str(exc))
            except Exception as exc:
                unresolved(cand, f"{type(exc).__name__}: {exc}")
            else:
                accept(cand, docs, fresh=True)

        def drain(keep: int) -> None:
            while len(inflight) > keep:
                cand, fut = inflight.popleft()
                settle(cand, fut.result)

        try:
            for cand in discover_all(cfg.discovery, env):
                decision = classify_metadata(cand.facts, cfg.rules)
                out.decisions.append((cand, decision))
                if decision.verdict is not Verdict.ALLOW:
                    continue
                if (
                    cand.resolve
                    and cand.resolve != "git"
                    and resolve_limit is not None
                    and resolved + len(inflight) >= resolve_limit
                ):
                    out.unresolved += 1
                    out.unresolved_keys.append(cand.key)
                    continue
                cached = cache.get(cand) if cache is not None and cand.resolve else None
                if cached is not None:
                    accept(cand, cached.docs, fresh=False, none=cached.none)
                elif pool is not None and cand.resolve:
                    inflight.append((cand, pool.submit(resolve, cand, env)))
                    drain(4 * cfg.concurrency)  # bounded: never more than a few dozen waiting
                else:
                    settle(cand, lambda cand=cand: resolve(cand, env))
            drain(0)
        finally:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
            if cache is not None:
                cache.close()  # an exception must not lose the resolves of the last minute
        out.totals = dict(env.totals)
        out.scale = max(env.scale.values(), default=1.0)
        out.notes.extend(env.notes)
        out.incomplete.extend(env.incomplete)
        out.sampled.extend(env.sampled)
        if out.skipped_formats:
            out.notes.append(
                f"{out.skipped_formats} document(s) in formats outside access.formats {cfg.formats}"
            )

        if isinstance(env.fetcher, Throttle):
            out.requests = dict(env.fetcher.requests)
        return out

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        """The release the issuer lists now (the orchestrator's path; ``wsp collections fetch`` is the
        other). Writes the audit of every decision and the *discovery record* next to it: the resolved
        candidates by file name and what the listing proved (:meth:`finalize` builds the catalog from
        it, :meth:`screen` reads publication numbers from it)."""
        survey = self.survey(ctx)
        version = datetime.now(UTC).strftime("%Y-%m-%d")
        record_dir = ctx.workdir / "doc-collection" / version
        write_audit(record_dir / "candidates.jsonl", survey.decisions)
        files: list[RemoteFile] = []
        names: set[str] = set()
        kept: list[Candidate] = []
        for cand in survey.allowed:
            if cand.resolve == "git" or cand.file_name in names:
                continue  # git repositories are bundled by `wsp collections fetch`
            names.add(cand.file_name)
            files.append(remote_for(cand))
            kept.append(cand)
        write_discovery_record(record_dir, kept, survey)
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
        file with no text to screen), else ``ok``. The same :func:`screen_document` as
        ``wsp collections fetch``, with the publication number from the discovery record, so owner
        decisions by number apply on both paths.
        """
        if not local.is_file():
            return FileVerdict(VerdictStatus.MISSING, f"{local.name} not found")
        cand = discovery_candidate(ctx.workdir, remote.name)
        keys = (cand.key, cand.pub_number) if cand is not None else (key_of_file_name(remote.name),)
        try:
            screen = screen_document(local, self.config(ctx.source).rules, keys=keys)
        except WrongBodyError as exc:
            return FileVerdict(VerdictStatus.MISMATCH, str(exc))
        if screen.decision.verdict is Verdict.DENY:
            return FileVerdict(VerdictStatus.REJECTED, f"policy {screen.decision}")
        if screen.decision.verdict is Verdict.REVIEW:
            return FileVerdict(VerdictStatus.HELD, f"policy {screen.decision}")
        return FileVerdict(VerdictStatus.OK, f"policy {screen.decision}")

    def finalize(
        self, ctx: Ctx, plan: FetchPlan, previous_dir: Path | None, kept: Mapping[str, Path]
    ) -> list[tuple[str, Path]]:
        """The generation's catalog, built as ``wsp collections fetch`` builds it (``GenerationFinalizer``;
        2026-10-12 audit, CA-7): the previous generation's catalog carried over (documents it and
        earlier generations hold stay listed, with their titles, editions and banners), this cycle's
        documents added, editions labelled, policy applied, and withdrawals inferred only from a
        complete listing. Raises when the discovery record is missing: a generation is never sealed
        without its catalog.
        """
        cfg = self.config(ctx.source)
        record = read_discovery_record(ctx.workdir / "doc-collection" / plan.release.version)
        if record is None:
            raise FileNotFoundError(
                f"{ctx.source.id} {plan.release.version}: no discovery record; discover again before sealing"
            )
        missing = sorted(name for name in kept if name not in record.candidates)
        if missing:
            raise ValueError(f"{ctx.source.id}: files not in the discovery record: {missing[:5]}")
        fetched = [
            fetched_doc(
                record.candidates[name],
                local,
                screen_document(
                    local, cfg.rules, keys=(record.candidates[name].key, record.candidates[name].pub_number)
                ),
                file=f"upstream/{name}",
            )
            for name, local in sorted(kept.items())
        ]
        previous = (
            list(read_catalog(previous_dir)) if previous_dir is not None and previous_dir.is_dir() else []
        )
        catalog = build_catalog(
            previous,
            plan.previous_generation,
            fetched,
            cfg.currency,
            listed=record.listed,
            protected=record.protected,
            listing={c.key: c for c in record.candidates.values()},
            policy=cfg.rules,
            formats=cfg.formats,
        )
        out = ctx.workdir / "doc-collection" / plan.release.version / "generation" / CATALOG
        write_catalog(out, catalog)
        return [(CATALOG, out)]

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
        """Every catalogued document that policy still admits (2026-10-12 audit, CA-8).

        A document the catalog marks ``excluded``, or that the source's *current* rules deny by its
        metadata (an owner deny, a deny-core title, a marking in the title, a format the source no
        longer takes), is left out, so the index run of this generation removes it; the reader never
        sees it again. Documents the issuer stopped listing stay, with a banner (``banner_for``).
        """
        spec = ctx.source
        cfg = self.config(spec)
        for meta in read_catalog(generation_dir):
            why = meta.excluded or excluded_by(meta, cfg.rules, cfg.formats)
            if why:
                log.info("%s: %s left out of the index (%s)", spec.id, meta.doc_key, why)
                continue
            path = resolve_file(generation_dir, meta)
            if path is None:
                log.warning("%s: %s not found in any generation", spec.id, meta.file)
                continue
            if cfg.reader and path.suffix.lower() in (".zip", ".xml", ".json"):
                yield from self._snapshot_documents(spec, cfg, meta, path)
                continue
            if (
                cfg.index_text
                and path.suffix.lower() == ".pdf"
                and (sections := long_pdf_sections(path, cfg.pdf_max_pages))
            ):
                yield from self._section_documents(spec, meta, path, generation_dir, sections)
                continue
            body = document_text(path, max_pages=cfg.pdf_max_pages) if cfg.index_text else ""
            title = reader_title(meta, path, body)
            text = catalog_header(meta, title=title) + ("\n\n" + body if body else "")
            license_ = meta.license or spec.license
            listed_as = clean_title(meta.title) if meta.title else ""
            yield DocRecord(
                doc_id=make_doc_id(spec.id, meta.doc_key),
                source_id=spec.id,
                native_id=meta.doc_key,
                title=title,
                lang=meta.language or "en",
                license=license_,
                text=text,
                revision=meta.edition_date or (pdf_metadata_date(path) if cfg.index_text else None) or None,
                url_native=meta.origin_url or None,
                html_ref=f"file:{meta.generation or generation_dir.name}/{meta.file}",
                content_hash=content_hash(text),
                aliases=tuple(dict.fromkeys(a for a in (meta.pub_number, listed_as) if a and a != title)),
                meta=meta.record_meta(),
            )

    def _section_documents(
        self, spec: SourceSpec, meta: DocumentMeta, path: Path, generation_dir: Path, sections: list[Section]
    ) -> Iterator[DocRecord]:
        """A PDF longer than ``pdf_max_pages`` as its chapters (:func:`wsp_adapters.pdftext.plan_sections`),
        each read whole, and the book itself: its catalog header and its contents, under the id the
        whole PDF always had."""
        front = pdf_text(path, max_pages=min(FRONT_MATTER_PAGES, sections[0].stop))
        title = reader_title(meta, path, front)
        license_ = meta.license or spec.license
        revision = meta.edition_date or pdf_metadata_date(path) or None
        html_ref = f"file:{meta.generation or generation_dir.name}/{meta.file}"
        record_meta = meta.record_meta()
        contents = "\n".join(f"{s.title} (page {s.start + 1})" for s in sections)
        text = catalog_header(meta, title=title) + "\n\nContents\n" + contents
        listed_as = clean_title(meta.title) if meta.title else ""
        yield DocRecord(
            doc_id=make_doc_id(spec.id, meta.doc_key),
            source_id=spec.id,
            native_id=meta.doc_key,
            title=title,
            lang=meta.language or "en",
            license=license_,
            text=text,
            revision=revision,
            url_native=meta.origin_url or None,
            html_ref=html_ref,
            content_hash=content_hash(text),
            aliases=tuple(dict.fromkeys(a for a in (meta.pub_number, listed_as) if a and a != title)),
            meta={**record_meta, "pages": f"1-{sections[-1].stop}"},
        )
        for section in sections:
            body = pdf_text(path, max_pages=section.stop - section.start, first=section.start)
            text = f"{title}\n{section.title} (pages {section.start + 1}-{section.stop})\n\n{body}"
            native = f"{meta.doc_key}/p{section.start + 1}"
            yield DocRecord(
                doc_id=make_doc_id(spec.id, native),
                source_id=spec.id,
                native_id=native,
                title=f"{section.title} — {title}",
                lang=meta.language or "en",
                license=license_,
                text=text,
                revision=revision,
                url_native=f"{meta.origin_url}#page={section.start + 1}" if meta.origin_url else None,
                html_ref=html_ref,
                content_hash=content_hash(text),
                meta={**record_meta, "pages": f"{section.start + 1}-{section.stop}", "part_of": meta.doc_key},
            )

    def _snapshot_documents(
        self, spec: SourceSpec, cfg: CollectionConfig, meta: DocumentMeta, path: Path
    ) -> Iterator[DocRecord]:
        """The documents ``cfg.reader`` finds in a repository snapshot (FRUS volumes, Perseus texts)."""
        published = urlsplit(meta.origin_url).path.rsplit("/", 1)[-1] if meta.origin_url else None
        for d in tei.read_snapshot(path, cfg.reader, unquote(published) if published else None):
            native = f"{meta.doc_key}/{d.key}"
            yield DocRecord(
                doc_id=make_doc_id(spec.id, native),
                source_id=spec.id,
                native_id=native,
                title=d.title,
                lang=d.lang,
                license=meta.license or spec.license,
                text=d.text,
                revision=d.date or meta.edition_date or None,
                url_native=d.url or meta.origin_url or None,
                content_hash=content_hash(d.text),
                meta=meta.record_meta(),
            )


# --- helpers ---------------------------------------------------------------------------------------

DISCOVERY_CANDIDATES = "documents.jsonl"
DISCOVERY_LISTING = "listing.json"


@dataclass(frozen=True, slots=True)
class DiscoveryRecord:
    """What :meth:`DocCollectionAdapter.discover` learned, kept for the release's later steps."""

    candidates: Mapping[str, Candidate]  # by file name
    listed: frozenset[str] | None  # every listed document key; None: the listing was not complete
    protected: frozenset[str]  # listing keys whose documents must not be inferred withdrawn


def write_discovery_record(directory: Path, candidates: Sequence[Candidate], survey: Survey) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    rows = directory / DISCOVERY_CANDIDATES
    tmp = rows.with_name(rows.name + ".partial")
    with tmp.open("w", encoding="utf-8") as fh:
        for cand in candidates:
            fh.write(json.dumps(cand.to_json(), sort_keys=True) + "\n")
    tmp.replace(rows)
    listed = sorted({c.key for c in survey.allowed} | {c.key for c, _d in survey.decisions})
    listing = {
        "complete": survey.listing_complete,
        "listed": listed if survey.listing_complete else None,
        "protected": sorted(survey.protected_keys()),
    }
    out = directory / DISCOVERY_LISTING
    tmp = out.with_name(out.name + ".partial")
    tmp.write_text(json.dumps(listing), encoding="utf-8")
    tmp.replace(out)


def read_discovery_record(directory: Path) -> DiscoveryRecord | None:
    rows, listing = directory / DISCOVERY_CANDIDATES, directory / DISCOVERY_LISTING
    if not rows.is_file() or not listing.is_file():
        return None
    candidates: dict[str, Candidate] = {}
    with rows.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                cand = Candidate.from_json(cast(Mapping[str, Any], json.loads(line)))
                candidates[cand.file_name] = cand
    data = cast(Mapping[str, Any], json.loads(listing.read_text(encoding="utf-8")))
    listed = data.get("listed")
    return DiscoveryRecord(
        candidates=candidates,
        listed=frozenset(str(k) for k in cast(list[object], listed)) if isinstance(listed, list) else None,
        protected=frozenset(str(k) for k in cast(list[object], data.get("protected") or [])),
    )


def discovery_candidate(workdir: Path, name: str) -> Candidate | None:
    """The candidate a stored file name came from, from the newest discovery record that has it."""
    root = workdir / "doc-collection"
    if not root.is_dir():
        return None
    for directory in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        record = _cached_record(directory / DISCOVERY_CANDIDATES)
        if name in record:
            return record[name]
    return None


_RECORDS: dict[tuple[Path, float], dict[str, Candidate]] = {}


def _cached_record(path: Path) -> dict[str, Candidate]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    key = (path, mtime)
    if key not in _RECORDS:
        record = read_discovery_record(path.parent)
        if len(_RECORDS) > 8:
            _RECORDS.clear()
        _RECORDS[key] = dict(record.candidates) if record is not None else {}
    return _RECORDS[key]


def screen_document(path: Path, rules: Rules, *, keys: Iterable[str] = ()) -> Screen:
    """The one document screen of both acquisition paths: the bytes must be what the name says
    (:func:`well_formed`; :class:`WrongBodyError` otherwise: a failed download, not a document), then
    the distribution gate and content screen (:func:`screen_file`)."""
    problem = well_formed(path)
    if problem:
        raise WrongBodyError(problem)
    return screen_file(path, rules, keys=keys)


def fetched_doc(cand: Candidate, path: Path, screen: Screen, *, file: str) -> FetchedDoc:
    """The catalog's record of a screened, allowed download (SHA-256 and MD5 in one read)."""
    sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    with path.open("rb") as fh:
        while chunk := fh.read(8 << 20):
            sha.update(chunk)
            md5.update(chunk)
    rights = cand.rights
    return FetchedDoc(
        cand=cand,
        file=file,
        sha256=sha.hexdigest(),
        bytes=path.stat().st_size,
        decision=str(screen.decision),
        distribution=screen.marking.distribution or (rights if rights.startswith("license:") else ""),
        distribution_text=screen.marking.text,
        fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
        license=rights.removeprefix("license:") if rights.startswith("license:") else "",
        md5=md5.hexdigest(),
    )


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


class Resolved(NamedTuple):
    """A cached resolve: its documents, or why there are none (the resolver said so)."""

    docs: list[Candidate]
    none: str = ""


class ResolveCache:
    """Resolved document candidates by listing candidate (local SQLite in the source's workdir).

    Resolving is one request per item at the source's polite rate: ~11 days for the CIA reading room's
    985k items. A run interrupted by a reboot kept none of it, so every restart began again. Entries are
    keyed by the candidate's whole listing record (a changed title, date or URL misses) and expire after
    ``ttl`` seconds, so a quarterly refresh still sees new files.
    """

    TTL = 45 * 86400.0

    def __init__(self, path: Path, *, ttl: float = TTL, clock: Callable[[], float] = time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS resolved (k TEXT PRIMARY KEY, at REAL NOT NULL, docs TEXT NOT NULL)"
        )
        self._ttl = ttl
        self._clock = clock
        self._pending = 0

    @classmethod
    def open(cls, workdir: Path) -> ResolveCache | None:
        try:
            return cls(workdir / "resolve-cache.sqlite")
        except (OSError, sqlite3.Error) as exc:
            log.warning("resolve cache unavailable in %s: %s", workdir, exc)
            return None

    @staticmethod
    def _key(cand: Candidate) -> str:
        return hashlib.sha256(json.dumps(cand.to_json(), sort_keys=True).encode("utf-8")).hexdigest()

    def get(self, cand: Candidate) -> Resolved | None:
        """The cached resolve, or ``None``. A bare empty list (what earlier versions cached for a failed
        resolve) is a miss: only a resolver's positive "no documents" is remembered empty."""
        row = self._db.execute("SELECT at, docs FROM resolved WHERE k = ?", (self._key(cand),)).fetchone()
        if row is None or self._clock() - float(row[0]) > self._ttl:
            return None
        data: object = json.loads(row[1])
        if isinstance(data, Mapping):
            return Resolved([], str(cast(Mapping[str, object], data).get("none") or "no documents"))
        docs = [Candidate.from_json(d) for d in cast(list[Mapping[str, Any]], data)]
        return Resolved(docs) if docs else None

    def put(self, cand: Candidate, docs: Sequence[Candidate], *, none: str = "") -> None:
        """Remember a resolve: documents, or the resolver's positive ``none`` (a reason). An empty
        result without a reason is a failure and is never stored."""
        if not docs and not none:
            return
        if self._pending == 0:
            self._db.execute("BEGIN")
        payload: object = {"none": none} if not docs else [d.to_json() for d in docs]
        self._db.execute(
            "INSERT OR REPLACE INTO resolved VALUES (?, ?, ?)",
            (self._key(cand), self._clock(), json.dumps(payload)),
        )
        self._pending += 1
        if self._pending >= 50:  # at 1 request/s: lose at most a minute of resolving to a crash
            self.flush()

    def flush(self) -> None:
        if self._pending:
            self._db.execute("COMMIT")
            self._pending = 0

    def close(self) -> None:
        self.flush()
        self._db.close()


def write_audit(path: Path, decisions: Sequence[tuple[Candidate, Decision]]) -> None:
    """Every candidate with its decision (local disk): why each document is or is not in the library."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    with tmp.open("w", encoding="utf-8") as fh:
        for cand, decision in decisions:
            row = {**cand.to_json(), "decision": str(decision), "stage": decision.stage.value}
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    tmp.replace(path)


class WrongBodyError(ValueError):
    """The bytes are not the document the link named: an HTML landing, login or home page, or an
    error body (JSON, XML, a few words of text) where a PDF was expected. A failed download, to be
    retried later; never a document (2026-10-12 audit, CA-2)."""


_SNIFF_BYTES = 4096
# A short text body that says this is an error, not a document.
_ERROR_BODY_MAX = 2048
_ERROR_WORDS_RE = re.compile(
    rb"error|exception|not\s+found|no\s+such\s+key|access\s*denied|forbidden|unauthori[sz]ed|"
    rb"bad\s+(?:request|gateway)|service\s+unavailable|too\s+many\s+requests|rate\s+limit|"
    rb"temporarily\s+unavailable|sign\s?in|log\s?in",
    re.IGNORECASE,
)
_HTML_RE = re.compile(rb"<html|<!doctype\s+html|<head[\s>]|<body[\s>]", re.IGNORECASE)
_OFFICE_SUFFIXES = (".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".zip", ".epub")
# Legacy (97-2003) Office files are OLE2 compound files, not zips. Their main stream names the kind.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OLE_SUFFIXES = (".doc", ".xls", ".ppt")
_OLE_STREAMS = (
    ("WordDocument".encode("utf-16-le"), ".doc"),
    ("Workbook".encode("utf-16-le"), ".xls"),
    ("Book".encode("utf-16-le"), ".xls"),
    ("PowerPoint Document".encode("utf-16-le"), ".ppt"),
)
_OLE_SCAN_BYTES = 4 * 1024 * 1024
_TEXT_DOC_SUFFIXES = (".txt", ".csv", ".md")


def _head(path: Path, n: int = _SNIFF_BYTES) -> bytes:
    with path.open("rb") as fh:
        return fh.read(n)


_ERROR_LEAD = 400  # an error body says so at once: in its first bytes ...
_ERROR_LINES = 2  # ... and its first lines (RFC 71's title "... Input Error" is its 3rd line)
_ERROR_BODY_LINES = 4  # a body this short is all message


def _error_text(head: bytes, size: int) -> bool:
    """A short text body that opens like an error message: the words in its first lines, or anywhere in
    a body of a few lines. RFC 6 says "Error" in its last third; seven early RFCs whose titles name an
    error ("Typographical Error in RFC 107") were refused as error bodies (2026-10-17)."""
    if size > _ERROR_BODY_MAX:
        return False
    lines = [ln for ln in head[:_ERROR_LEAD].splitlines() if ln.strip()]
    if sum(1 for ln in head.splitlines() if ln.strip()) > _ERROR_BODY_LINES:
        lines = lines[:_ERROR_LINES]
    return any(_ERROR_WORDS_RE.search(ln) for ln in lines)


_CONTROL_RE = re.compile(rb"[\x01-\x08\x0e-\x1a\x1c-\x1f\x7f]")  # not in any text (tab, LF, FF, CR, ESC are)
# Text padded with NULs (RFC 734: 15% of its bytes, between pages) is still text: what is left once
# they go must be most of the file, with hardly a control character.
_TEXT_LEFT = 0.75
_TEXT_CONTROLS = 0.005
_XML_RE = re.compile(rb"^\s*<\?xml|</[A-Za-z_]")  # a declaration or a closing tag: not "<GJOURNAL>" (RFC 674)


def body_kind(path: Path) -> str:
    """What a downloaded file's bytes are: ``pdf``, ``zip``, ``ole`` (a legacy Office file), ``html``,
    ``json``, ``xml``, ``text``, ``empty`` or ``binary`` (by content, never by name)."""
    head = _head(path)
    stripped = head.lstrip()
    if not stripped:
        return "empty"
    if stripped.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(_OLE_MAGIC):
        return "ole"
    if _HTML_RE.search(head[:1024]):
        return "html"
    if b"\x00" in head:
        left = head.replace(b"\x00", b"")
        if len(left) < _TEXT_LEFT * len(head) or len(_CONTROL_RE.findall(left)) > _TEXT_CONTROLS * len(left):
            return "binary"
        head, stripped = left, left.lstrip()
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Not merely a character cut at the end of the sample, and not an 8-bit text either (RFC 64
        # writes "µsec" in Latin-1; seven RFCs were refused as binary, 2026-10-17).
        if exc.start < len(head) - 4 and _CONTROL_RE.search(head):
            return "binary"
    if stripped[:1] in (b"{", b"["):
        return "json"
    if stripped[:1] == b"<" and _XML_RE.search(head):
        return "xml"
    return "text"


def true_suffix(path: Path) -> Path:
    """``path`` renamed to the format its bytes are, when a link named it ``.pdf`` by guess: a GOV.UK
    attachment linked without an extension turns out to be a Word, Excel or ODF file, a zip or a CSV.

    Only genuine documents are renamed. An HTML page, a JSON or XML body, or a short text that reads
    like an error, where a PDF was expected, raises :class:`WrongBodyError`: a server's error or landing
    page is a failed download, never a document (HTML publications are named ``.html`` by the
    discoverer that finds them, see ``Candidate.extra['suffix']``).
    """
    if path.suffix.lower() != ".pdf":
        return path
    kind = body_kind(path)
    if kind in ("pdf", "binary", "empty"):
        return path  # a PDF, or something well_formed refuses as one
    suffix = ""
    if kind == "zip":
        suffix = _zip_kind(path)
    elif kind == "ole":
        suffix = _ole_kind(path)
    elif kind == "text" and not _error_text(_head(path), path.stat().st_size):
        suffix = ".txt"  # CSV and plain text attachments
    else:
        raise WrongBodyError(f"expected a PDF, got {kind if kind != 'text' else 'an error message'}")
    if not suffix:
        return path
    target = path.with_suffix(suffix)
    path.replace(target)
    return target


def _ole_kind(path: Path) -> str:
    """``.doc``, ``.xls`` or ``.ppt`` by the main stream a legacy Office file holds (its directory entry
    names are UTF-16), or ``""`` when none is found in the first :data:`_OLE_SCAN_BYTES`: a GOV.UK
    attachment from 2013 linked as a PDF is an Excel 97 workbook (``LEXICON_v2_1_1-Feb-2013.xls``)."""
    data = _head(path, _OLE_SCAN_BYTES)
    for name, suffix in _OLE_STREAMS:
        if name in data:
            return suffix
    return ""


def _zip_kind(path: Path) -> str:
    """``.docx``, ``.xlsx``, ``.pptx``, ``.odt``/``.ods``/``.odp`` or ``.zip`` by what a zip file holds."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "word/document.xml" in names:
                return ".docx"
            if any(n.startswith("xl/") for n in names):
                return ".xlsx"
            if any(n.startswith("ppt/") for n in names):
                return ".pptx"
            if "mimetype" in names:
                info = zf.getinfo("mimetype")
                mime = _read_member(zf, info, 200).decode("ascii", "replace")
                for kind, ext in (("text", ".odt"), ("spreadsheet", ".ods"), ("presentation", ".odp")):
                    if kind in mime:
                        return ext
    except zipfile.BadZipFile:
        return ""
    return ".zip"


_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_CANONICAL_RE = re.compile(
    rb"<link[^>]+rel=[\"']?canonical[\"']?[^>]*href=[\"']?([^\"'\s>]+)|"
    rb"<link[^>]+href=[\"']?([^\"'\s>]+)[\"']?[^>]*rel=[\"']?canonical",
    re.IGNORECASE,
)
_LANDING_TITLE_RE = re.compile(
    rb"\b(?:sign\s?in|log\s?in|login|access\s+denied|page\s+not\s+found|not\s+found|404|403|error)\b",
    re.IGNORECASE,
)


def landing_page(head: bytes) -> str | None:
    """Why an HTML body is a landing, login, error or home page rather than a document, or ``None``."""
    title = _TITLE_RE.search(head)
    if title is not None and _LANDING_TITLE_RE.search(title.group(1)):
        return f"a login or error page ({title.group(1).decode('utf-8', 'replace').strip()[:60]!r})"
    canonical = _CANONICAL_RE.search(head)
    if canonical is not None:
        href = (canonical.group(1) or canonical.group(2) or b"").decode("utf-8", "replace")
        parts = urlsplit(href)
        if parts.netloc and parts.path in ("", "/") and not parts.query:
            return f"the site's home page ({href})"
    return None


def well_formed(path: Path) -> str | None:
    """A cheap format check by name: PDFs start with ``%PDF-``; office files and zips are zip
    containers; text is text, not an error message; HTML is a document, not a landing page."""
    head = _head(path, 64 * 1024)
    name = path.name.lower()
    if name.endswith(".pdf") and not head.lstrip().startswith(b"%PDF-"):
        return "not a PDF (missing %PDF- header)"
    if name.endswith(_OFFICE_SUFFIXES) and not head.startswith(b"PK\x03\x04"):
        return (
            "not a zip archive"
            if name.endswith(".zip")
            else f"not a {Path(name).suffix} file (no zip container)"
        )
    if name.endswith(_OLE_SUFFIXES) and not head.startswith(_OLE_MAGIC):
        return f"not a {Path(name).suffix} file (no OLE2 header)"
    if name.endswith(_TEXT_DOC_SUFFIXES):
        kind = body_kind(path)
        if kind not in ("text", "empty"):
            return f"not text ({kind})"
        if _error_text(head, path.stat().st_size):
            return "an error message, not a document"
    if name.endswith((".html", ".htm")):
        if b"<" not in head[:1024]:
            return "not HTML"
        if why := landing_page(head):
            return f"not a document: {why}"
    return None


# pdfium keeps what it parsed of each page (drawings, fonts, images) until the document is closed:
# FEMA's "Seismic Design of Rigid Wall-Flexible Diaphragm Buildings" (CAD drawings) grew the nightly
# refine past its 7 GB ceiling and the kernel killed it, so no source after it was indexed
# (2026-10-17). Reopening the document every few pages lets that memory go; a document past its
# time budget keeps the text read so far and says which pages were not read.
PAGES_PER_OPEN = 10
PDF_SECONDS = 300.0


def pdf_text(
    path: Path,
    max_pages: int,
    *,
    first: int = 0,
    seconds: float = PDF_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Text of ``max_pages`` pages from page ``first`` (0-based; pypdfium2, one page in memory at a
    time), read for at most ``seconds``."""
    import pypdfium2  # imported on use: only PDF documents need it

    pdf = pypdfium2.PdfDocument(str(path))
    try:
        out: list[str] = []
        pages = min(len(pdf), first + max_pages)
        started = clock()
        for i in range(first, pages):
            if clock() - started > seconds:
                log.warning(
                    "%s: text read for %.0f s; pages %d-%d left unread", path.name, seconds, i + 1, pages
                )
                out.append(
                    f"[Pages {i + 1}-{pages} of this PDF were not read: its drawings take too long to read.]"
                )
                break
            if i > first and (i - first) % PAGES_PER_OPEN == 0:
                pdf.close()
                pdf = pypdfium2.PdfDocument(str(path))
            page = pdf[i]
            try:
                textpage = page.get_textpage()
                out.append(page_text(page, textpage))
                textpage.close()
            finally:
                page.close()
        # A form feed between pages: policy._edge_banner reads page edges. U+FFFD (a glyph the PDF does
        # not map to a character, e.g. the USDA tables' spacing) becomes a space.
        text = "\n\f\n".join(out).replace("\ufffd", " ").replace("\u00ad", "")  # soft hyphens split words
        return _LINE_HYPHEN.sub("", text)
    finally:
        pdf.close()


# pdfium writes U+FFFE for a hyphen that breaks a word at the end of a line ("fol\ufffe\r\nlowing"):
# the word is joined again. It split "following" and "position" in Bowditch, so neither searching nor
# finding them worked (2026-10-16 field exam).
_LINE_HYPHEN = re.compile("\ufffe[ \t]*(?:\r?\n)?")


class Extracted(NamedTuple):
    """Text of a document, or why there is none (``problem``: see :data:`NO_TEXT`)."""

    text: str
    problem: str = ""  # "" | "unsupported-format" | "extraction-error" | "no-text-layer"


# Fewer letters than this in the front matter is no text layer: a scan, or only page numbers.
MIN_FRONT_MATTER_LETTERS = 64
_LETTER_RE = re.compile(r"[^\W\d_]")
TEXT_SUFFIXES = (".txt", ".xml", ".json", ".md")


def plain_text(data: bytes) -> str:
    """A text file's text: UTF-8, else Windows-1252 (RFC 64's "µsec" is Latin-1); NUL padding dropped."""
    data = data.replace(b"\x00", b"")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start >= len(data) - 4:  # a character cut at the end of the read
            return data[: exc.start].decode("utf-8", "replace")
        return data.decode("cp1252", "replace")


def extract_text(path: Path, *, max_pages: int) -> Extracted:
    """Text of a vault file. Its page cache is released afterwards: a refine or a screening reads
    each file once, and on the NAS those pages would stay charged to the job's memory ceiling."""
    try:
        return _extract_text(path, max_pages=max_pages)
    finally:
        drop_file_cache(path)


def _extract_text(path: Path, *, max_pages: int) -> Extracted:
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
            return Extracted(plain_text(path.read_bytes()[: 16 * 1024 * 1024]))
        if suffix == ".zip" and head.startswith(b"PK\x03\x04"):  # an archive, not a DOCX/EPUB container
            return Extracted(zip_screen_text(path))
        if suffix == ".docx" and head.startswith(b"PK\x03\x04"):
            return Extracted(docx_text(path))
    except Exception as exc:
        log.warning("text extraction failed for %s: %s", path.name, exc)
        return Extracted("", "extraction-error")
    return Extracted("", "unsupported-format")


ZIP_SCREEN_CHARS = 60_000
# Bounds on what the gate reads inside an archive (2026-10-12 audit, CA-17): members are streamed,
# never read whole; a member larger than this is named but not read; a member that inflates more
# than ZIP_MAX_RATIO times is a zip bomb and the archive is held.
ZIP_MEMBER_MAX_BYTES = 64 * 1024 * 1024
ZIP_MAX_RATIO = 200
ZIP_MEMBERS_SCREENED = 40
ZIP_MEMBER_CHARS = 20_000
ZIP_DOCUMENT_SUFFIXES = (".pdf", ".docx", ".txt", ".md", ".html", ".htm", ".xml")


class ArchiveError(ValueError):
    """A zip member the gate refuses to inflate (compression ratio of a zip bomb)."""


_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _readable_member(info: zipfile.ZipInfo) -> bool:
    """``False`` for a member too large to read; :class:`ArchiveError` for a zip bomb."""
    if info.file_size and (info.compress_size == 0 or info.file_size / info.compress_size > ZIP_MAX_RATIO):
        raise ArchiveError(f"{info.filename}: compression ratio beyond {ZIP_MAX_RATIO}")
    return info.file_size <= ZIP_MEMBER_MAX_BYTES


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """At most ``limit`` bytes of a member, streamed (a lying header cannot make it read more)."""
    with zf.open(info) as fh:
        return fh.read(limit)


def _docx_paragraphs(zf: zipfile.ZipFile) -> str:
    import xml.etree.ElementTree as ET

    info = zf.getinfo("word/document.xml")
    if not _readable_member(info):
        raise ArchiveError(f"word/document.xml is {info.file_size:,} bytes")
    data = _read_member(zf, info, ZIP_MEMBER_MAX_BYTES)
    root = ET.fromstring(data)  # noqa: S314 - expat rejects external entities
    paragraphs = ("".join(t.text or "" for t in p.iter(f"{_W_NS}t")) for p in root.iter(f"{_W_NS}p"))
    return "\n".join(p for p in paragraphs if p.strip())


def docx_text(path: Path) -> str:
    """The text of a Word (.docx) document: its paragraphs from ``word/document.xml``, in order."""
    with zipfile.ZipFile(path) as zf:
        return _docx_paragraphs(zf)


def zip_screen_text(path: Path) -> str:
    """What the gate reads of a zip archive as a whole: its README-like members, then its member names
    (nested zips' names too). A list of names reads like a table of contents ("U_Windows_10_V3R1_STIG.zip"),
    so the content screen's title-page and three-phrase rules apply to it, as for git bundles. The
    member documents themselves are screened one by one (:func:`screen_zip`)."""
    parts: list[str] = []
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        for info in infos:
            low = info.filename.lower().rsplit("/", 1)[-1]
            readme = low.startswith("readme") and low.endswith((".txt", ".md", "")) and not info.is_dir()
            if readme and _readable_member(info):
                parts.append(_read_member(zf, info, ZIP_MEMBER_CHARS).decode("utf-8", "replace"))
        parts.extend(i.filename.replace("/", " / ").replace("_", " ") for i in infos)
    return "\n".join(parts)[:ZIP_SCREEN_CHARS]


def zip_member_texts(path: Path) -> list[tuple[str, Extracted]]:
    """Front matter of the first :data:`ZIP_MEMBERS_SCREENED` documents in an archive (PDF, Word,
    text, HTML, XML), each read within the bounds above. Raises :class:`ArchiveError` for a bomb."""
    import io
    import tempfile

    out: list[tuple[str, Extracted]] = []
    with zipfile.ZipFile(path) as zf:
        members = [
            i for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith(ZIP_DOCUMENT_SUFFIXES)
        ]
        for info in members[:ZIP_MEMBERS_SCREENED]:
            name = info.filename
            if not _readable_member(info):
                out.append((name, Extracted("", "unsupported-format")))
                continue
            suffix = Path(name).suffix.lower()
            try:
                if suffix == ".pdf":
                    with tempfile.TemporaryDirectory(prefix="wsp-zip-") as tmp:
                        member = Path(tmp) / "member.pdf"
                        member.write_bytes(_read_member(zf, info, ZIP_MEMBER_MAX_BYTES))
                        text = pdf_text(member, FRONT_MATTER_PAGES)
                elif suffix == ".docx":
                    data = _read_member(zf, info, ZIP_MEMBER_MAX_BYTES)
                    with zipfile.ZipFile(io.BytesIO(data)) as inner:
                        text = _docx_paragraphs(inner)
                elif suffix in (".html", ".htm"):
                    text = html_to_text(_read_member(zf, info, 4 * ZIP_MEMBER_CHARS))
                else:
                    text = _read_member(zf, info, ZIP_MEMBER_CHARS).decode("utf-8", "replace")
            except ArchiveError:
                raise
            except Exception as exc:
                log.warning("%s: cannot read member %s: %s", path.name, name, exc)
                out.append((name, Extracted("", "extraction-error")))
                continue
            out.append((name, checked_front_matter(text[: 4 * ZIP_MEMBER_CHARS])))
    return out


def long_pdf_sections(path: Path, max_pages: int) -> list[Section]:
    """The sections of a PDF longer than ``max_pages`` pages; ``[]`` for a shorter one (read whole) or
    one that does not open."""
    import pypdfium2

    try:
        pdf = pypdfium2.PdfDocument(str(path))
    except pypdfium2.PdfiumError:
        return []
    try:
        n = len(pdf)
        return plan_sections(n, outline_marks(pdf)) if n > max_pages else []
    finally:
        pdf.close()


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
    overrides a deny. A content review is released only by ``allow_content_review`` and a
    deny-core hit only by ``override_safety`` (with its reason in the rules file).

    A zip archive is screened member by member as well (:func:`screen_zip`).
    """
    if path.suffix.lower() == ".zip":
        return screen_zip(path, rules, keys=keys)
    return screen_text(front_matter(path), rules, keys=keys)


def screen_text(got: Extracted, rules: Rules, *, keys: Iterable[str] = ()) -> Screen:
    """:func:`screen_file` on already extracted front matter (also used for git bundles)."""
    text_found = not got.problem
    # Owner decisions hold normalized numbers and keys (Rules.merged), so normalize to compare.
    ids = {normalize_pub_number(k) for k in keys if k}
    owner_allowed = bool(ids & rules.allow_documents)
    content_released = bool(ids & rules.allow_content_review)
    marking = parse_markings(got.text)
    if rules.public_issuer:  # the issuer publishes everything openly (Rules.public_issuer)
        gate = Decision(Verdict.ALLOW, "marking=public-issuer", Stage.DISTRIBUTION)
    else:
        gate = check_distribution(marking, rules.require, declassified_ok=rules.declassified_ok)
    if gate.verdict is Verdict.DENY:
        return Screen(gate, marking, text_found)
    content = classify_content(got.text, rules, overridden=bool(ids & rules.override_safety))
    if content.verdict is Verdict.DENY:
        return Screen(content, marking, text_found)
    if gate.verdict is Verdict.REVIEW and not owner_allowed:
        return Screen(gate, marking, text_found)
    image_only_ok = got.problem == "no-text-layer" and rules.allow_image_only
    if not text_found and not owner_allowed and not image_only_ok:
        return Screen(Decision(Verdict.REVIEW, f"text={got.problem}", Stage.CONTENT), marking, text_found)
    if content.verdict is Verdict.REVIEW and not content_released:
        return Screen(content, marking, text_found)
    rule = gate.rule
    if gate.verdict is Verdict.REVIEW:
        rule = f"owner=allow+{gate.rule}"
    if content.verdict is Verdict.REVIEW:
        rule += f"+owner=allow-content:{content.rule}"
    if not text_found:
        rule += f"+text={got.problem}" + ("" if image_only_ok else "+owner=allow")
    return Screen(Decision(Verdict.ALLOW, rule, Stage.DISTRIBUTION), marking, text_found)


def screen_zip(path: Path, rules: Rules, *, keys: Iterable[str] = ()) -> Screen:
    """An archive is as restricted as its most restricted member (2026-10-12 audit, CA-17).

    The archive's README and member names are screened as a whole (:func:`zip_screen_text`), then
    the front matter of each member document (:func:`zip_member_texts`): a member a marking or the
    deny core rejects rejects the archive, and a member the content screen holds holds it. Members'
    missing-evidence holds (no text, no printed release statement) are the archive's business, not
    each member's. A zip bomb holds the archive.
    """
    archive = screen_text(front_matter(path), rules, keys=keys)
    if archive.decision.verdict is Verdict.DENY or not archive.text_found:  # unreadable: the hold stands
        return archive
    try:
        members = zip_member_texts(path)
    except (ArchiveError, zipfile.BadZipFile, OSError) as exc:
        held = Decision(Verdict.REVIEW, f"text=extraction-error:{type(exc).__name__}", Stage.CONTENT)
        return Screen(held, archive.marking, archive.text_found)
    review: Screen | None = None
    for name, got in members:
        member = screen_text(got, rules, keys=keys)
        named = replace(member.decision, rule=f"{member.decision.rule}@{name}")
        if member.decision.verdict is Verdict.DENY:
            return Screen(named, member.marking, archive.text_found)
        held_for_content = member.decision.rule.startswith(("safety=", "marking=export-mention"))
        if member.decision.verdict is Verdict.REVIEW and held_for_content and review is None:
            review = Screen(named, member.marking, archive.text_found)
    if review is not None and archive.decision.verdict is Verdict.ALLOW:
        return review
    return archive


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


_JUNK_PDF_TITLES = frozenset(
    {"untitled", "document", "title", "pdf", "slide 1", "powerpoint presentation", "microsoft word"}
)
_WORD_PREFIX_RE = re.compile(r"^microsoft (?:word|powerpoint|excel)\s*-\s*", re.IGNORECASE)


def _meaningful(title: str) -> str:
    """``title`` when it can name a document, else ``""`` (a file name, a template's placeholder, a
    sentence fragment)."""
    t = re.sub(r"\s+", " ", _WORD_PREFIX_RE.sub("", title)).strip()
    if len(t) < 4 or len(t) > 200 or t.casefold() in _JUNK_PDF_TITLES:
        return ""
    if is_filename_title(t) or is_fragment(t):
        return ""
    return t


def pdf_metadata_title(path: Path) -> str:
    """The PDF's own ``/Title``, when it is meaningful."""
    if path.suffix.lower() != ".pdf":
        return ""
    try:
        import pypdfium2

        pdf = pypdfium2.PdfDocument(str(path))
        try:
            raw = str(pdf.get_metadata_dict().get("Title") or "")
        finally:
            pdf.close()
    except Exception as exc:  # a damaged PDF has no title to offer; its text may still be read
        log.debug("no PDF metadata for %s: %s", path.name, exc)
        return ""
    return _meaningful(raw)


_PDF_DATE_RE = re.compile(r"^(?:D:)?((?:19|20)\d\d)(\d\d)?(\d\d)?")


def pdf_date(raw: str) -> str:
    """``D:20150709...`` -> ``2015-07-09`` (``2015-07`` or ``2015`` when the rest is missing or bad)."""
    m = _PDF_DATE_RE.match(raw.strip())
    if m is None:
        return ""
    year, month, day = m.group(1), m.group(2), m.group(3)
    if month is None or not 1 <= int(month) <= 12:
        return year
    if day is None or not 1 <= int(day) <= 31:
        return f"{year}-{month}"
    return f"{year}-{month}-{day}"


def pdf_metadata_date(path: Path) -> str:
    """The PDF's own modification (else creation) date, for a document the issuer lists undated: the
    USDA canning guides read "as of: unknown" (field exam 2026-10-14)."""
    if path.suffix.lower() != ".pdf":
        return ""
    try:
        import pypdfium2

        pdf = pypdfium2.PdfDocument(str(path))
        try:
            meta = pdf.get_metadata_dict()
        finally:
            pdf.close()
    except Exception as exc:  # a damaged PDF has no date to offer
        log.debug("no PDF metadata for %s: %s", path.name, exc)
        return ""
    return pdf_date(str(meta.get("ModDate") or "")) or pdf_date(str(meta.get("CreationDate") or ""))


def heading_line(body: str, *, scan_chars: int = 3000) -> str:
    """The first heading-like line of a document's text: a few words, mostly letters, not a fragment."""
    for line in body[:scan_chars].splitlines():
        t = re.sub(r"\s+", " ", line).strip()
        words = t.split()
        if len(words) < 2 or not 8 <= len(t) <= 150:
            continue
        letters = sum(c.isalpha() for c in t)
        if letters < 0.6 * len(t.replace(" ", "")):
            continue
        if found := _meaningful(t):
            return found
    return ""


def reader_title(meta: DocumentMeta, path: Path, body: str) -> str:
    """The title readers see. The catalog's title, cleaned (no extension or URL escapes; a USGS sheet
    named in words); when that is a file name or a sentence fragment, the PDF's own title, else the
    document's first heading-like line, else the cleaned file name."""
    shown = display_title(meta.title) if meta.title else ""
    if not shown or is_fragment(shown) or is_filename_title(meta.title):
        shown = pdf_metadata_title(path) or heading_line(body) or shown
    return shown or meta.pub_number or meta.doc_key


def catalog_header(meta: DocumentMeta, *, title: str = "") -> str:
    """A short, human-readable header so search and readers see what the document is."""
    lines = [title or meta.title or meta.doc_key]
    facts = [
        f"Issuer: {meta.issuer}" if meta.issuer else "",
        f"Publication: {meta.pub_number}" if meta.pub_number else "",
        f"Edition: {meta.edition_date}" if meta.edition_date else "",
        f"Status: {meta.currency}" if meta.currency and meta.currency != "current" else "",
    ]
    lines += [f for f in facts if f]
    if banner := banner_for(meta.record_meta()):
        lines.append(banner)
    return "\n".join(lines)
