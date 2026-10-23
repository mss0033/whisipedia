"""HttpFetcher behaviour beyond the port contract: mirrors, pieces, politeness, resume."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from wsp_core.model import Checksum, RemoteFile
from wsp_core.testing.fetcher_contract import RangeServer, payload, sha256_checksum
from wsp_fetch import (
    ChecksumMismatchError,
    FetchConfig,
    HttpFetcher,
    MetalinkFile,
    MetalinkUrl,
    MirrorsExhaustedError,
    Pieces,
    build_metalink,
)
from wsp_fetch.http import backoff_delay, choose_piece_size, parse_retry_after
from wsp_fetch.state import partial_path, state_path

PIECE = 64 * 1024
CFG = FetchConfig(
    robots=False,  # robots.txt has tests of its own (test_fetch_safety)
    user_agent="wsp-test/1.0",
    piece_size=PIECE,
    min_piece_size=PIECE,
    backoff_base=0.01,
    backoff_max=0.05,
    max_attempts=4,
    state_interval=0.05,
)
Servers = Callable[[], RangeServer]


def remote_for(data: bytes, url: str, *mirrors: str, name: str = "f.bin") -> RemoteFile:
    return RemoteFile(url, name, len(data), (sha256_checksum(data),), tuple(mirrors))


def meta4(name: str, data: bytes, urls: list[str], piece: int = PIECE) -> bytes:
    hashes = tuple(hashlib.sha1(data[i : i + piece]).hexdigest() for i in range(0, len(data), piece))
    return build_metalink(
        [
            MetalinkFile(
                name=name,
                size=len(data),
                checksums=(Checksum("sha256", hashlib.sha256(data).hexdigest()),),
                pieces=Pieces(piece, "sha1", hashes),
                urls=tuple(MetalinkUrl(u, i + 1) for i, u in enumerate(urls)),
            )
        ]
    )


def test_parallel_multi_mirror_correctness(servers: Servers, tmp_path: Path) -> None:
    data = payload(900_000, seed=10)
    a, b, c = servers(), servers(), servers()
    for s in (a, b, c):
        s.add("f.bin", data)
        s.faults.delay = 0.002
    remote = remote_for(data, a.url("f.bin"), b.url("f.bin"), c.url("f.bin"))
    with HttpFetcher(CFG) as f:
        result = f.fetch(remote, tmp_path / "f.bin")
    assert (tmp_path / "f.bin").read_bytes() == data
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.verdict.ok
    assert all(n > 0 for n in result.per_mirror_bytes.values()), result.per_mirror_bytes
    assert sum(result.per_mirror_bytes.values()) >= len(data)
    assert {r.user_agent for s in (a, b, c) for r in s.requests} == {"wsp-test/1.0"}


def test_mirrors_are_probed_in_turn_when_no_thread_can_start(
    servers: Servers, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-19: "can't start new thread" while probing ended a nine-hour collection fetch; the
    mirrors left are probed one by one instead."""
    import threading

    class Starved(threading.Thread):
        def start(self) -> None:
            target = getattr(self, "_target", None)
            if "_probe_all" in getattr(target, "__qualname__", ""):
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr("wsp_fetch.http.threading.Thread", Starved)
    data = payload(300_000, seed=11)
    a, b = servers(), servers()
    for s in (a, b):
        s.add("f.bin", data)
    with HttpFetcher(CFG) as f:
        result = f.fetch(remote_for(data, a.url("f.bin"), b.url("f.bin")), tmp_path / "f.bin")
    assert (tmp_path / "f.bin").read_bytes() == data
    assert result.verdict.ok


def test_per_host_cap_respected(servers: Servers, tmp_path: Path) -> None:
    data = payload(640_000, seed=11)
    two, four = servers(), servers()
    for s in (two, four):
        s.add("f.bin", data)
        s.faults.delay = 0.003
    cfg = replace(CFG, host_connections={f"127.0.0.1:{four.port}": 4})
    with HttpFetcher(cfg) as f:
        f.download(remote_for(data, two.url("f.bin"), four.url("f.bin")), tmp_path / "f.bin")
    assert (tmp_path / "f.bin").read_bytes() == data
    assert two.max_active == 2  # default cap, and it was actually used in parallel
    assert 2 < four.max_active <= 4


def test_corrupt_piece_refetched_from_other_mirror(servers: Servers, tmp_path: Path) -> None:
    data = payload(512 * 1024, seed=12)
    bad, good, ml = servers(), servers(), servers()
    bad.add("z.zim", data)
    good.add("z.zim", data)
    bad.faults.corrupt_offsets = {i * PIECE + 17 for i in range(8)}  # every piece it serves is bad
    good.faults.delay = 0.004  # make the corrupting mirror the fast one
    meta_url = ml.add("z.zim.meta4", meta4("z.zim", data, [bad.url("z.zim"), good.url("z.zim")]))
    with HttpFetcher(CFG) as f:
        result = f.fetch(RemoteFile(meta_url, "z.zim"), tmp_path / "z.zim")
    assert (tmp_path / "z.zim").read_bytes() == data
    bad_pieces = {r.range for r in bad.gets("z.zim")} - {"bytes=0-0"}
    good_pieces = {r.range for r in good.gets("z.zim")}
    assert bad_pieces, "the corrupting mirror was never used"
    assert bad_pieces <= good_pieces  # each bad piece was re-fetched from the other mirror
    assert len(bad_pieces) <= 3  # dropped after its 2nd bad piece (+1 already in flight)
    assert {c.algorithm for c in result.verified} == {"sha256"}


def test_full_hash_mismatch_raises_typed_error(servers: Servers, tmp_path: Path) -> None:
    data = payload(200_000, seed=13)
    s = servers()
    s.add("f.bin", data)
    s.faults.corrupt_offsets = {1234}
    dest = tmp_path / "f.bin"
    with HttpFetcher(CFG) as f, pytest.raises(ChecksumMismatchError) as info:
        f.download(remote_for(data, s.url("f.bin")), dest)
    assert info.value.algorithm == "sha256"
    assert not dest.exists()
    assert not partial_path(dest).exists()
    assert not state_path(dest).exists()


def test_retry_after_is_honoured(servers: Servers, tmp_path: Path) -> None:
    data = payload(100_000, seed=14)
    s = servers()
    s.add("f.bin", data)
    t0 = time.monotonic()
    s.faults.fail_first = 1
    s.faults.fail_status = 429
    s.faults.retry_after = "1"
    with HttpFetcher(CFG) as f:
        f.download(remote_for(data, s.url("f.bin")), tmp_path / "f.bin")
    assert time.monotonic() - t0 >= 1.0
    assert (tmp_path / "f.bin").read_bytes() == data


def test_dropped_connections_are_retried(servers: Servers, tmp_path: Path) -> None:
    data = payload(300_000, seed=15)
    s = servers()
    s.add("f.bin", data)
    s.faults.drop_after = 40_000  # every response dies mid-piece
    m = servers()
    m.add("f.bin", data)
    with HttpFetcher(CFG) as f:
        f.download(remote_for(data, s.url("f.bin"), m.url("f.bin")), tmp_path / "f.bin")
    assert (tmp_path / "f.bin").read_bytes() == data


def test_all_mirrors_failing_raises_and_keeps_resume_state(servers: Servers, tmp_path: Path) -> None:
    data = payload(300_000, seed=16)
    s = servers()
    s.add("f.bin", data)
    s.faults.drop_after = 1000
    dest = tmp_path / "f.bin"
    with HttpFetcher(CFG) as f, pytest.raises(MirrorsExhaustedError):
        f.download(remote_for(data, s.url("f.bin")), dest)
    assert not dest.exists()
    assert state_path(dest).exists()


def test_server_ignoring_range_falls_back_to_single_stream(servers: Servers, tmp_path: Path) -> None:
    data = payload(150_000, seed=17)
    s = servers()
    s.add("f.bin", data)
    s.faults.ignore_range = True
    with HttpFetcher(CFG) as f:
        f.download(remote_for(data, s.url("f.bin")), tmp_path / "f.bin")
    assert (tmp_path / "f.bin").read_bytes() == data


def test_bandwidth_cap(servers: Servers, tmp_path: Path) -> None:
    data = payload(512 * 1024, seed=18)
    s = servers()
    s.add("f.bin", data)
    cfg = replace(CFG, bandwidth_limit=512 * 1024)
    t0 = time.monotonic()
    with HttpFetcher(cfg) as f:
        f.download(remote_for(data, s.url("f.bin")), tmp_path / "f.bin")
    # burst = rate/4 = 128 KiB, so the other 384 KiB need at least 0.75 s
    assert time.monotonic() - t0 >= 0.7


def test_existing_verified_file_is_not_downloaded_again(servers: Servers, tmp_path: Path) -> None:
    data = payload(80_000, seed=19)
    s = servers()
    s.add("f.bin", data)
    dest = tmp_path / "f.bin"
    dest.write_bytes(data)
    with HttpFetcher(CFG) as f:
        result = f.fetch(remote_for(data, s.url("f.bin")), dest)
    assert result.bytes_downloaded == 0
    assert s.gets() == []


def test_empty_file(servers: Servers, tmp_path: Path) -> None:
    s = servers()
    s.add("empty", b"")
    with HttpFetcher(CFG) as f:
        f.download(RemoteFile(s.url("empty"), "empty", 0, (sha256_checksum(b""),)), tmp_path / "empty")
    assert (tmp_path / "empty").read_bytes() == b""


def test_slow_mirror_does_not_stall_the_download(servers: Servers, tmp_path: Path) -> None:
    data = payload(1_000_000, seed=20)
    slow, fast = servers(), servers()
    slow.add("f.bin", data)
    fast.add("f.bin", data)
    slow.faults.delay = 0.25  # ~1 s per 64 KiB piece
    t0 = time.monotonic()
    with HttpFetcher(CFG) as f:
        result = f.fetch(remote_for(data, slow.url("f.bin"), fast.url("f.bin")), tmp_path / "f.bin")
    assert (tmp_path / "f.bin").read_bytes() == data
    assert time.monotonic() - t0 < 4.0
    assert result.per_mirror_bytes[fast.url("f.bin")] > result.per_mirror_bytes[slow.url("f.bin")]


def test_metalink_probed_on_configured_hosts(servers: Servers, tmp_path: Path) -> None:
    data = payload(200_000, seed=21)
    origin, mirror = servers(), servers()
    origin.add("z.zim", data)
    origin.faults.delay = 0.002
    mirror.add("z.zim", data)
    origin.add("z.zim.meta4", meta4("z.zim", data, [mirror.url("z.zim")]))
    cfg = replace(CFG, metalink_hosts=(f"127.0.0.1:{origin.port}",))
    with HttpFetcher(cfg) as f:
        result = f.fetch(RemoteFile(origin.url("z.zim"), "z.zim"), tmp_path / "z.zim")
    assert result.verdict.ok
    assert result.verified
    assert mirror.gets("z.zim")


_CHILD = """
import sys
from pathlib import Path
from wsp_core.model import Checksum, RemoteFile
from wsp_fetch import FetchConfig, HttpFetcher
url, dest, sha = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
cfg = FetchConfig(piece_size=65536, min_piece_size=65536, state_interval=0.02)
HttpFetcher(cfg).download(RemoteFile(url, dest.name, int(sys.argv[4]), (Checksum("sha256", sha),)), dest)
"""


def test_resume_after_kill(servers: Servers, tmp_path: Path) -> None:
    data = payload(20 * PIECE, seed=22)
    s = servers()
    url = s.add("big.bin", data)
    s.faults.delay = 0.02
    dest = tmp_path / "big.bin"
    sha = hashlib.sha256(data).hexdigest()
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    proc = subprocess.Popen([sys.executable, "-c", _CHILD, url, str(dest), sha, str(len(data))], env=env)
    try:
        deadline = time.monotonic() + 30
        done: dict[str, str] = {}
        while time.monotonic() < deadline and len(done) < 4:
            time.sleep(0.02)
            try:
                done = json.loads(state_path(dest).read_text())["done"]
            except (OSError, ValueError):
                continue
        assert len(done) >= 4, "child made no progress"
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait()
    assert not dest.exists()
    # Rot one recorded piece on disk: it must be detected and fetched again.
    first = min(int(k) for k in done)
    with partial_path(dest).open("r+b") as fh:
        fh.seek(first * PIECE + 5)
        fh.write(b"\x00\x01\x02")
    before = len(s.gets("big.bin"))
    s.faults.delay = 0.0
    with HttpFetcher(CFG) as f:
        result = f.fetch(RemoteFile(url, dest.name, len(data), (Checksum("sha256", sha),)), dest)
    assert dest.read_bytes() == data
    assert result.bytes_resumed >= 3 * PIECE
    second_run = s.gets("big.bin")[before:]
    ranges = {r.range for r in second_run}
    assert f"bytes={first * PIECE}-{(first + 1) * PIECE - 1}" in ranges  # rotted piece re-fetched
    fetched = sum(1 for r in second_run if r.range != "bytes=0-0")
    assert fetched <= 20 - (len(done) - 1)  # verified pieces were not downloaded again
    assert not state_path(dest).exists()


def test_helpers() -> None:
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412480.0) == 0.0
    assert parse_retry_after("garbage") is None
    assert 0.25 <= backoff_delay(3, 0.125, 10) <= 0.5
    assert choose_piece_size(10, CFG) == PIECE
    big = choose_piece_size(100 << 30, FetchConfig())
    assert big == 8 << 20
    assert big % 65536 == 0


def test_written_bytes_are_paced_by_the_library_budget(servers: Servers, tmp_path: Path) -> None:
    # 2026-10-16: six lanes writing into the vault at internet speed starved the API's NAS reads.
    data = payload(300_000, seed=31)
    s = servers()
    s.add("f.bin", data)
    paced: list[int] = []
    with HttpFetcher(CFG, pace=paced.append) as f:
        f.download(remote_for(data, s.url("f.bin")), tmp_path / "f.bin")
        assert f.get_bytes(s.url("f.bin")) == data  # read into memory, never written: not paced
    assert (tmp_path / "f.bin").read_bytes() == data
    assert sum(paced) == len(data)
