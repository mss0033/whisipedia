"""Routes for a document's stored original (see :mod:`wsp_api.originals`)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast
from urllib.parse import unquote

from fastapi import APIRouter
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from starlette.types import Receive, Scope, Send

from wsp_api.errors import ApiError, error_response, errors
from wsp_api.offload import RETRY_AFTER, Offload
from wsp_api.originals import INLINE, MEDIA_TYPES, PREVIEW_SIZE, PREVIEWABLE, display_name, stored_file
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
# own Download or the viewer's next range got "busy" (2026-10-18 audit, 7 of the first 46 pages).
TRANSFER_WAIT = 30.0


class _BoundedFile(FileResponse):
    """A :class:`FileResponse` that takes a transfer slot for as long as it streams, or answers 503."""

    def __init__(self, slots: asyncio.Semaphore, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._slots = slots

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await asyncio.wait_for(self._slots.acquire(), TRANSFER_WAIT)
        except TimeoutError:
            busy = error_response(
                503, "busy", "too many files are being sent; try again shortly", retry_after=RETRY_AFTER
            )
            await busy(scope, receive, send)
            return
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._slots.release()


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

    return r


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
