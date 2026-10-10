"""Resolve a ``RemoteFile`` into a download target: mirror URLs, size, digests and piece hashes.

Sources are merged: the ``RemoteFile`` itself, its ``mirrors`` and, when available, a Metalink
(``<url>.meta4`` on hosts listed in ``FetchConfig.metalink_hosts``, or a URL that *is* a Metalink).
Disagreeing digests or sizes are an error, never a silent pick.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from wsp_core.model import Checksum, RemoteFile
from wsp_fetch.config import FetchConfig, host_key
from wsp_fetch.errors import FetchError, SizeMismatchError
from wsp_fetch.hashing import merge_checksums, new_hasher
from wsp_fetch.metalink import MetalinkFile, Pieces, parse_metalink

log = logging.getLogger(__name__)

METALINK_SUFFIXES = (".meta4", ".metalink")
METALINK_MAX_BYTES = 16 << 20


@dataclass(frozen=True, slots=True)
class Target:
    url: str  # identity of the file (the RemoteFile URL), used for resume state
    urls: tuple[str, ...]  # candidate sources in preference order
    size: int | None
    checksums: tuple[Checksum, ...]
    pieces: Pieces | None = None
    metalink: MetalinkFile | None = None


def is_metalink_url(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(METALINK_SUFFIXES)


def _pick_file(files: tuple[MetalinkFile, ...], name: str) -> MetalinkFile:
    if len(files) == 1:
        return files[0]
    for f in files:
        if f.name == name or f.name.rsplit("/", 1)[-1] == name:
            return f
    raise FetchError(f"Metalink lists {len(files)} files and none is named {name!r}")


def resolve_target(remote: RemoteFile, get_bytes: Callable[[str, int], bytes], config: FetchConfig) -> Target:
    ml: MetalinkFile | None = None
    if is_metalink_url(remote.url):
        ml = _pick_file(parse_metalink(get_bytes(remote.url, METALINK_MAX_BYTES)), remote.name)
    elif host_key(remote.url) in config.metalink_hosts:
        try:
            ml = _pick_file(parse_metalink(get_bytes(remote.url + ".meta4", METALINK_MAX_BYTES)), remote.name)
        except FetchError as exc:  # optional: fall back to plain HTTP + mirrors
            log.info("no usable metalink for %s: %s", remote.url, exc)

    urls: list[str] = [] if is_metalink_url(remote.url) else [remote.url]
    urls += remote.mirrors
    if ml is not None:
        urls += ml.mirror_urls()
    urls = list(dict.fromkeys(u for u in urls if u))
    if not urls:
        raise FetchError(f"no download URL for {remote.name}")

    size = remote.size
    if ml is not None and ml.size is not None:
        if size is not None and size != ml.size:
            raise SizeMismatchError(f"{remote.name}: RemoteFile says {size} bytes, Metalink {ml.size}")
        size = ml.size
    checksums = merge_checksums(remote.checksums, ml.checksums if ml else ())
    for c in checksums:
        new_hasher(c.algorithm)  # fail fast on algorithms we cannot verify
    pieces = ml.pieces if ml else None
    if pieces is not None:
        new_hasher(pieces.algorithm)
    return Target(remote.url, tuple(urls), size, checksums, pieces, ml)
