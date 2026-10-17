"""Navigator resources: start page, directory, random page, document assets and gap reports (PLAN §10.2)."""

from __future__ import annotations

import ipaddress
import random
import secrets
from collections.abc import Callable, Sequence
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Query, Request, Response

from wsp_api import sites
from wsp_api.addresses import address_of
from wsp_api.errors import ApiError, errors
from wsp_api.schemas import (
    Change,
    DirectoryCategory,
    DirectoryCrumb,
    DirectoryEntry,
    DirectoryPage,
    DocKind,
    DocRef,
    FeaturedDoc,
    GapReport,
    OnThisDay,
    OutsideCopy,
    Portal,
    SourceInfo,
)
from wsp_api.views import Library
from wsp_core.ids import DocId, parse_doc_id
from wsp_core.model import ChangeKind, ChangeRecord, Gap, SourceSpec
from wsp_core.ports import PortalSource

# Only images are served from containers: never HTML or scripts at our origin.
SERVED_MIMETYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/bmp",
        "image/svg+xml",
        "image/x-icon",
        "image/vnd.microsoft.icon",
    }
)
# A page, a script or a stylesheet is never an asset: the API serves pages through the page route, and
# code from an upstream container must not run (or style) at our origin. Every other type a page links
# to (htmlrewrite.FILES: PDFs, EPUBs, sheet music, recordings, data) is sent as a download.
REFUSED_MIMETYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/css",
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "text/ecmascript",
        "application/wasm",
    }
)
_BLURB = 280


def _blurb(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= _BLURB:
        return flat
    cut = flat[:_BLURB].rsplit(" ", 1)[0]
    return cut + "…"


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


CATEGORY_LIMIT = 200  # entries per directory category page; the catalog keeps a bounded list anyway


def browse_router(lib: Library) -> APIRouter:
    r = APIRouter(prefix="/v1")
    s = lib.s

    def doc_ref(did: DocId | None) -> DocRef | None:
        """A link to a document by its title: a projected lookup, never the article text (audit #9)."""
        title = None if did is None else lib.title(did)
        if did is None or title is None:
            return None
        source_id, native_id = parse_doc_id(did)
        return DocRef(doc_id=did, source_id=source_id, title=title, address=address_of(source_id, native_id))

    # --- Portal ---------------------------------------------------------------------------------

    @r.get("/portal", tags=["navigator"], responses=errors(503))
    async def portal() -> Portal:
        # The random pick and the featured document read the Refinery (the NAS since ADR-0011).
        return await lib.nas.run(_portal)

    def _portal() -> Portal:
        today = s.clock().date()
        portal_source = lib.catalog if isinstance(lib.catalog, PortalSource) else None
        pick = portal_source.featured(today) if portal_source is not None else None
        if pick is None:  # no precomputed picks: a random encyclopedia page, fixed for the day
            encyclopedic = [x.id for x in s.sources if sites.kind_of(x) is DocKind.ENCYCLOPEDIA]
            pick = lib.catalog.random_doc(
                sources=encyclopedic or None,
                rng=random.Random(today.toordinal()),  # noqa: S311 - not security
            )
        featured_doc = None if pick is None else s.documents.get(pick)
        featured = (
            None
            if featured_doc is None
            else FeaturedDoc(
                doc_id=featured_doc.doc_id,
                title=featured_doc.title,
                address=address_of(featured_doc.source_id, featured_doc.native_id),
                blurb=_blurb(featured_doc.text),
            )
        )
        newest = _presentable_recent(lib, limit=8)
        rng = random.Random(secrets.randbits(64))  # noqa: S311 - picking a page to read, not a secret
        infos = lib.source_infos()  # from the library snapshot
        return Portal(
            date=today,
            featured=featured,
            on_this_day=[
                OnThisDay(year=e.year, text=e.text, doc_id=e.doc_id)
                for e in (portal_source.on_this_day(today) if portal_source is not None else ())
            ],
            did_you_know=[],  # TODO: Wikipedia "Did you know" pages are not in the dumps or ZIMs
            newest=(
                news := _site_news(
                    [i for i in infos if lib.is_readable(i)],
                    limit=6,
                    is_new=lambda sid: len(lib.vault.generations(sid)) <= 1,
                )
            )
            + [lib.change(c) for c in newest][: 8 - len(news)],
            random=doc_ref(lib.catalog.random_doc(sources=None, rng=rng)),
            random_sources=[i.id for i in infos if i.doc_count],
            sites=[i for i in infos if lib.is_readable(i)],
        )

    # --- Directory ------------------------------------------------------------------------------

    @r.get("/directory", tags=["navigator"], responses=errors(404, 503))
    async def directory(
        path: Annotated[
            str, Query(max_length=500, description="`''` (Top), `<domain>`, `<domain>/<source>`, …")
        ] = "",
    ) -> DirectoryPage:
        # A category page looks up to 200 titles in the Refinery (the NAS since ADR-0011): in the
        # bounded NAS pool like the other NAS routes, not the shared thread pool (review 2026-10-14).
        return await lib.nas.run(lambda: _directory(path))

    def _directory(path: str) -> DirectoryPage:
        parts = [p for p in path.strip("/").split("/") if p]
        # Only sites a reader can open (Library.readable_sources); a planned source's address still
        # opens a page saying so, and a directory path to it still resolves, for old bookmarks.
        held = lib.readable_sources()
        specs = sorted(s.sources, key=lambda x: x.name.casefold())
        listed = [x for x in specs if x.id in held]
        domains = sorted({d for x in specs for d in x.domain})

        def not_found() -> ApiError:
            return ApiError(404, "category_not_found", f"no such category: {path}")

        if not parts:
            children = [
                DirectoryCategory(path=d, name=sites.domain_name(d), count=sum(d in x.domain for x in listed))
                for d in domains
                if any(d in x.domain for x in listed)
            ]
            children.sort(key=lambda c: c.name)
            return DirectoryPage(path="", name="Top", trail=[], children=children, entries=[])

        domain = parts[0]
        if domain not in domains:
            raise not_found()
        trail = [DirectoryCrumb(path=domain, name=sites.domain_name(domain))]
        members = [x for x in specs if domain in x.domain]
        if len(parts) == 1:
            counts = lib.state.snapshot().doc_counts
            children = [
                DirectoryCategory(
                    path=f"{domain}/{x.id}",
                    name=x.name,
                    count=counts.get(x.id) or 0,
                    crosslink=x.domain[0] != domain,
                )
                for x in members
                if x.id in held
            ]
            return DirectoryPage(path=domain, name=trail[0].name, trail=trail, children=children, entries=[])

        spec: SourceSpec | None = next((x for x in members if x.id == parts[1]), None)
        if spec is None or len(parts) > 3:
            raise not_found()
        here = f"{domain}/{spec.id}"
        trail.append(DirectoryCrumb(path=here, name=spec.name))
        cats = lib.catalog.categories(spec.id)
        if len(parts) == 2:
            site = DirectoryEntry(
                title=spec.name,
                address=address_of(spec.id),
                source_id=spec.id,
                doc_id=sites.raw_str(spec, "home_doc_id"),
                description=sites.raw_str(spec, "description"),
            )
            children = [DirectoryCategory(path=f"{here}/{c.slug}", name=c.name, count=c.count) for c in cats]
            return DirectoryPage(path=here, name=spec.name, trail=trail, children=children, entries=[site])

        cat = next((c for c in cats if c.slug == parts[2]), None)
        if cat is None:
            raise not_found()
        trail.append(DirectoryCrumb(path=f"{here}/{cat.slug}", name=cat.name))
        entries: list[DirectoryEntry] = []
        for did in lib.catalog.category_docs(spec.id, cat.slug, limit=CATEGORY_LIMIT):
            ref = doc_ref(did)
            if ref is not None:
                entries.append(
                    DirectoryEntry(
                        title=ref.title, address=ref.address, source_id=ref.source_id, doc_id=ref.doc_id
                    )
                )
        return DirectoryPage(
            path=f"{here}/{cat.slug}",
            name=cat.name,
            trail=trail,
            children=[],
            entries=entries,
            total=cat.count if cat.count > len(entries) else None,
        )

    # --- Random ---------------------------------------------------------------------------------

    @r.get("/random", tags=["navigator"], responses=errors(404, 503))
    async def random_page(
        source: Annotated[str | None, Query(description="Pick from this source only.")] = None,
        seed: Annotated[int | None, Query(description="Make the pick reproducible.")] = None,
    ) -> DocRef:
        return await lib.nas.run(lambda: _random_page(source, seed))

    def _random_page(source: str | None, seed: int | None) -> DocRef:
        if source is not None:
            lib.source(source)
        rng = random.Random(secrets.randbits(64) if seed is None else seed)  # noqa: S311 - not security
        ref = doc_ref(lib.catalog.random_doc(sources=[source] if source else None, rng=rng))
        if ref is None and source is not None:
            ref = lib.random_zim_page(source)  # a site served straight from its ZIM archive
        if ref is None:
            raise ApiError(404, "empty", "no pages to pick from")
        return ref

    # --- Assets ---------------------------------------------------------------------------------

    @r.get(
        "/assets/{source_id}/{generation_id}/{path:path}",
        tags=["navigator"],
        response_class=Response,
        responses={
            200: {
                "description": "The image, with a long-lived cache header (generations are immutable); "
                "any other file a page links to (EPUB, PDF, a recording, sheet music) is sent as an "
                "attachment. Pages, scripts and stylesheets are never served here.",
                "content": {
                    "image/*": {"schema": {"type": "string", "format": "binary"}},
                    "application/epub+zip": {"schema": {"type": "string", "format": "binary"}},
                    "application/pdf": {"schema": {"type": "string", "format": "binary"}},
                },
            }
        }
        | errors(404, 503),
    )
    async def asset(source_id: str, generation_id: str, path: str) -> Response:
        lib.source(source_id)
        found = await lib.nas.run(lambda: lib.assets.asset(source_id, generation_id, path))
        kind = found.mimetype.split(";")[0].strip() if found is not None else ""
        if found is None or kind in REFUSED_MIMETYPES:
            raise ApiError(
                404, "asset_not_found", f"no image or download at {source_id}/{generation_id}/{path}"
            )
        headers = {"Cache-Control": "public, max-age=31536000, immutable"}
        if kind not in SERVED_MIMETYPES:
            name = path.rsplit("/", 1)[-1]
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name, safe='')}"
        return Response(content=found.content, media_type=found.mimetype, headers=headers)

    # --- Outside links ----------------------------------------------------------------------------

    @r.get("/outside", tags=["navigator"], responses=errors(404, 422))
    def outside_copy(
        url: Annotated[str, Query(min_length=1, max_length=2048, description="An outside (http/https) URL.")],
    ) -> OutsideCopy:
        """The library's copy of an outside URL (``wsp_api.outside``): a Wikipedia article, an RFC, a
        page of a website the library archives. 404 ``not_held`` when the library has none."""
        address = lib.outside_url(url)
        hit = lib.outside().target(url)
        if address is None or hit is None:
            raise ApiError(404, "not_held", "the library holds no copy of this address")
        return OutsideCopy(url=url, address=address, source_id=hit[0][0])

    # --- Gaps -----------------------------------------------------------------------------------

    @r.post("/gaps", tags=["navigator"], status_code=204, responses=errors(403, 422, 501))
    def report_gap(body: GapReport, request: Request) -> Response:
        """Record a "not in library" link for the gap miner. Accepted from loopback clients only."""
        if not _is_loopback(request):
            raise ApiError(403, "local_only", "gap reports are accepted from this computer only")
        if s.gaps is None:
            raise ApiError(501, "not_configured", "no gap log is configured")
        s.gaps.record(Gap(at=s.clock(), href=body.href, from_doc_id=body.from_doc_id))
        return Response(status_code=204)

    return r


def _readable(title: str) -> bool:
    """A title worth showing on the start page: printable, with a letter or digit from a common script."""
    if not title or not title.isprintable():
        return False
    return any(ch.isalnum() and ord(ch) < 0x3000 for ch in title)


def _presentable_recent(lib: Library, *, limit: int) -> list[ChangeRecord]:
    """Newest additions for the start page: readable titles, mixed across sources (round-robin), so one
    huge new generation (millions of dictionary entries) does not fill the box with obscure glyphs."""
    pool, _ = lib.catalog.recent(limit=limit * 50)
    by_source: dict[str, list[ChangeRecord]] = {}
    for c in pool:
        if c.kind is ChangeKind.REMOVED or not _readable(lib.title(c.doc_id) or ""):
            continue
        by_source.setdefault(c.doc_id.removeprefix("wsp:").partition("/")[0], []).append(c)
    out: list[ChangeRecord] = []
    queues = list(by_source.values())
    while queues and len(out) < limit:
        for q in list(queues):
            if q:
                out.append(q.pop(0))
            if not q:
                queues.remove(q)
            if len(out) >= limit:
                break
    return out


def _site_news(infos: Sequence[SourceInfo], *, limit: int, is_new: Callable[[str], bool]) -> list[Change]:
    """Recently sealed readable sites, newest first, linked to each site (PLAN §10.2 What's New). A site
    with no landing page has no ``doc_id``; clients follow ``address``. ``is_new``: its first generation."""
    fresh = sorted(
        (i for i in infos if i.as_of is not None and i.current_generation),
        key=lambda i: i.as_of or 0,
        reverse=True,
    )
    out: list[Change] = []
    for info in fresh[:limit]:
        when = info.as_of.strftime("%b %d") if info.as_of else ""
        out.append(
            Change(
                doc_id=info.home_doc_id or "",
                kind="added" if is_new(info.id) else "modified",
                generation_id=info.current_generation or "",
                source_id=info.id,
                title=f"{info.name} ({when})" if when else info.name,  # the kind says new or updated
                address=info.address,
            )
        )
    return out
