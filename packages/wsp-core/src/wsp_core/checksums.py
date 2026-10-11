"""Checksum algorithm names, normalized one way for the whole library.

Upstreams spell algorithms in many ways (Metalink uses IANA names such as ``SHA-256``; checksum
files say ``sha256`` or ``SHA256``). Every component that computes or compares a digest normalizes
the name with :func:`normalize_algorithm` first, so a digest recorded as ``SHA-256`` is the same
digest that is verified as ``sha256`` (vault-fetch audit #23).
"""

from __future__ import annotations

_ALIASES = {
    "sha-1": "sha1",
    "sha-224": "sha224",
    "sha-256": "sha256",
    "sha-384": "sha384",
    "sha-512": "sha512",
    "sha3-224": "sha3_224",
    "sha3-256": "sha3_256",
    "sha3-384": "sha3_384",
    "sha3-512": "sha3_512",
    "sha-3-256": "sha3_256",
    "sha-3-512": "sha3_512",
    "md-5": "md5",
}


def normalize_algorithm(name: str) -> str:
    """``SHA-256`` / ``sha-256`` / `` Sha256 `` -> ``sha256``; ``SHA3-256`` -> ``sha3_256`` (hashlib)."""
    key = name.strip().lower()
    return _ALIASES.get(key, key.replace("-", ""))
