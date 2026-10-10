"""Whisipedia fetchers: fast, polite, hash-verified downloads (PLAN §6.5, §8.1).

``HttpFetcher`` is the reference ``Fetcher``; ``Aria2Fetcher`` drives ``aria2c`` under the same
contract. See ``docs/components/fetch.md``.
"""

from wsp_fetch.aria2 import Aria2Fetcher, find_aria2c
from wsp_fetch.config import FetchConfig
from wsp_fetch.errors import (
    Aria2Error,
    ChecksumMismatchError,
    ConflictingChecksumsError,
    FetchError,
    MirrorsExhaustedError,
    ResponseTooLargeError,
    SizeMismatchError,
    UnsupportedChecksumError,
)
from wsp_fetch.finish import FetchResult
from wsp_fetch.http import HttpFetcher
from wsp_fetch.metalink import MetalinkFile, MetalinkUrl, Pieces, build_metalink, parse_metalink
from wsp_fetch.mirrors import (
    MirrorCatalog,
    kiwix_metalink_url,
    kiwix_mirror_bases,
    kiwix_mirrors,
    load_wikimedia_catalog,
    wikimedia_mirror_urls,
)

__version__ = "0.1.0"

__all__ = [
    "Aria2Error",
    "Aria2Fetcher",
    "ChecksumMismatchError",
    "ConflictingChecksumsError",
    "FetchConfig",
    "FetchError",
    "FetchResult",
    "HttpFetcher",
    "MetalinkFile",
    "MetalinkUrl",
    "MirrorCatalog",
    "MirrorsExhaustedError",
    "Pieces",
    "ResponseTooLargeError",
    "SizeMismatchError",
    "UnsupportedChecksumError",
    "build_metalink",
    "find_aria2c",
    "kiwix_metalink_url",
    "kiwix_mirror_bases",
    "kiwix_mirrors",
    "load_wikimedia_catalog",
    "parse_metalink",
    "wikimedia_mirror_urls",
]
