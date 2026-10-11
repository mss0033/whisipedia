import hashlib

import pytest

from wsp_core.checksums import normalize_algorithm
from wsp_core.model import FetchResult, VerdictStatus


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("SHA-256", "sha256"),
        ("sha256", "sha256"),
        (" Sha256 ", "sha256"),
        ("SHA-1", "sha1"),
        ("MD5", "md5"),
        ("SHA3-256", "sha3_256"),
        ("sha-512", "sha512"),
        ("blake3", "blake3"),
    ],
)
def test_normalize_algorithm(raw: str, want: str) -> None:
    assert normalize_algorithm(raw) == want


def test_every_normalized_sha_name_is_computable() -> None:
    for raw in ("SHA-1", "SHA-224", "SHA-256", "SHA-384", "SHA-512", "SHA3-256", "SHA3-512", "MD5"):
        hashlib.new(normalize_algorithm(raw))


def test_corroborated_fetch_result_is_still_unverifiable() -> None:
    from pathlib import Path

    r = FetchResult(Path("x"), 1, "a" * 64, "b" * 64, corroborated=True)
    assert r.verdict.status is VerdictStatus.UNVERIFIABLE
    assert "agree" in r.verdict.detail
