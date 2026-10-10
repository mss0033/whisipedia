from __future__ import annotations

import bz2
import gzip
from pathlib import Path

import pytest
import zstandard

from wsp_adapters._streams import Bitset, Throttle, listing_links, open_any, open_bz2


@pytest.mark.parametrize("workers", [1, 3])
def test_open_bz2_sequential_and_parallel(tmp_path: Path, workers: int) -> None:
    data = b"".join(f"line {i}\n".encode() for i in range(50_000))
    path = tmp_path / "x.json.bz2"
    path.write_bytes(bz2.compress(data))
    with open_bz2(path, workers) as fh:
        assert fh.read() == data


def test_open_any(tmp_path: Path) -> None:
    for name, blob in {
        "a.gz": gzip.compress(b"gz"),
        "a.zst": zstandard.ZstdCompressor().compress(b"zst"),
        "a.txt": b"txt",
    }.items():
        (tmp_path / name).write_bytes(blob)
        with open_any(tmp_path / name) as fh:
            assert fh.read() == name.split(".")[1].replace("txt", "txt").encode()


def test_listing_links() -> None:
    html = b'<a href="../">..</a><a href="a.xml.gz">a</a><a href="/pub/b.gz">b</a><a href="?C=M">sort</a>'
    assert listing_links(html, "https://h/pub/") == ["https://h/pub/a.xml.gz", "https://h/pub/b.gz"]


def test_bitset_and_throttle() -> None:
    bits = Bitset()
    bits.add(40_000_000)
    bits.add(3)
    assert 3 in bits
    assert 40_000_000 in bits
    assert 4 not in bits
    assert 10**10 not in bits
    now = [10.0]
    slept: list[float] = []
    throttle = Throttle(3.0, clock=lambda: now[0], sleep=slept.append)
    throttle.wait()
    now[0] = 11.0
    throttle.wait()
    assert slept == [2.0]
