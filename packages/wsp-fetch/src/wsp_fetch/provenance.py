"""Provenance of a download: who served which bytes, over which TLS session (ADR-0009).

Everything here is JSON-safe plain data. ``HttpFetcher`` records, per request URL, what it saw
(final URL after redirects, the redirect chain, the TLS peer certificate, HTTP validators) and, per
downloaded file, which mirror served which byte ranges, which mirrors were rejected and why, and
the result of spot checks that compare random ranges of every mirror with the verified file.
The orchestrator turns this into the signed ``PROVENANCE.json`` of the generation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

FORMAT = "whisipedia-fetch-provenance/1"
_NAME_KEYS = {"organizationName": "o", "commonName": "cn", "countryName": "c"}


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _rdn(name: object) -> dict[str, str]:
    """``getpeercert()`` subject/issuer (tuple of RDN tuples) -> ``{"o": ..., "cn": ..., "c": ...}``."""
    out: dict[str, str] = {}
    if not isinstance(name, tuple):
        return out
    for rdn in cast(tuple[object, ...], name):
        if not isinstance(rdn, tuple):
            continue
        for pair in cast(tuple[object, ...], rdn):
            if isinstance(pair, tuple) and len(cast(tuple[object, ...], pair)) == 2:
                key, value = cast(tuple[object, object], pair)
                short = _NAME_KEYS.get(str(key))
                if short:
                    out[short] = str(value)
    return out


def _cert_time(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.strptime(value, "%b %d %H:%M:%S %Y %Z")
    except ValueError:
        return value
    return when.replace(tzinfo=UTC).isoformat()


def issuer_family(issuer: Mapping[str, str]) -> str:
    """The CA family used for pinning: the issuer's organization (``Let's Encrypt``), else its CN
    without trailing generation numbers (``R10`` -> ``R``)."""
    org = issuer.get("o")
    if org:
        return org.strip()
    cn = issuer.get("cn", "").strip()
    return cn.rstrip("0123456789").strip() or cn or "unknown"


def tls_info(response: httpx.Response) -> dict[str, Any] | None:
    """The TLS peer of an open (streaming) response, or ``None`` for plain HTTP / unknown."""
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    get = getattr(stream, "get_extra_info", None)
    if get is None:
        return None
    try:
        ssl_object = get("ssl_object")
    except Exception:  # transports differ; provenance is best effort, never a download failure
        return None
    if ssl_object is None:
        return None
    try:
        der = cast(bytes | None, ssl_object.getpeercert(True))  # positional: _SSLSocket takes no kwargs
        cert = cast(dict[str, object], ssl_object.getpeercert() or {})
        version = cast(str | None, ssl_object.version())
    except (ValueError, AttributeError, TypeError):
        return None
    issuer = _rdn(cert.get("issuer"))
    return {
        "sha256": hashlib.sha256(der).hexdigest() if der else None,
        "subject": _rdn(cert.get("subject")),
        "issuer": issuer,
        "issuer_family": issuer_family(issuer) if issuer else None,
        "not_before": _cert_time(cert.get("notBefore")),
        "not_after": _cert_time(cert.get("notAfter")),
        "protocol": version,
    }


def redirect_chain(response: httpx.Response) -> list[dict[str, Any]]:
    """Every hop that redirected, oldest first: ``[{url, status, location}]``."""
    return [
        {"url": str(r.url), "status": r.status_code, "location": r.headers.get("Location")}
        for r in response.history
    ]


def observe(response: httpx.Response, request_url: str) -> dict[str, Any]:
    """What one request saw: final URL, redirects, TLS peer and HTTP validators."""
    headers = response.headers
    return {
        "url": request_url,
        "final_url": str(response.url),
        "host": host_of(str(response.url)),
        "status": response.status_code,
        "redirects": redirect_chain(response),
        "tls": tls_info(response),
        "etag": headers.get("ETag"),
        "last_modified": headers.get("Last-Modified"),
        "content_length": headers.get("Content-Length"),
        "content_range": headers.get("Content-Range"),
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def byte_ranges(pieces: Iterable[int], piece_size: int, size: int) -> list[list[int]]:
    """Piece indexes -> merged inclusive byte ranges ``[[start, end], ...]``."""
    out: list[list[int]] = []
    for i in sorted(set(pieces)):
        start = i * piece_size
        end = min(start + piece_size, size) - 1
        if out and out[-1][1] + 1 == start:
            out[-1][1] = end
        else:
            out.append([start, end])
    return out


def checksum_list(checksums: Sequence[Any]) -> list[dict[str, str]]:
    return [{"algorithm": str(c.algorithm), "hexdigest": str(c.hexdigest)} for c in checksums]
