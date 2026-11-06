"""Routes for a document's stored original (see :mod:`wsp_api.originals`)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import threading
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import IO, TYPE_CHECKING, Annotated, Any, cast
from urllib.parse import quote, unquote

import anyio
from fastapi import APIRouter, Query, Response
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from wsp_api import archive
from wsp_api.errors import ApiError, error_response, errors
from wsp_api.offload import RETRY_AFTER, Offload
from wsp_api.originals import (
    ARCHIVES,
    INLINE,
    MEDIA_TYPES,
    PREVIEW_SIZE,
    PREVIEWABLE,
    display_name,
    stored_file,
)
from wsp_api.schemas import ArchiveListing, ArchiveView
from wsp_api.views import Library, parse_doc
from wsp_core.model import DocRecord

if TYPE_CHECKING:
    from PIL import Image

DocIdSegment = Annotated[
    str, PathParam(description="`wsp:<source_id>/<native_id>`, percent-encoded as one segment.")
]


MAX_TRANSFERS = 8  # concurrent originals: a stalled NAS read holds a thread of Starlette's shared pool
# How long a transfer waits for a slot before the 503. Waiting holds no thread (an asyncio semaphore).
# A browser's PDF viewer asks for several ranges of one file at once: refused at once, the reader's
# own Download or the viewer's next range got "busy" (2026-10-18 audit, 7 of the first 46 pages). A
# browser waits minutes for a download to start; 30 s was too short while big ingests held the NAS
# (an OpenStax page's Download, 2026-10-21 audit).
TRANSFER_WAIT = 120.0
MEMBER_CHUNK = 1024 * 1024  # an archive member is inflated and sent this much at a time
LISTING_TIMEOUT = 60.0  # a 200,000-member directory is ~20 MB read from the NAS

MemberPath = Annotated[str, Query(min_length=1, max_length=4096, description="The member's `path`.")]

log = logging.getLogger(__name__)


async def _take_slot(slots: asyncio.Semaphore, scope: Scope, receive: Receive, send: Send) -> bool:
    """Wait up to :data:`TRANSFER_WAIT` for a transfer slot; without one, answer 503 and say so."""
    try:
        await asyncio.wait_for(slots.acquire(), TRANSFER_WAIT)
    except TimeoutError:
        busy = error_response(
            503, "busy", "too many files are being sent; try again shortly", retry_after=RETRY_AFTER
        )
        await busy(scope, receive, send)
        return False
    return True


class _BoundedFile(FileResponse):
    """A :class:`FileResponse` that takes a transfer slot for as long as it streams, or answers 503."""

    def __init__(self, slots: asyncio.Semaphore, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._slots = slots

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not await _take_slot(self._slots, scope, receive, send):
            return
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._slots.release()


def _close_member(zf: zipfile.ZipFile, fh: IO[bytes]) -> None:
    try:
        fh.close()
    finally:
        zf.close()


class _BoundedMember(StreamingResponse):
    """One archive member, inflated as it is sent, holding a transfer slot like :class:`_BoundedFile`.
    The member and its archive are closed when the response ends, however it ends (sent, refused
    busy, the reader cancelling the download, a CRC mismatch breaking it off)."""

    def __init__(
        self, slots: asyncio.Semaphore, zf: zipfile.ZipFile, fh: IO[bytes], name: str, **kwargs: Any
    ) -> None:
        self._slots = slots
        self._zf = zf
        self._fh = fh
        self._name = name
        super().__init__(self._chunks(), **kwargs)

    async def _chunks(self) -> AsyncIterator[bytes]:
        while True:
            try:
                block = await anyio.to_thread.run_sync(self._fh.read, MEMBER_CHUNK)
            except zipfile.BadZipFile as exc:  # its CRC did not match: break the download off
                log.warning("archive member %s is damaged: %s", self._name, exc)
                raise
            if not block:
                return
            yield block

    def _close(self) -> None:
        _close_member(self._zf, self._fh)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            if not await _take_slot(self._slots, scope, receive, send):
                return
            try:
                await super().__call__(scope, receive, send)
            finally:
                self._slots.release()
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self._close)


def files_router(lib: Library) -> APIRouter:
    r = APIRouter(prefix="/v1")
    transfers = asyncio.Semaphore(MAX_TRANSFERS)
    private_cache: list[Path] = []  # without a cache dir: one private temporary directory per router

    def preview_dir() -> Path:
        if lib.settings.cache_dir is not None:
            return lib.settings.cache_dir / "previews"
        if not private_cache:
            private_cache.append(Path(tempfile.mkdtemp(prefix="wsp-previews-")))
        return private_cache[0]

    # One decode at a time, in its own pool: a 100-megapixel map scan takes seconds and must not hold
    # a `nas` slot that document reads need. Requests beyond the small queue are shed with a 503.
    previews = Offload(workers=1, queue=4, timeout=120.0, name="preview")

    def locate(doc_id: str) -> tuple[DocRecord, Path]:
        did = parse_doc(unquote(doc_id))
        doc = lib.s.documents.get(did)
        if doc is None:
            raise ApiError(404, "document_not_found", f"no such document: {did}")
        where = stored_file(doc)
        if where is None:
            raise ApiError(404, "no_original", "this document has no stored original file")
        generation, rel = where
        root = Path(lib.vault.generation_path(doc.source_id, generation))
        path = root / rel
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError:
            raise ApiError(404, "no_original", "the stored file is outside its generation") from None
        if not path.is_file():
            raise ApiError(404, "no_original", "the stored file is not in the vault")
        return doc, path

    @r.head("/files/{doc_id}", include_in_schema=False)
    @r.get(
        "/files/{doc_id}", tags=["documents"], response_class=FileResponse, responses=errors(400, 404, 503)
    )
    async def original(doc_id: DocIdSegment) -> FileResponse:
        """The document's stored original file (PDF, map scan, archive). Supports ``Range``, and ``HEAD``
        (headers only: a download manager asks for the size first; it was a 404, 2026-10-19 audit)."""
        _doc, path = await lib.nas.run(lambda: locate(doc_id))
        suffix = path.suffix.lower()
        return _BoundedFile(
            transfers,
            path,
            media_type=MEDIA_TYPES.get(suffix, "application/octet-stream"),
            filename=display_name(path.name),
            content_disposition_type="inline" if suffix in INLINE else "attachment",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @r.get(
        "/files/{doc_id}/preview", tags=["documents"], response_class=FileResponse, responses=errors(404, 503)
    )
    async def preview(doc_id: DocIdSegment) -> FileResponse:
        """A JPEG of a scanned map or image, at most 2000 pixels on its long side (cached)."""

        def find() -> tuple[Path, Path]:
            _doc, path = locate(doc_id)
            if path.suffix.lower() not in PREVIEWABLE:
                raise ApiError(404, "no_preview", "only images and map scans have a preview")
            st = path.stat()
            key = hashlib.sha256(f"{path}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:32]
            return path, preview_dir() / f"{key}.jpg"

        src, out = await lib.nas.run(find)
        if not out.is_file():  # a cached preview never waits behind a render
            await previews.run(lambda: out if out.is_file() else _render_preview(src, out))
        return FileResponse(
            out, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"}
        )

    listings = archive.Listings()
    # A metadata XML file is parsed whole (gNATSGO's is 7 MB): one at a time, like previews.
    views = Offload(workers=1, queue=4, timeout=120.0, name="archive-view")

    def locate_archive(doc_id: str) -> tuple[DocRecord, Path]:
        doc, path = locate(doc_id)
        if path.suffix.lower() not in ARCHIVES:
            raise ApiError(404, "not_an_archive", "only zip archives can be listed")
        return doc, path

    @r.get(
        "/files/{doc_id}/archive",
        tags=["documents"],
        response_model=ArchiveListing,
        responses=errors(400, 404, 413, 422, 503),
    )
    async def archive_listing(
        doc_id: DocIdSegment,
        response: Response,
        folder: Annotated[
            str, Query(max_length=4096, description='List only this folder and those under it ("a/b/").')
        ] = "",
        after: Annotated[
            str | None,
            Query(
                min_length=1,
                max_length=4096,
                description="The `next` of the page before: list from where it stopped.",
            ),
        ] = None,
    ) -> ArchiveListing:
        """What a stored zip archive holds, grouped by folder with sizes, read from its central
        directory: nothing is unpacked. At most 2,000 files a page (``truncated``, ``next`` for the
        page after with ``after=``); ``folder=`` lists one folder and those under it."""

        def run() -> ArchiveListing:
            doc, path = locate_archive(doc_id)
            base = f"/v1/files/{quote(doc.doc_id, safe='')}/archive"
            return archive.listing(
                display_name(path.name),
                listings.index(path, folder),  # kept for the next page (2026-11-06 review)
                folder,
                member_url=f"{base}/member",
                view_url=f"{base}/view",
                after=after,
            )

        out = await lib.nas.run(run, timeout=LISTING_TIMEOUT)
        response.headers["Cache-Control"] = "private, max-age=86400"
        return out

    @r.get(
        "/files/{doc_id}/archive/view",
        tags=["documents"],
        response_model=ArchiveView,
        responses=errors(404, 409, 413, 415, 422, 503),
    )
    async def archive_view(doc_id: DocIdSegment, path: MemberPath, response: Response) -> ArchiveView:
        """A text file in a stored archive (its first 512 KB), or an ISO 19115 / ISO 19139 / ISO 19110
        / FGDC metadata XML file in it read into sections and a data dictionary."""

        def run() -> ArchiveView:
            with archive.open_archive(locate_archive(doc_id)[1]) as zf:
                return archive.read_view(zf, path)

        out = await views.run(run)
        response.headers["Cache-Control"] = "private, max-age=86400"
        return out

    @r.get(
        "/files/{doc_id}/archive/member",
        tags=["documents"],
        response_class=StreamingResponse,
        responses=errors(404, 409, 413, 415, 422, 503),
    )
    async def archive_member(doc_id: DocIdSegment, path: MemberPath) -> StreamingResponse:
        """One file of a stored archive, unpacked as it is sent, always as a download. A damaged file
        (its CRC does not match) breaks off before its end, so the browser says the download failed."""

        # The archive is opened on a NAS thread and handed over here. Past the deadline (or with the
        # reader gone) nobody would close it: whichever side comes second closes it.
        handoff = threading.Lock()
        state: dict[str, Any] = {}

        def run() -> tuple[zipfile.ZipFile, zipfile.ZipInfo, IO[bytes]]:
            zf = archive.open_archive(locate_archive(doc_id)[1])
            try:
                info = archive.member_info(zf, path)
                opened = (zf, info, archive.open_member(zf, info))
            except BaseException:
                zf.close()
                raise
            with handoff:
                if not state.get("abandoned"):
                    state["opened"] = opened
                    return opened
            _close_member(opened[0], opened[2])
            raise ApiError(
                503, "timeout", "the library storage did not answer in time", retry_after=RETRY_AFTER
            )

        try:
            zf, info, fh = await lib.nas.run(run)
        except BaseException:
            with handoff:
                state["abandoned"] = True
                late = state.pop("opened", None)
            if late is not None:
                _close_member(late[0], late[2])
            raise
        name = info.filename.rsplit("/", 1)[-1] or "file"
        return _BoundedMember(
            transfers,
            zf,
            fh,
            info.filename,
            # Never typed by its name: a member's HTML or SVG must not render at our origin, and a
            # 2 GB CSV must not go through gzip in the API process (2026-10-26 review #117).
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(info.file_size),
                "Content-Disposition": attachment(name),
                "Cache-Control": "private, max-age=86400",
            },
        )

    return r


def attachment(name: str) -> str:
    """``Content-Disposition`` of a download named ``name``: an ASCII fallback, then the UTF-8 name."""
    plain = "".join(c if " " <= c <= "~" and c not in '"\\' else "_" for c in name)
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"


def _render_preview(src: Path, out: Path) -> Path:
    from PIL import Image  # imported on use: only previews need it

    Image.MAX_IMAGE_PIXELS = 400_000_000  # USGS map scans run to ~100 megapixels; still a bomb guard
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.name}.{os.getpid()}.tmp")
    try:
        img = _pdf_first_page(src) if src.suffix.lower() == ".pdf" else Image.open(src)
        with img:
            img.draft("RGB", (PREVIEW_SIZE, PREVIEW_SIZE))  # JPEG: decode at a reduced scale
            img.thumbnail((PREVIEW_SIZE, PREVIEW_SIZE))
            img.convert("RGB").save(tmp, "JPEG", quality=85, optimize=True)
        tmp.replace(out)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ApiError(404, "no_preview", f"the file could not be previewed: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    return out


def _pdf_first_page(src: Path) -> Image.Image:
    """Page 1 rendered so its long side is :data:`PREVIEW_SIZE` (a USGS map sheet is one page)."""
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(src)
    except pdfium.PdfiumError as exc:
        raise ValueError(f"unreadable PDF: {exc}") from exc
    try:
        if len(pdf) == 0:
            raise ValueError("the PDF has no pages")
        page = pdf[0]
        width, height = page.get_size()  # points
        scale = min(PREVIEW_SIZE / max(width, height, 1.0), 4.0)
        bitmap = page.render(scale=scale)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        return cast("Image.Image", bitmap.to_pil())  # pyright: ignore[reportUnknownMemberType]
    finally:
        pdf.close()
