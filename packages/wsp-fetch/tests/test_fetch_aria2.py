"""Aria2Fetcher specifics: Metalink piece checks, multi-mirror use, our own re-hash."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from wsp_core.model import Checksum, RemoteFile
from wsp_core.testing.fetcher_contract import RangeServer, payload, sha256_checksum
from wsp_fetch import (
    Aria2Fetcher,
    FetchConfig,
    MetalinkFile,
    MetalinkUrl,
    Pieces,
    build_metalink,
    find_aria2c,
)
from wsp_fetch.aria2 import staging_dir

pytestmark = pytest.mark.skipif(find_aria2c() is None, reason="aria2c not installed")
CFG = FetchConfig(user_agent="wsp-test/aria2", max_attempts=3, backoff_base=0.25)
PIECE = 256 * 1024
Servers = Callable[[], RangeServer]


def test_multi_mirror_download_records_our_digests(servers: Servers, tmp_path: Path) -> None:
    data = payload(900_000, seed=30)
    a, b = servers(), servers()
    a.add("f.bin", data)
    b.add("f.bin", data)
    remote = RemoteFile(a.url("f.bin"), "f.bin", len(data), (sha256_checksum(data),), (b.url("f.bin"),))
    with Aria2Fetcher(CFG) as f:
        result = f.fetch(remote, tmp_path / "f.bin")
    assert (tmp_path / "f.bin").read_bytes() == data
    assert result.blake3
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert not staging_dir(tmp_path / "f.bin").exists()
    assert {r.user_agent for r in a.requests + b.requests} == {"wsp-test/aria2"}


def test_metalink_piece_corruption_is_repaired(servers: Servers, tmp_path: Path) -> None:
    data = payload(4 * PIECE, seed=31)
    bad, good, ml = servers(), servers(), servers()
    bad.add("z.zim", data)
    good.add("z.zim", data)
    bad.faults.corrupt_offsets = {PIECE + 3}
    hashes = tuple(hashlib.sha1(data[i : i + PIECE]).hexdigest() for i in range(0, len(data), PIECE))
    doc = build_metalink(
        [
            MetalinkFile(
                "z.zim",
                len(data),
                (Checksum("sha256", hashlib.sha256(data).hexdigest()),),
                Pieces(PIECE, "sha1", hashes),
                (MetalinkUrl(bad.url("z.zim"), 1), MetalinkUrl(good.url("z.zim"), 2)),
            )
        ]
    )
    url = ml.add("z.zim.meta4", doc)
    with Aria2Fetcher(CFG) as f:
        f.download(RemoteFile(url, "z.zim"), tmp_path / "z.zim")
    assert (tmp_path / "z.zim").read_bytes() == data
