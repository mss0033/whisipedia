"""Read-side helpers shared by the routes: the services, the library snapshot and the defaults for
unwired seams."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from wsp_api import convert, schemas
from wsp_api.addresses import address_of
from wsp_api.catalog import FeedCatalog
from wsp_api.errors import ApiError
from wsp_api.htmlrewrite import VERSION as REWRITE_VERSION
from wsp_api.htmlrewrite import rewrite_html
from wsp_api.offload import Offload
from wsp_api.originals import FILE_REF, stored_file
from wsp_api.pages import ByteLRU, Rendered, render
from wsp_api.renditions import Rendition, release_time, rendition_doc_id, renditions
from wsp_api.schemas import Change, DocRef, DocumentLink, SourceInfo, VaultLocation
from wsp_api.seams import DocumentFields, VaultView
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
)
from wsp_core.ports import AssetStore, Catalog

log = logging.getLogger(__name__)
MANIFEST_NAME = "MANIFEST.json"  # same name as wsp_vault.manifest.MANIFEST_NAME
ZIM_ADAPTER = "kiwix-opds"
BOOK_INDEX_FILE = "zim-books.json"
_MAX_LINK_LOOKUPS = 1000
_MAX_ZIM_LINK_CHECKS = 32  # each opens an archive entry (no content read)
_HISTORY_CACHE = 2048
HIT_COLUMNS = ("license", "timestamp", "lang", "url_native")


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
        self._history: OrderedDict[tuple[DocId, tuple[str, ...]], tuple[tuple[str, str], ...]] = OrderedDict()
        self._history_lock = threading.Lock()
        self._fields = services.documents if isinstance(services.documents, DocumentFields) else None
        lookup = getattr(services.index, "lookup", None)
        self._index_lookup = cast(Callable[[str], SearchHit | None], lookup) if callable(lookup) else None

    @property
    def vault(self) -> VaultView:
        """The vault as requests see it: the in-memory snapshot in the server, else the vault itself."""
        return self.state.vault

    def close(self) -> None:
        self.state.stop()
        self.nas.close()
        self.searches.close()
        if self.zim is not None:
            self.zim.close()

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
        return convert.change(rec, title=self.title(rec.doc_id))

    def links(self, doc: DocRecord) -> list[DocumentLink]:
        """Outbound links, each resolved to a local document when the library holds it: in the store,
        or (at most :data:`_MAX_ZIM_LINK_CHECKS` of them) a page of a Kiwix source's current ZIM."""
        out: list[DocumentLink] = []
        zim_checks = 0
        for i, href in enumerate(doc.links):
            target: str | None = None
            if i < _MAX_LINK_LOOKUPS and href.startswith("wsp:"):
                try:
                    did = parse_doc(href)
                except ApiError:
                    did = None
                if did is not None and self.in_store(did):
                    target = did
                elif did is not None and zim_checks < _MAX_ZIM_LINK_CHECKS and self._zim_backed(did):
                    zim_checks += 1
                    try:
                        target = did if self.exists(did) else None
                    except (ApiError, ZimUnavailable):  # busy: show it unresolved rather than fail the page
                        target = None
            out.append(DocumentLink(href=href, target=target))
        return out

    def _zim_backed(self, did: DocId) -> bool:
        spec = self.s.source(parse_doc_id(did)[0])
        return spec is not None and spec.adapter == ZIM_ADAPTER

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
        key = (source_id, gid, html_ref, native_id, str(REWRITE_VERSION))

        def build() -> Rendered | None:
            if self.zim is not None and html_ref.startswith("zim:"):
                page = self.zim.page(source_id, gid, html_ref.removeprefix("zim:"))
                if page is None:
                    return None
                base, root, original = page.native_id, page.book_root, page.html
            else:
                found = self.assets.html(source_id, gid, html_ref)
                if found is None:
                    return None
                base, root, original = native_id, "", found
            return render(
                original,
                lambda h: rewrite_html(h, source_id=source_id, generation_id=gid, native_id=base, root=root),
            )

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
        )
        return doc, page, gen

    def document(self, did: DocId, at: str | None) -> DocumentView:
        """The document (as of generation ``at``), its HTML and where it lives. Each entry is read once."""
        versions = self.history(did)
        doc = self.s.documents.get(did, at_generation=at)
        html: str | None = None
        gen: Generation | None = None
        introduced: str | None = None
        if doc is None and at is None:
            live = self.live_zim(did)
            if live is not None:
                doc, page, gen = live
                html = page.html
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
                html = None if page is None else page.html
            if html is None and at is None:
                # e.g. the Wikipedia dump (indexed text) rendered with the Kiwix ZIM's page for its title.
                for alt in renditions(self.s.source(doc.source_id), doc, self.vault):
                    page = self.rendered(alt.source_id, alt.generation, alt.html_ref, alt.html_ref[4:])
                    if page is not None:
                        html, rendition = page.html, alt
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
        base = self._vault_rel(gen)
        art: Artifact | None = None
        entry: str | None = None
        if html_ref is not None and html_ref.startswith("zim:"):
            entry = html_ref.removeprefix("zim:")
            zims = [a for a in gen.artifacts if a.relpath.suffix == ".zim"]
            if len(zims) == 1:
                art = zims[0]
            elif zims and self.zim is not None:
                file = self.zim.file_of(source_id, gen.generation_id, entry)
                name = None if file is None else Path(file).name
                art = next((a for a in zims if a.relpath.name == name), None)
        elif html_ref is not None and html_ref.startswith(FILE_REF):
            rel = html_ref.removeprefix(FILE_REF).partition("/")[2]
            art = next((a for a in gen.artifacts if str(a.relpath) == rel), None)
        elif len(gen.artifacts) == 1:
            art = gen.artifacts[0]
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
