"""``wsp://`` addresses (PLAN §10.2) and their mapping to API resources. Spec: docs/api/addresses.md."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal
from urllib.parse import quote, unquote, urlencode

from wsp_api.errors import ApiError
from wsp_api.schemas import Resolution
from wsp_api.services import Services
from wsp_core.ids import DocId, InvalidDocIdError, make_doc_id, parse_doc_id

SCHEME = "wsp://"

# Pages that are not sources. Source ids must not use these names.
_PageKind = Literal["home", "directory", "system", "whatsnew", "random", "client"]
_PAGES: dict[str, tuple[_PageKind, str | None]] = {
    "home": ("home", "/v1/portal"),
    "directory": ("directory", "/v1/directory"),
    "system": ("system", "/v1/system"),
    "whatsnew": ("whatsnew", "/v1/changes/recent"),
    "random": ("random", "/v1/random"),
    # Drawn entirely by the Navigator; reserved so no source can take the name.
    **{name: ("client", None) for name in ("history", "hotlist", "about", "help", "offline")},
}
RESERVED = frozenset({*_PAGES, "search"})

# encodeURIComponent's unreserved set, so Python and the Navigator build identical URLs.
_SEGMENT_SAFE = "!'()*"


def encode_native(native_id: str) -> str:
    """A native id as the path of an address, by the Navigator's rule (``encodePath``): readable, with
    ``%``, ``?``, ``#`` and spaces escaped, and a trailing ``/`` as ``%2F`` so it survives the parser,
    which drops trailing slashes. Unescaped, ``…/GeneratePF?articleId=3547`` lost its query and
    ``xkcd.com/936/`` its slash, and both opened as Not Found (trawler, 2026-10-14)."""
    body = "".join(quote(c, safe="") if c in "%?#" or c.isspace() else c for c in native_id)
    stripped = body.rstrip("/")
    return stripped + "%2F" * (len(body) - len(stripped))


def address_of(source_id: str, native_id: str | None = None) -> str:
    return f"{SCHEME}{source_id}" if native_id is None else f"{SCHEME}{source_id}/{encode_native(native_id)}"


def encode_segment(value: str) -> str:
    """Percent-encode as one path segment (``/`` → ``%2F``), like JavaScript's ``encodeURIComponent``."""
    return quote(value, safe=_SEGMENT_SAFE)


def document_href(doc_id: str) -> str:
    return "/v1/documents/" + encode_segment(doc_id)


def _strip(address: str) -> str:
    a = address.strip()
    if a.startswith(SCHEME):
        return a[len(SCHEME) :]
    if a.startswith("wsp:"):  # a bare doc id: wsp:<source>/<native>
        return a[len("wsp:") :]
    if "://" in a:
        raise ApiError(400, "external_address", f"not a library address (offline only): {a}")
    return a


def _near_misses(native: str) -> list[str]:
    """``native`` and what a mistyped or older address may have meant: a trailing slash some native ids
    end with, and the last part's first letter in the other case (`/Safety` for `/safety`)."""
    out = [native] if native.endswith("/") else [native, native + "/"]
    cut = native.rfind("/") + 1
    first = native[cut : cut + 1]
    if first and first.swapcase() != first:
        out.append(native[:cut] + first.swapcase() + native[cut + 1 :])
    return out


def resolve(
    services: Services,
    address: str,
    query: Mapping[str, str],
    exists: Callable[[DocId], bool] | None = None,
    stand_in: Callable[[DocId], DocId | None] | None = None,
) -> Resolution:
    """Resolve an address. ``query`` carries the address's own query string (``wsp://search?q=...``).

    ``exists`` decides whether a document is in the library; the API passes ``Library.exists``, which
    also counts pages served straight from a ZIM (audit #10). Default: the document store.
    ``stand_in`` finds another source's page for a document the library lacks (``Library.stand_in``)."""
    path = _strip(address).strip("/")
    head, _, rest = path.partition("/")
    if head == "search":
        q = query.get("q", "").strip()
        href = "/v1/search?" + urlencode({"q": q}) if q else None
        canonical = f"{SCHEME}search" + (f"?{urlencode({'q': q})}" if q else "")
        return Resolution(address=canonical, kind="search", href=href)
    if head in _PAGES:
        kind, href = _PAGES[head]
        if head == "directory" and rest:
            return Resolution(
                address=f"{SCHEME}directory/{rest}",
                kind=kind,
                href="/v1/directory?" + urlencode({"path": rest}),
            )
        if head == "random" and query.get("source"):
            params = urlencode({"source": query["source"]})
            return Resolution(address=f"{SCHEME}random?{params}", kind=kind, href=f"/v1/random?{params}")
        return Resolution(address=f"{SCHEME}{head}", kind=kind, href=href)
    if services.source(head) is None:
        raise ApiError(404, "address_not_found", f"no site at {SCHEME}{path}")
    if not rest:
        return Resolution(address=address_of(head), kind="source", href=f"/v1/sources/{head}")
    rest = unquote(rest)  # the inverse of encode_native (a trailing %2F becomes the slash again)

    def found(native: str) -> DocId | None:
        try:
            doc_id: DocId = make_doc_id(head, native)
        except InvalidDocIdError as exc:
            raise ApiError(400, "invalid_address", str(exc)) from exc
        ok = exists(doc_id) if exists is not None else services.documents.get(doc_id) is not None
        return doc_id if ok else None

    # A typed or older address lost a native id's trailing slash (xkcd.com/936/): try it with one.
    for native in _near_misses(rest):
        doc_id = found(native)
        if doc_id is not None:
            return Resolution(address=address_of(head, native), kind="document", href=document_href(doc_id))
    alt = stand_in(make_doc_id(head, rest)) if stand_in is not None else None
    if alt is not None:
        alt_source, alt_native = parse_doc_id(alt)
        return Resolution(
            address=address_of(alt_source, alt_native), kind="document", href=document_href(alt)
        )
    raise ApiError(404, "not_in_library", f"{address_of(head, rest)} is not in the library")
