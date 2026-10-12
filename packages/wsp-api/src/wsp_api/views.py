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

from wsp_api import convert
from wsp_api.catalog import FeedCatalog
from wsp_api.errors import ApiError
from wsp_api.htmlrewrite import VERSION as REWRITE_VERSION
from wsp_api.htmlrewrite import rewrite_html
from wsp_api.offload import Offload
from wsp_api.pages import ByteLRU, Rendered, render
from wsp_api.renditions import borrowed
from wsp_api.schemas import Change, DocumentLink, SourceInfo, VaultLocation
from wsp_api.seams import DocumentFields, VaultView
from wsp_api.services import ApiSettings, Services
from wsp_api.snapshot import LibrarySnapshot, LibraryState, StateSettings
from wsp_api.zim import BookIndex, ZimAssetStore
from wsp_api.zimpool import ZimProcessPool
from wsp_api.zimread import InProcessZimReader, ZimReader
from wsp_core.ids import DocId, InvalidDocIdError, make_doc_id, parse_doc_id
from wsp_core.model import ChangeRecord, DocRecord, Generation, GenerationStatus, SearchHit, SourceSpec
from wsp_core.ports import AssetStore, Catalog

log = logging.getLogger(__name__)
MANIFEST_NAME = "MANIFEST.json"  # same name as wsp_vault.manifest.MANIFEST_NAME
ZIM_ADAPTER = "kiwix-opds"
BOOK_INDEX_FILE = "zim-books.json"
_MAX_LINK_LOOKUPS = 1000
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


DEFAULT_ZIM_MAX_OPEN = 512


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
        return convert.source_info(
            spec,
            st.current,
            specs=self.s.sources,
            now=self.s.clock(),
            doc_count=snap.doc_counts.get(spec.id),
            home_doc_id=self.state.home_doc_id(spec, st.current),
            members=st.members if spec.partitioned else (),
        )

    def source_infos(self, snap: LibrarySnapshot | None = None) -> list[SourceInfo]:
        snap = snap or self.state.snapshot()
        return [self.source_info(x, snap) for x in sorted(self.s.sources, key=lambda x: x.id)]

    def source_name(self, source_id: str) -> str:
        spec = self.s.source(source_id)
        return source_id if spec is None else spec.name

    def readable_sources(self) -> frozenset[str]:
        """Sites a reader can open: held (a current generation) with documents or a home page.

        A planned source with nothing downloaded, or raw data the reader cannot show (a map tile
        planet, a Wikidata dump) until it is refined, is not listed on the portal or in the directory.
        """
        return frozenset(
            i.id for i in self.source_infos() if i.current_generation and (i.doc_count or i.home_doc_id)
        )

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
        except ApiError:
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
        out: list[DocumentLink] = []
        for i, href in enumerate(doc.links):
            target: str | None = None
            if i < _MAX_LINK_LOOKUPS and href.startswith("wsp:"):
                try:
                    did = parse_doc(href)
                except ApiError:
                    did = None
                if did is not None and self.in_store(did):
                    target = did
            out.append(DocumentLink(href=href, target=target))
        return out

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
            timestamp=gen.sealed_at,
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
        if gen is None:
            introduced = self.generation_of(versions, doc.content_hash, at)
            gen = self.serving_generation(doc.source_id, introduced, at) or (
                self.vault.current(doc.source_id) if doc.html_ref else None
            )
            if doc.html_ref and gen is not None:
                page = self.rendered(doc.source_id, gen, doc.html_ref, doc.native_id)
                html = None if page is None else page.html
            spec = self.s.source(doc.source_id)
            if html is None and at is None and (alt := borrowed(spec, doc, self.vault)):
                # e.g. the Wikipedia dump (indexed text) rendered with the Kiwix ZIM's page for its title.
                page = self.rendered(alt.source_id, alt.generation, alt.html_ref, alt.html_ref[4:])
                html = None if page is None else page.html
        return DocumentView(
            doc=doc,
            generation_id=gen.generation_id if gen is not None else introduced,
            html=html,
            vault=None if gen is None else self.vault_location(gen, doc),
            has_history=len(versions) > 1,
        )

    def vault_location(self, gen: Generation, doc: DocRecord) -> VaultLocation:
        base = self._vault_rel(gen)
        zim = doc.html_ref is not None and doc.html_ref.startswith("zim:")
        candidates = [a for a in gen.artifacts if a.relpath.suffix == ".zim"] if zim else list(gen.artifacts)
        art = candidates[0] if len(candidates) == 1 or (zim and candidates) else None
        return VaultLocation(
            generation_id=gen.generation_id,
            path=str(base / str(art.relpath)) if art is not None else str(base),
            manifest=str(base / MANIFEST_NAME),
            entry=doc.html_ref.removeprefix("zim:") if zim and doc.html_ref else None,
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
