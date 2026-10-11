"""Read-side helpers shared by the routes: the services plus the defaults for unwired seams."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from wsp_api import convert
from wsp_api.catalog import FeedCatalog
from wsp_api.errors import ApiError
from wsp_api.renditions import zim_document
from wsp_api.schemas import Change, DocumentLink, SourceInfo, VaultLocation
from wsp_api.services import Services
from wsp_api.zim import ZimAssetStore
from wsp_core.ids import DocId, InvalidDocIdError, make_doc_id, parse_doc_id
from wsp_core.model import ChangeRecord, DocRecord, Generation, GenerationStatus, SourceSpec
from wsp_core.ports import AssetStore, Catalog

log = logging.getLogger(__name__)
MANIFEST_NAME = "MANIFEST.json"  # same name as wsp_vault.manifest.MANIFEST_NAME
_MAX_LINK_LOOKUPS = 1000


def parse_doc(raw: str) -> DocId:
    try:
        source_id, native_id = parse_doc_id(raw)
        return make_doc_id(source_id, native_id)
    except InvalidDocIdError as exc:
        raise ApiError(400, "invalid_doc_id", str(exc)) from exc


class Library:
    def __init__(self, services: Services, *, info_seconds: float = 0.0) -> None:
        self.s = services
        self._info_seconds = info_seconds
        self._infos: dict[str, tuple[float, SourceInfo]] = {}
        self._refreshing: set[str] = set()
        self._infos_lock = threading.Lock()
        self.catalog: Catalog = services.catalog or FeedCatalog(services.changes)
        # TODO(integration): wire both in wsp_cli.bootstrap; these defaults keep `wsp serve` working.
        self.assets: AssetStore = services.assets or ZimAssetStore(services.vault)
        if isinstance(self.assets, ZimAssetStore):
            self.assets.names = {x.id: x.name for x in services.sources}
        self._homes: dict[tuple[str, str], str | None] = {}

    # --- sources ---

    def source(self, source_id: str) -> SourceSpec:
        spec = self.s.source(source_id)
        if spec is None:
            raise ApiError(404, "source_not_found", f"unknown source: {source_id}")
        return spec

    def source_info(self, spec: SourceSpec) -> SourceInfo:
        """A site's card. With ``info_seconds`` set, a card is reused that long and then refreshed in
        the background while the old one is served: each card costs NAS reads, and every Navigator
        page needs the site list, so a busy NAS must not stall browsing (E2E-2)."""
        if self._info_seconds <= 0:
            return self._fresh_info(spec)
        with self._infos_lock:
            hit = self._infos.get(spec.id)
            stale = hit is not None and time.monotonic() - hit[0] > self._info_seconds
            refresh = stale and spec.id not in self._refreshing
            if refresh:
                self._refreshing.add(spec.id)
        if hit is None:
            info = self._fresh_info(spec)
            with self._infos_lock:
                self._infos[spec.id] = (time.monotonic(), info)
            return info
        if refresh:
            threading.Thread(target=self._refresh_info, args=(spec,), name="source-info", daemon=True).start()
        return hit[1]

    def _refresh_info(self, spec: SourceSpec) -> None:
        try:
            info = self._fresh_info(spec)
            with self._infos_lock:
                self._infos[spec.id] = (time.monotonic(), info)
        except Exception:  # keep serving the old card; the next request retries
            log.warning("could not refresh the card of %s", spec.id, exc_info=True)
        finally:
            with self._infos_lock:
                self._refreshing.discard(spec.id)

    def _fresh_info(self, spec: SourceSpec) -> SourceInfo:
        current = self.s.vault.current(spec.id)
        return convert.source_info(
            spec,
            current,
            specs=self.s.sources,
            now=self.s.clock(),
            doc_count=self.catalog.doc_count(spec.id),
            home_doc_id=self.home_doc_id(spec, current),
        )

    def home_doc_id(self, spec: SourceSpec, current: Generation | None) -> str | None:
        """Landing page of a Kiwix site (ZIM main page, or the book list of a bundle); cached."""
        if current is None or spec.adapter != "kiwix-opds" or not isinstance(self.assets, ZimAssetStore):
            return None
        key = (spec.id, current.generation_id)
        if key not in self._homes:
            try:
                native = self.assets.home(spec.id, current.generation_id)
            except Exception:
                native = None
            self._homes[key] = None if native is None else make_doc_id(spec.id, native)
        return self._homes[key]

    def source_infos(self) -> list[SourceInfo]:
        return [self.source_info(x) for x in sorted(self.s.sources, key=lambda x: x.id)]

    def source_name(self, source_id: str) -> str:
        spec = self.s.source(source_id)
        return source_id if spec is None else spec.name

    # --- documents & changes ---

    def exists(self, did: DocId) -> bool:
        """In the document store, or (Kiwix sources) a page of the current ZIM."""
        if self.s.documents.get(did) is not None:
            return True
        source_id, native_id = parse_doc_id(did)
        try:
            return zim_document(self.s.source(source_id), native_id, self.s.vault, self.assets) is not None
        except Exception:  # an unreadable archive is "not here", not an error page
            return False

    def title(self, did: str) -> str | None:
        try:
            doc = self.s.documents.get(parse_doc(did))
        except ApiError:
            return None
        return None if doc is None else doc.title

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
                if did is not None and self.s.documents.get(did) is not None:
                    target = did
            out.append(DocumentLink(href=href, target=target))
        return out

    def generation_of(self, did: DocId, content_hash: str, at: str | None) -> str | None:
        """The generation that introduced this content version (history is oldest first)."""
        found: str | None = None
        for gen, h in self.s.documents.history(did):
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
            for g in self.s.vault.generations(source_id)
            if g.status in usable
            and (introduced is None or g.generation_id >= introduced)
            and (at is None or g.generation_id <= at)
        ]
        current = self.s.vault.current(source_id)
        if at is None and current is not None and any(g.generation_id == current.generation_id for g in gens):
            return current
        return max(gens, key=lambda g: g.generation_id, default=None)

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
        path = self.s.vault.generation_path(gen.source_id, gen.generation_id)
        # TODO(port): Vault should expose the relative path; FileVault has `root`, the port does not.
        root: object = getattr(self.s.vault, "root", None)
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

    def has_docs(self, sources: Sequence[str]) -> list[str]:
        return [x for x in sources if self.catalog.doc_count(x)]
