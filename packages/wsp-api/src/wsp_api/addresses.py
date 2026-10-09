"""``wsp://`` addresses (PLAN §10.2) and their mapping to API resources. Spec: docs/api/addresses.md."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from urllib.parse import quote, urlencode

from wsp_api.errors import ApiError
from wsp_api.schemas import Resolution
from wsp_api.services import Services
from wsp_core.ids import DocId, InvalidDocIdError, make_doc_id

SCHEME = "wsp://"

# Pages that are not sources. Source ids must not use these names.
_PageKind = Literal["home", "directory", "system", "whatsnew"]
_PAGES: dict[str, tuple[_PageKind, str | None]] = {
    "home": ("home", "/v1/sources"),
    "directory": ("directory", None),
    "system": ("system", "/v1/system"),
    "whatsnew": ("whatsnew", "/v1/changes"),
}
RESERVED = frozenset({*_PAGES, "search"})


def address_of(source_id: str, native_id: str | None = None) -> str:
    return f"{SCHEME}{source_id}" if native_id is None else f"{SCHEME}{source_id}/{native_id}"


def document_href(doc_id: str) -> str:
    return "/v1/documents/" + quote(doc_id, safe="/:")


def _strip(address: str) -> str:
    a = address.strip()
    if a.startswith(SCHEME):
        return a[len(SCHEME) :]
    if a.startswith("wsp:"):  # a bare doc id: wsp:<source>/<native>
        return a[len("wsp:") :]
    if "://" in a:
        raise ApiError(400, "external_address", f"not a library address (offline only): {a}")
    return a


def resolve(services: Services, address: str, query: Mapping[str, str]) -> Resolution:
    """Resolve an address. ``query`` carries the address's own query string (``wsp://search?q=...``)."""
    path = _strip(address).strip("/")
    head, _, rest = path.partition("/")
    if head == "search":
        q = query.get("q", "").strip()
        href = "/v1/search?" + urlencode({"q": q}) if q else None
        canonical = f"{SCHEME}search" + (f"?{urlencode({'q': q})}" if q else "")
        return Resolution(address=canonical, kind="search", href=href)
    if head in _PAGES and not rest:
        kind, href = _PAGES[head]
        return Resolution(address=f"{SCHEME}{head}", kind=kind, href=href)
    if services.source(head) is None:
        raise ApiError(404, "address_not_found", f"no site at {SCHEME}{path}")
    if not rest:
        return Resolution(address=address_of(head), kind="source", href=f"/v1/sources/{head}")
    try:
        doc_id: DocId = make_doc_id(head, rest)
    except InvalidDocIdError as exc:
        raise ApiError(400, "invalid_address", str(exc)) from exc
    if services.documents.get(doc_id) is None:
        raise ApiError(404, "not_in_library", f"{address_of(head, rest)} is not in the library")
    return Resolution(address=address_of(head, rest), kind="document", href=document_href(doc_id))
