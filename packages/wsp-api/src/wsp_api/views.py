"""Read-side helpers shared by the routes: the services, the library snapshot and the defaults for
unwired seams."""

from __future__ import annotations

import contextlib
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import quote

from wsp_api import convert, schemas
from wsp_api.addresses import address_of
from wsp_api.catalog import FeedCatalog
from wsp_api.errors import ApiError
from wsp_api.htmlrewrite import VERSION as REWRITE_VERSION
from wsp_api.htmlrewrite import rewrite_html
from wsp_api.offload import DaemonPool, LinkChecks, Offload
from wsp_api.originals import FILE_REF, stored_file
from wsp_api.outside import OutsideLinks
from wsp_api.pages import ByteLRU, Rendered, page_links, render
from wsp_api.renditions import (
    Rendition,
    has_renditions,
    release_time,
    rendition_doc_id,
    renditions,
    stand_ins,
)
from wsp_api.schemas import Change, DocRef, DocumentLink, SourceInfo, VaultLocation
from wsp_api.seams import DocumentFields, VaultView, full_generation
from wsp_api.services import ApiSettings, Services
from wsp_api.snapshot import LibrarySnapshot, LibraryState, StateSettings
from wsp_api.zim import BookIndex, ZimAssetStore
from wsp_api.zimpool import ZimProcessPool
from wsp_api.zimread import InProcessZimReader, ZimReader, ZimUnavailable
from wsp_core.ids import DocId, InvalidDocIdError, make_doc_id, parse_doc_id
from wsp_core.model import (
    Artifact,
    ChangeRecord,
    DocRecord,
    Generation,
    GenerationStatus,
    SearchHit,
    SourceSpec,
    SourceStatus,
    searchable,
)
from wsp_core.ports import AssetStore, Catalog
from wsp_core.titles import shown_title

log = logging.getLogger(__name__)
MANIFEST_NAME = "MANIFEST.json"  # same name as wsp_vault.manifest.MANIFEST_NAME
ZIM_ADAPTER = "kiwix-opds"
BOOK_INDEX_FILE = "zim-books.json"
SITE_INDEX_FILE = "zim-sites.json"  # the websites the archives copy (wsp_api.outside), kept across starts
_MAX_LINK_LOOKUPS = 1000
_MAX_ZIM_LINK_CHECKS = 32  # each opens an archive entry (no content read)
_HISTORY_CACHE = 2048
HIT_COLUMNS = ("license", "timestamp", "lang", "url_native", "revision")


def parse_doc(raw: str) -> DocId:
    try:
        source_id, native_id = parse_doc_id(raw)
        return make_doc_id(source_id, native_id)
    except InvalidDocIdError as exc:
        raise ApiError(400, "invalid_doc_id", str(exc)) from exc


@dataclass(frozen=True, slots=True)
class DocumentView:
    """Everything ``GET /v1/documents/{id}`` returns, before it becomes JSON."""

    doc: DocRecord
    generation_id: str | None
    html: str | None
    vault: VaultLocation | None
    has_history: bool
    rendition: schemas.Rendition | None = None
    section: str | None = None


# The contents a long PDF's whole document lists (wsp_adapters.doccollection: "Title (page N)").
_CONTENTS_HEAD = "\n\nContents\n"
_CONTENTS_LINE = re.compile(r"^(.+) \(page (\d+)\)$")
_GENERATION_LIKE = re.compile(r"^\d{4}(-\d{2}){0,2}")  # 2026-08, 2026-10-12r5.b0001 ...

DEFAULT_ZIM_MAX_OPEN = 512
SNAPSHOT_CACHE_FILE = "library-snapshot.pickle"


def _zim_reader(settings: ApiSettings, services: Services) -> ZimReader:
    if settings.zim_workers <= 0:
        return InProcessZimReader()
    # Every archive stays open: an open archive costs ~0.2 MB of anonymous memory (the rest is page
    # cache), while a cold open of a large one over SMB takes up to minutes. One open per Kiwix *source*
    # (the old default) let stackexchange's 181 archives evict each other on every page.
    max_open = settings.zim_max_open or DEFAULT_ZIM_MAX_OPEN
    return ZimProcessPool(
        workers=settings.zim_workers, max_open=max_open, timeout=settings.zim_timeout_seconds
    )


class Library:
    def __init__(self, services: Services, settings: ApiSettings | None = None) -> None:
        settings = settings or ApiSettings()
        self.s = services
        self.settings = settings
        self.catalog: Catalog = services.catalog or FeedCatalog(services.changes)
        self.state = LibraryState(
            vault=services.vault,
            specs=services.sources,
            catalog=self.catalog,
            ledger=services.ledger,
            ops=services.ops,
            clock=services.clock,
            settings=StateSettings(
                background=settings.background,
                snapshot_seconds=settings.snapshot_seconds,
                ops_seconds=settings.ops_seconds,
                ledger_verify_seconds=settings.ledger_verify_seconds,
                cache_file=settings.cache_dir / SNAPSHOT_CACHE_FILE if settings.cache_dir else None,
            ),
        )
        self.zim: ZimAssetStore | None
        if services.assets is None:
            index = BookIndex(settings.cache_dir / BOOK_INDEX_FILE if settings.cache_dir else None)
            self.zim = ZimAssetStore(
                self.state.vault,
                _zim_reader(settings, services),
                book_index=index,
                sync_books=not settings.background,
                site_index=BookIndex(settings.cache_dir / SITE_INDEX_FILE if settings.cache_dir else None),
            )
            self.assets: AssetStore = self.zim
        else:
            self.assets = services.assets
            self.zim = services.assets if isinstance(services.assets, ZimAssetStore) else None
        if self.zim is not None:
            self.zim.names = {x.id: x.name for x in services.sources}
            self.state.home_finder = self.zim
        self.pages: ByteLRU[tuple[str, ...], Rendered] = ByteLRU(
            settings.page_cache_bytes, lambda r: r.nbytes
        )
        self.nas = Offload(
            workers=settings.nas_workers,
            queue=settings.nas_queue,
            timeout=settings.nas_timeout_seconds,
            name="nas",
        )
        self.searches = Offload(
            workers=settings.search_workers,
            queue=settings.search_queue,
            timeout=settings.search_timeout_seconds,
            name="search",
        )
        # /v1/links/missing: four lookups at a time, shared by every page, so one slow archive does not
        # hold up a page's other links. Daemon threads: they never keep a process alive.
        self.link_checks = DaemonPool(4, "link-check")
        self.link_answers = LinkChecks(self.link_checks)
        self._history: OrderedDict[tuple[DocId, tuple[str, ...]], tuple[tuple[str, str], ...]] = OrderedDict()
        self._history_lock = threading.Lock()
        self._fields = services.documents if isinstance(services.documents, DocumentFields) else None
        lookup = getattr(services.index, "lookup", None)
        self._index_lookup = cast(Callable[[str], SearchHit | None], lookup) if callable(lookup) else None
        self._outside: tuple[int, OutsideLinks] | None = None

    # --- outside links ---

    def outside(self) -> OutsideLinks:
        """Outside URLs the library holds (:mod:`wsp_api.outside`), rebuilt as archives' sites are found."""
        version = self.zim.sites_version if self.zim is not None else 0
        cached = self._outside
        if cached is not None and cached[0] == version:
            return cached[1]

        def current(source_id: str) -> str | None:
            gen = self.vault.current(source_id)
            return None if gen is None else gen.generation_id

        def held(source_id: str) -> bool:
            spec = self.s.source(source_id)
            if spec is None or spec.status is not SourceStatus.ACTIVE or current(source_id) is None:
                return False
            return spec.adapter == ZIM_ADAPTER or searchable(spec)

        links = OutsideLinks(self.zim.sites(current) if self.zim is not None else (), held)
        self._outside = (version, links)
        return links

    def outside_url(self, url: str, *, asset: bool = False) -> str | None:
        """The library's address of an outside URL (``asset``: the file's asset URL), or ``None``."""
        hit = self.outside().target(url)
        if hit is None:
            return None
        (source_id, native), fragment = hit
        if asset:
            gen = self.vault.current(source_id)
            spec = self.s.source(source_id)
            if gen is None or spec is None or spec.adapter != ZIM_ADAPTER:
                return None
            return f"/v1/assets/{source_id}/{gen.generation_id}/{quote(native, safe='/')}"
        return address_of(source_id, native) + (f"#{fragment}" if fragment else "")

    @property
    def vault(self) -> VaultView:
        """The vault as requests see it: the in-memory snapshot in the server, else the vault itself."""
        return self.state.vault

    def close(self) -> None:
        self.state.stop()
        self.nas.close()
        self.searches.close()
        self.link_checks.close()
        if self.zim is not None:
            self.zim.close()
        if self.s.translator is not None:
            self.s.translator.close()  # stops the translation server: its GPU memory goes back

    # --- sources ---

    def source(self, source_id: str) -> SourceSpec:
        spec = self.s.source(source_id)
        if spec is None:
            raise ApiError(404, "source_not_found", f"unknown source: {source_id}")
        return spec

    def source_info(self, spec: SourceSpec, snap: LibrarySnapshot | None = None) -> SourceInfo:
        """A site's card, from the snapshot (no NAS read in the server, audit #4/#11)."""
        snap = snap or self.state.snapshot()
        st = snap.source(spec.id)
        info = convert.source_info(
            spec,
            st.current,
            specs=self.s.sources,
            now=self.s.clock(),
            doc_count=snap.doc_counts.get(spec.id) or self._zim_count(spec.id, st.current),
            home_doc_id=self.state.home_doc_id(spec, st.current),
            members=st.members if spec.partitioned else (),
            sizes=st.sizes,
        )
        return info.model_copy(update={"readable": self.is_readable(info)})

    def _zim_count(self, source_id: str, current: Generation | None) -> int | None:
        """Articles of a ZIM-served generation (counted when its archives were opened), else None."""
        if self.zim is None or current is None:
            return None
        return self.zim.article_counts.get((source_id, current.generation_id))

    def source_infos(self, snap: LibrarySnapshot | None = None) -> list[SourceInfo]:
        snap = snap or self.state.snapshot()
        return [self.source_info(x, snap) for x in sorted(self.s.sources, key=lambda x: x.id)]

    def source_name(self, source_id: str) -> str:
        spec = self.s.source(source_id)
        return source_id if spec is None else spec.name

    def random_zim_page(self, source_id: str) -> DocRef | None:
        """A random article of a single-archive ZIM site (no Refinery catalog to pick from)."""
        spec = self.s.source(source_id)
        if self.zim is None or spec is None or spec.adapter != ZIM_ADAPTER:
            return None
        current = self.state.snapshot().source(source_id).current
        if current is None:
            return None
        found = self.zim.random(source_id, current.generation_id)
        if found is None:
            return None
        native, title = found
        return DocRef(
            doc_id=make_doc_id(source_id, native),
            source_id=source_id,
            title=title or native,
            address=address_of(source_id, native),
        )

    def readable_sources(self) -> frozenset[str]:
        """Sites a reader can open: held (a current generation) and either served from ZIM archives or
        holding refined documents (or a home page).

        A planned source with nothing downloaded, or raw data the reader cannot show (a map tile
        planet, a Wikidata dump), is not listed on the portal or in the directory.
        """
        return frozenset(i.id for i in self.source_infos() if self.is_readable(i))

    def is_readable(self, info: SourceInfo) -> bool:
        spec = self.s.source(info.id)
        zim = spec is not None and spec.adapter == ZIM_ADAPTER
        return bool(info.current_generation) and (zim or bool(info.doc_count) or bool(info.home_doc_id))

    def has_docs(self, sources: Sequence[str]) -> list[str]:
        counts = self.state.snapshot().doc_counts
        return [x for x in sources if counts.get(x)]

    # --- cheap document lookups (no article text: audit #9, #10, #20) ---

    def fields(self, did: DocId, columns: Sequence[str]) -> Mapping[str, object] | None:
        """Some columns of the current version, without the text when the store can project."""
        if self._fields is not None:
            return self._fields.fields(did, columns)
        doc = self.s.documents.get(did)
        return None if doc is None else {c: getattr(doc, c) for c in ("doc_id", *columns)}

    def in_store(self, did: DocId) -> bool:
        """In the document store (current version)."""
        if self._index_lookup is not None and self._index_lookup(did) is not None:
            return True
        return self.fields(did, ()) is not None

    def exists(self, did: DocId) -> bool:
        """In the document store, or (Kiwix sources) an HTML page of the current ZIM. Never reads a page."""
        if self.in_store(did):
            return True
        source_id, native_id = parse_doc_id(did)
        spec = self.s.source(source_id)
        if spec is None or spec.adapter != ZIM_ADAPTER:
            return False
        try:
            gen = self.vault.current(source_id)
            if gen is None:
                return False
            if self.zim is not None:
                return self.zim.exists(source_id, gen.generation_id, native_id)
            return self.assets.html(source_id, gen.generation_id, f"zim:{native_id}") is not None
        except (ApiError, ZimUnavailable):  # busy is a 503 the client retries, not "not in the library"
            raise
        except Exception:  # an unreadable archive is "not here", not an error page
            log.warning("existence check failed for %s", did, exc_info=True)
            return False

    def stand_in(self, did: DocId) -> DocId | None:
        """A page of a rendition source that stands in for ``did`` when the library lacks ``did``
        itself (:func:`wsp_api.renditions.stand_ins`). Raises :class:`ZimUnavailable` when busy."""
        source_id, native_id = parse_doc_id(did)
        for alt in stand_ins(self.s.source(source_id), native_id, self.vault):
            if alt != did and self.exists(alt):
                return alt
        return None

    def title(self, did: str) -> str | None:
        try:
            parsed = parse_doc(did)
        except ApiError:
            return None
        if self._index_lookup is not None:
            hit = self._index_lookup(parsed)
            if hit is not None and hit.title:
                return hit.title
        row = self.fields(parsed, ("title",))
        return None if row is None else str(row["title"])

    def hit_fields(self, did: DocId) -> Mapping[str, object] | None:
        return self.fields(did, HIT_COLUMNS)

    def change(self, rec: ChangeRecord) -> Change:
        source_id = rec.doc_id.removeprefix("wsp:").partition("/")[0]
        return convert.change(
            rec, title=self.title(rec.doc_id), added_at=self.sealed_at(source_id, rec.generation_id)
        )

    def sealed_at(self, source_id: str, generation_id: str) -> datetime | None:
        """When the generation was sealed (the vault view is the server's in-memory snapshot)."""
        try:
            gens = self.vault.generations(source_id)
        except Exception:  # an unreadable source dates nothing; the feed still lists its changes
            log.debug("no generations for %s", source_id, exc_info=True)
            return None
        return next((g.sealed_at for g in gens if g.generation_id == generation_id), None)

    def copies(self, doc: DocRecord, spec: SourceSpec | None) -> list[schemas.OtherCopy]:
        """Other copies of ``doc`` the library holds (registry ``access.copies``): matched by the same
        native id in another source (``prefix`` put in front), or by a link of ``doc`` into that source.

        The text copies of Gutenberg books carry no pictures while the ZIM edition does, and a
        Wikisource page from the dumps can be an empty transclusion whose ZIM rendering is complete;
        search returns the first and nothing pointed at the second (2026-10-14 field exam).
        """
        access = spec.raw.get("access") if spec is not None else None
        rules = cast(Mapping[str, object], access).get("copies") if isinstance(access, Mapping) else None
        if not isinstance(rules, list):
            return []
        out: list[schemas.OtherCopy] = []
        targets: list[str] | None = None
        for raw in cast(list[object], rules):
            if not isinstance(raw, Mapping):
                continue
            rule = cast(Mapping[str, object], raw)
            source, why = str(rule.get("source", "")), str(rule.get("why", "another copy"))
            if not source or self.s.source(source) is None:
                continue
            found: str | None = None
            if rule.get("match") == "link":
                if targets is None:
                    targets = [link.target for link in self.links(doc) if link.target]
                found = next((t for t in targets if t.startswith(f"wsp:{source}/")), None)
            else:
                candidate = f"wsp:{source}/{rule.get('prefix') or ''}{doc.native_id}"
                with contextlib.suppress(Exception):
                    if self.exists(DocId(candidate)):
                        found = candidate
            if found:
                native = found.removeprefix("wsp:").split("/", 1)[1]
                out.append(
                    schemas.OtherCopy(
                        doc_id=found, source_id=source, address=address_of(source, native), why=why
                    )
                )
        return out

    def parts(self, doc: DocRecord) -> schemas.DocumentParts | None:
        """A long PDF read as its chapters (meta ``part_of`` on each, the contents in the whole's text):
        the whole, the chapters in order and this document's place, for the reader's way between them.
        The whole's text lists ``Title (page N)``; chapter N's native id is ``<whole>/pN``."""
        meta = doc.meta or {}
        whole_native = str(meta.get("part_of") or "")
        if whole_native:
            whole = self.s.documents.get(DocId(f"wsp:{doc.source_id}/{whole_native}"))
        elif "pages" in meta and _CONTENTS_HEAD in doc.text:
            whole, whole_native = doc, doc.native_id
        else:
            return None
        if whole is None:
            return None
        parts: list[schemas.PartRef] = []
        for line in whole.text.split(_CONTENTS_HEAD, 1)[-1].splitlines():
            m = _CONTENTS_LINE.match(line.strip())
            if m is None:
                continue
            native = f"{whole_native}/p{m.group(2)}"
            parts.append(
                schemas.PartRef(
                    doc_id=f"wsp:{doc.source_id}/{native}",
                    address=address_of(doc.source_id, native),
                    title=m.group(1),
                    first_page=int(m.group(2)),
                )
            )
        if not parts:
            return None
        index = next((i for i, p in enumerate(parts) if p.doc_id == doc.doc_id), None)
        return schemas.DocumentParts(
            whole=schemas.PartRef(
                doc_id=whole.doc_id,
                address=address_of(doc.source_id, whole_native),
                title=shown_title(whole.title),
                first_page=1,
            ),
            parts=parts,
            index=index,
        )

    def links(self, doc: DocRecord) -> list[DocumentLink]:
        """Outbound links, each resolved to a local document when the library holds it: in the store,
        or (at most :data:`_MAX_ZIM_LINK_CHECKS` of them) a page of another Kiwix source's current ZIM.

        A link inside the page's own archive is taken as held without a lookup: 32 lookups one after
        another made a Wikipedia page nobody had opened take 1-5 s on a cold archive (2026-10-19),
        and the Navigator asks afterwards which links lead nowhere (``POST /v1/links/missing``), all
        at once, and marks them."""
        out: list[DocumentLink] = []
        zim_checks = 0
        own = f"wsp:{doc.source_id}/"
        own_zim = self._zim_backed(DocId(own + "x"))
        for i, href in enumerate(doc.links):
            target: str | None = None
            if i < _MAX_LINK_LOOKUPS and href.startswith("wsp:"):
                try:
                    did = parse_doc(href)
                except ApiError:
                    did = None
                if did is not None and ((own_zim and did.startswith(own)) or self.in_store(did)):
                    target = did
                elif did is not None and zim_checks < _MAX_ZIM_LINK_CHECKS and self._may_stand_in(did):
                    zim_checks += 1
                    try:
                        target = did if self._zim_backed(did) and self.exists(did) else self.stand_in(did)
                    except (ApiError, ZimUnavailable):  # busy: show it unresolved rather than fail the page
                        target = None
                elif did is not None and did.startswith(f"wsp:{doc.source_id}/") and self._zim_backed(did):
                    # Past the checks: a link inside the page's own archive is taken as held (the archive
                    # wrote it); "not in library" for each of them sent readers away from pages they have.
                    target = did
            out.append(DocumentLink(href=href, target=target))
        return out

    def _zim_backed(self, did: DocId) -> bool:
        spec = self.s.source(parse_doc_id(did)[0])
        return spec is not None and spec.adapter == ZIM_ADAPTER

    def _may_stand_in(self, did: DocId) -> bool:
        """A ZIM page, or a page a rendition source may hold in its place: worth one ZIM lookup."""
        return self._zim_backed(did) or has_renditions(self.s.source(parse_doc_id(did)[0]))

    # --- history ---

    def history(self, did: DocId) -> tuple[tuple[str, str], ...]:
        """``(generation, content hash)`` versions, oldest first; cached per set of refined generations
        (reading it opens a reader per generation)."""
        generations = getattr(self.s.documents, "generations", None)
        if not callable(generations):
            return tuple(self.s.documents.history(did))
        try:
            stamp = tuple(cast(Callable[[str], Sequence[str]], generations)(parse_doc_id(did)[0]))
        except Exception:
            return tuple(self.s.documents.history(did))
        key = (did, stamp)
        with self._history_lock:
            hit = self._history.get(key)
            if hit is not None:
                self._history.move_to_end(key)
                return hit
        versions = tuple(self.s.documents.history(did))
        with self._history_lock:
            self._history[key] = versions
            while len(self._history) > _HISTORY_CACHE:
                self._history.popitem(last=False)
        return versions

    @staticmethod
    def generation_of(versions: Sequence[tuple[str, str]], content_hash: str, at: str | None) -> str | None:
        """The generation that introduced this content version (history is oldest first)."""
        found: str | None = None
        for gen, h in versions:
            if at is not None and gen > at:
                break
            if h == content_hash:
                found = gen
        return found

    def serving_generation(self, source_id: str, introduced: str | None, at: str | None) -> Generation | None:
        """The sealed generation whose files hold the version: the newest one in ``[introduced, at]``."""
        usable = (GenerationStatus.SEALED, GenerationStatus.DEGRADED)
        gens = [
            g
            for g in self.vault.generations(source_id)
            if g.status in usable
            and (introduced is None or g.generation_id >= introduced)
            and (at is None or g.generation_id <= at)
        ]
        current = self.vault.current(source_id)
        if at is None and current is not None and any(g.generation_id == current.generation_id for g in gens):
            return current
        return max(gens, key=lambda g: g.generation_id, default=None)

    # --- pages ---

    def rendered(self, source_id: str, gen: Generation, html_ref: str, native_id: str) -> Rendered | None:
        """The page ``html_ref`` of a generation, links rewritten; rendered once, then cached by bytes."""
        gid = gen.generation_id
        key = (
            source_id,
            gid,
            html_ref,
            native_id,
            str(REWRITE_VERSION),
            str(self.zim.sites_version if self.zim else 0),
        )

        def build() -> Rendered | None:
            sites: tuple[str, ...] = ()
            find: Callable[[str], str | None] | None = None
            is_image: Callable[[str], bool] | None = None
            if (zim := self.zim) is not None and html_ref.startswith("zim:"):
                page = zim.page(source_id, gid, html_ref.removeprefix("zim:"))
                if page is None:
                    return None
                base, root, original, section = page.native_id, page.book_root, page.html, page.fragment
                sites = page.site_roots
                find = partial(zim.find, source_id, gid)
                is_image = partial(zim.is_image, source_id, gid)
            else:
                found = self.assets.html(source_id, gid, html_ref)
                if found is None:
                    return None
                base, root, original, section = native_id, "", found, ""
            out = render(
                original,
                lambda h: rewrite_html(
                    h,
                    source_id=source_id,
                    generation_id=gid,
                    native_id=base,
                    root=root,
                    site_roots=sites,
                    find=find,
                    outside=self.outside_url,
                    is_image=is_image,
                ),
            )
            return replace(out, section=section) if section else out

        return self.pages.get_or_build(key, build)

    def live_zim(self, did: DocId) -> tuple[DocRecord, Rendered, Generation] | None:
        """A Kiwix page that is not refined (yet), read straight from the source's current ZIM."""
        source_id, native_id = parse_doc_id(did)
        spec = self.s.source(source_id)
        if spec is None or spec.adapter != ZIM_ADAPTER:
            return None
        try:
            gen = self.vault.current(source_id)
        except ApiError:
            raise
        except Exception:  # an unreachable or missing source is simply not browsable
            return None
        if gen is None:
            return None
        page = self.rendered(source_id, gen, f"zim:{native_id}", native_id)
        if page is None:
            return None
        doc = DocRecord(
            doc_id=did,
            source_id=spec.id,
            native_id=native_id,
            title=page.title or native_id.rsplit("/", 1)[-1].replace("_", " "),
            lang="",
            license=spec.license,
            text=page.text,
            revision=gen.generation_id,
            timestamp=release_time(gen),
            html_ref=f"zim:{native_id}",
            content_hash=page.content_hash,
            # Read from the page itself: a live page had no links in the API at all, so a reader of
            # `links` saw none on every Stack Exchange or xkcd page (2026-10-16 field exam).
            links=page_links(page.html),
        )
        return doc, page, gen

    def document(self, did: DocId, at: str | None, *, stand_in: bool = True) -> DocumentView:
        """The document (as of generation ``at``), its HTML and where it lives. Each entry is read once.
        A current document the library lacks is answered with the page that stands in for it
        (:meth:`stand_in`); the response then carries that page's own doc_id and address."""
        versions = self.history(did)
        if at is not None and not _GENERATION_LIKE.match(at):
            # 'nonsense' sorts after every dated generation, so it showed the current page under a
            # Time Machine banner (2026-10-14 field exam): say what exists instead.
            held = ", ".join(g for g, _ in versions[-12:]) or "none"
            raise ApiError(
                404, "generation_not_found", f"no generation {at!r} of {did}; versions held: {held}"
            )
        doc = self.s.documents.get(did, at_generation=at)
        html: str | None = None
        section = ""
        gen: Generation | None = None
        introduced: str | None = None
        if doc is None and at is None:
            live = self.live_zim(did)
            if live is not None:
                doc, page, gen = live
                html, section = page.html, page.section
        if doc is None and at is None and stand_in:
            alt = self.stand_in(did)
            if alt is not None:
                return self.document(alt, None, stand_in=False)
        if doc is None:
            where = f" at generation {at}" if at else ""
            raise ApiError(404, "document_not_found", f"no such document{where}: {did}")
        rendition: Rendition | None = None
        if gen is None:
            introduced = self.generation_of(versions, doc.content_hash, at)
            # A stored file names its generation (`file:<gen>/…`); a partitioned source's history holds
            # refinery set ids, which never equal a vault generation id (ADR-0010).
            gen = (
                self.stored_generation(doc)
                or self.serving_generation(doc.source_id, introduced, at)
                or (self.vault.current(doc.source_id) if doc.html_ref else None)
            )
            if doc.html_ref and gen is not None and stored_file(doc) is None:
                page = self.rendered(doc.source_id, gen, doc.html_ref, doc.native_id)
                if page is not None:
                    html, section = page.html, page.section
            if html is None and at is None:
                # e.g. the Wikipedia dump (indexed text) rendered with the Kiwix ZIM's page for its title.
                for alt in renditions(self.s.source(doc.source_id), doc, self.vault):
                    page = self.rendered(alt.source_id, alt.generation, alt.html_ref, alt.html_ref[4:])
                    if page is not None:
                        html, rendition, section = page.html, alt, page.section
                        break
        if rendition is not None:  # View Source Data describes the page on screen
            vault = self.vault_location(rendition.generation, rendition.html_ref, rendition.source_id)
        elif gen is not None:
            vault = self.vault_location(gen, doc.html_ref, doc.source_id)
        else:
            vault = None
        return DocumentView(
            doc=doc,
            generation_id=gen.generation_id if gen is not None else introduced,
            html=html,
            vault=vault,
            has_history=len(versions) > 1,
            rendition=None
            if rendition is None
            else schemas.Rendition(
                source_id=rendition.source_id,
                generation_id=rendition.generation.generation_id,
                doc_id=rendition_doc_id(rendition),
            ),
            section=section or None,
        )

    def stored_generation(self, doc: DocRecord) -> Generation | None:
        """The generation a stored original (``file:<gen>/…``) lives in, if the vault has it."""
        where = stored_file(doc)
        if where is None:
            return None
        return next((g for g in self.vault.generations(doc.source_id) if g.generation_id == where[0]), None)

    def vault_location(self, gen: Generation, html_ref: str | None, source_id: str) -> VaultLocation:
        """The file that holds the page: the stored original, the ZIM archive holding the entry, or the
        generation's only file. Otherwise the generation directory (no size or hashes)."""
        gen = full_generation(self.vault, gen)  # the snapshot's lists leave artifacts out
        art: Artifact | None = None
        entry: str | None = None
        if html_ref is not None and html_ref.startswith("zim:"):
            entry = html_ref.removeprefix("zim:")
            file = None if self.zim is None else self.zim.file_of(source_id, gen.generation_id, entry)
            if file is not None and Path(file).parent.name == "upstream":
                # A bundle release reads unchanged books from the release that stored them.
                holder = Path(file).parent.parent.name
                if holder != gen.generation_id:
                    gen = full_generation(
                        self.vault,
                        next(
                            (g for g in self.vault.generations(source_id) if g.generation_id == holder), gen
                        ),
                    )
            zims = [a for a in gen.artifacts if a.relpath.suffix == ".zim"]
            if file is not None:
                art = next((a for a in zims if a.relpath.name == Path(file).name), None)
            elif len(zims) == 1:
                art = zims[0]
        elif html_ref is not None and html_ref.startswith(FILE_REF):
            rel = html_ref.removeprefix(FILE_REF).partition("/")[2]
            art = next((a for a in gen.artifacts if str(a.relpath) == rel), None)
        elif len(gen.artifacts) == 1:
            art = gen.artifacts[0]
        base = self._vault_rel(gen)
        return VaultLocation(
            generation_id=gen.generation_id,
            path=str(base / str(art.relpath)) if art is not None else str(base),
            manifest=str(base / MANIFEST_NAME),
            entry=entry,
            size=None if art is None else art.size,
            sha256=None if art is None else art.sha256,
            blake3=None if art is None else art.blake3,
        )

    def _vault_rel(self, gen: Generation) -> PurePosixPath:
        """Generation directory relative to the vault root. Never an absolute path (no host details)."""
        path = self.vault.generation_path(gen.source_id, gen.generation_id)
        # TODO(port): Vault should expose the relative path; FileVault has `root`, the port does not.
        root: object = getattr(self.vault, "root", None)
        if isinstance(root, Path):
            try:
                return PurePosixPath(path.relative_to(root).as_posix())
            except ValueError:
                pass
        parts = path.parts
        if "library" in parts:
            i = len(parts) - 1 - parts[::-1].index("library")
            return PurePosixPath(*parts[i:])
        return PurePosixPath(gen.source_id, gen.generation_id)
