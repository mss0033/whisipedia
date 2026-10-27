"""Fetch safety rules (vault-fetch audit #8, #15, #16 and LOW): corroboration, no splicing, size
checks, lone-source retries, signed-redirect re-resolution, no HTTPS downgrade, politeness keyed on
the original host, one fetch per destination, aria2 supervision."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from wsp_core.errors import RedirectRefusedError
from wsp_core.model import RemoteFile, VerdictStatus
from wsp_core.redirects import current_guard, redirect_guard, unguarded
from wsp_core.testing.fetcher_contract import RangeServer, payload, sha256_checksum
from wsp_fetch import ChecksumMismatchError, FetchConfig, FetchError, HttpFetcher
from wsp_fetch.errors import FetchCancelledError, FetchLockedError, UnverifiableError

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
_RANGE = re.compile(r"bytes=(\d+)-(\d+)")


def _ranged(data: bytes, request: httpx.Request, *, etag: str = '"v1"') -> httpx.Response:
    m = _RANGE.match(request.headers.get("Range", ""))
    if m is None:
        return httpx.Response(
            200, stream=httpx.ByteStream(data), headers={"Content-Length": str(len(data)), "ETag": etag}
        )
    start, end = int(m.group(1)), min(int(m.group(2)), len(data) - 1)
    return httpx.Response(
        206,
        stream=httpx.ByteStream(data[start : end + 1]),
        headers={"Content-Range": f"bytes {start}-{end}/{len(data)}", "ETag": etag},
    )


# --- unverifiable sources -----------------------------------------------------------------------


def test_unverified_file_is_corroborated_by_a_second_full_download(servers: Servers, tmp_path: Path) -> None:
    data = payload(300_000, seed=1)
    a, b = servers(), servers()
    a.add("x.bin", data)
    b.add("x.bin", data)
    remote = RemoteFile(a.url("x.bin"), "x.bin", mirrors=(b.url("x.bin"),))
    with HttpFetcher(CFG) as f:
        report = f.fetch(remote, tmp_path / "x.bin")
    assert report.corroborated
    assert report.integrity == "corroborated"
    assert report.verdict.status is VerdictStatus.UNVERIFIABLE
    assert "agree" in report.verdict.detail
    assert report.provenance["corroboration"]["independent_source"] is True
    assert list(report.per_mirror_bytes) == [a.url("x.bin")]  # no splicing across mirrors
    assert b.body_bytes_requested("x.bin", len(data)) >= len(data)  # the second, full download


def test_disagreeing_downloads_are_a_mismatch_and_evidence_is_kept(servers: Servers, tmp_path: Path) -> None:
    data = payload(200_000, seed=2)
    other = bytearray(data)
    other[1234] ^= 1  # same size, one bit different: a mirror serving another file
    a, b = servers(), servers()
    a.add("x.bin", data)
    b.add("x.bin", bytes(other))
    remote = RemoteFile(a.url("x.bin"), "x.bin", mirrors=(b.url("x.bin"),))
    dest = tmp_path / "x.bin"
    with HttpFetcher(CFG) as f, pytest.raises(ChecksumMismatchError, match="second download"):
        f.fetch(remote, dest)
    assert not dest.exists()
    assert (tmp_path / "x.bin.disputed").read_bytes() == data


def test_lone_unverified_source_is_downloaded_twice(servers: Servers, tmp_path: Path) -> None:
    data = payload(100_000, seed=3)
    a = servers()
    remote = RemoteFile(a.add("x.bin", data), "x.bin")
    with HttpFetcher(CFG) as f:
        report = f.fetch(remote, tmp_path / "x.bin")
    assert report.corroborated
    assert report.provenance["corroboration"]["independent_source"] is False
    assert a.body_bytes_requested("x.bin", len(data)) >= 2 * len(data)


def test_unverified_policies_allow_and_refuse(servers: Servers, tmp_path: Path) -> None:
    data = payload(50_000, seed=4)
    a = servers()
    remote = RemoteFile(a.add("x.bin", data), "x.bin")
    with HttpFetcher(replace(CFG, unverified_policy="allow")) as f:
        report = f.fetch(remote, tmp_path / "a.bin")
    assert (report.integrity, report.corroborated) == ("unverified", False)
    with HttpFetcher(replace(CFG, unverified_policy="refuse")) as f, pytest.raises(UnverifiableError):
        f.fetch(remote, tmp_path / "r.bin")
    assert not (tmp_path / "r.bin").exists()
    with pytest.raises(ValueError, match="unverified_policy"):
        FetchConfig(unverified_policy="hope")


def test_checksummed_file_is_not_downloaded_twice(servers: Servers, tmp_path: Path) -> None:
    data = payload(100_000, seed=5)
    a = servers()
    remote = RemoteFile(a.add("x.bin", data), "x.bin", len(data), (sha256_checksum(data),))
    with HttpFetcher(CFG) as f:
        report = f.fetch(remote, tmp_path / "x.bin")
    assert report.integrity == "verified"
    assert a.body_bytes_requested("x.bin", len(data)) < 2 * len(data)


# --- size checks and retries for a lone source ---------------------------------------------------


def test_truncated_close_delimited_body_is_retried_not_accepted(tmp_path: Path) -> None:
    data = payload(80_000, seed=6)
    gets = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Range") == "bytes=0-0":  # probe: no range support, size advertised
            return httpx.Response(
                200, stream=httpx.ByteStream(data), headers={"Content-Length": str(len(data))}
            )
        gets["n"] += 1
        if gets["n"] == 1:
            return httpx.Response(
                200, stream=httpx.ByteStream(data[:30_000])
            )  # connection closed early, no length
        return httpx.Response(200, stream=httpx.ByteStream(data))

    remote = RemoteFile("https://only.example/x.bin", "x.bin", None, (sha256_checksum(data),))
    with HttpFetcher(CFG, transport=httpx.MockTransport(handler)) as f:
        report = f.fetch(remote, tmp_path / "x.bin")
    assert (tmp_path / "x.bin").read_bytes() == data
    assert report.size == len(data)
    assert gets["n"] == 2


# --- redirects --------------------------------------------------------------------------------------


def test_https_to_http_redirect_is_refused(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"Location": "http://cdn.example/x.bin"})
        return httpx.Response(200, stream=httpx.ByteStream(b"plaintext bytes"))

    with HttpFetcher(CFG, transport=httpx.MockTransport(handler)) as f:
        with pytest.raises(FetchError, match="insecure"):
            f.get_bytes("https://origin.example/x.bin")
        with pytest.raises(FetchError):
            f.fetch(RemoteFile("https://origin.example/x.bin", "x.bin"), tmp_path / "x.bin")
        assert f.get_bytes("http://plain.example/x.bin") == b"plaintext bytes"  # http->http is fine


def test_expired_signed_redirect_is_re_resolved(tmp_path: Path) -> None:
    data = payload(16 * PIECE, seed=7)
    state = {"version": 1, "served": 0}
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "origin.example":
            with lock:
                v = state["version"]
            return httpx.Response(302, headers={"Location": f"https://cdn.example/x.bin?sig={v}"})
        sig = int(request.url.params.get("sig", "0"))
        with lock:
            if sig != state["version"]:
                return httpx.Response(403)  # the signature expired
            state["served"] += 1
            if state["served"] == 5:  # rotate the signature in the middle of the download
                state["version"] += 1
        return _ranged(data, request)

    remote = RemoteFile("https://origin.example/x.bin", "x.bin", len(data), (sha256_checksum(data),))
    with HttpFetcher(CFG, transport=httpx.MockTransport(handler)) as f:
        report = f.fetch(remote, tmp_path / "x.bin")
    assert (tmp_path / "x.bin").read_bytes() == data
    assert report.sha256 == hashlib.sha256(data).hexdigest()
    assert state["version"] == 2


def test_politeness_cap_follows_the_original_host(tmp_path: Path) -> None:
    data = payload(32 * PIECE, seed=8)
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "dumps.example":
            return httpx.Response(302, headers={"Location": "https://cdn.example/x.bin"})
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.01)
        with lock:
            active["now"] -= 1
        return _ranged(data, request)

    cfg = replace(CFG, host_connections={"dumps.example": 1, "cdn.example": 8})
    remote = RemoteFile("https://dumps.example/x.bin", "x.bin", len(data), (sha256_checksum(data),))
    with HttpFetcher(cfg, transport=httpx.MockTransport(handler)) as f:
        f.fetch(remote, tmp_path / "x.bin")
    assert active["max"] == 1  # the cap of dumps.example, not of the CDN it redirects to


# --- one fetch per destination ---------------------------------------------------------------------


def test_a_destination_is_fetched_by_one_fetch_at_a_time(servers: Servers, tmp_path: Path) -> None:
    data = payload(40 * PIECE, seed=9)
    a = servers()
    a.faults.delay = 0.01
    remote = RemoteFile(a.add("x.bin", data), "x.bin", len(data), (sha256_checksum(data),))
    dest = tmp_path / "x.bin"
    results: list[object] = []

    def run() -> None:
        with HttpFetcher(CFG) as f:
            try:
                results.append(f.fetch(remote, dest))
            except FetchError as exc:
                results.append(exc)

    first = threading.Thread(target=run)
    first.start()
    time.sleep(0.3)
    second = threading.Thread(target=run)
    second.start()
    first.join(60)
    second.join(60)
    assert sum(isinstance(r, FetchLockedError) for r in results) == 1
    assert dest.read_bytes() == data


def test_destination_locks_live_in_the_configured_directory_and_are_removed(
    servers: Servers, tmp_path: Path
) -> None:
    """Audit 2026-10-12 M4: every process must share one lock directory (the orchestrator's /tmp is
    private), and a lock file per download used to stay forever (18,489 of them)."""
    data = payload(4 * PIECE, seed=3)
    a = servers()
    remote = RemoteFile(a.add("y.bin", data), "y.bin", len(data), (sha256_checksum(data),))
    locks = tmp_path / "locks" / "fetch"
    cfg = replace(CFG, lock_dir=str(locks))
    with HttpFetcher(cfg) as f:
        seen: list[list[str]] = []
        with f.destination_lock(tmp_path / "y.bin"):
            seen.append(sorted(p.name for p in locks.iterdir()))
            other = HttpFetcher(cfg)
            try:
                with pytest.raises(FetchLockedError), other.destination_lock(tmp_path / "y.bin"):
                    pass  # a second fetch of the same file, from any process
            finally:
                other.close()
        assert len(seen[0]) == 1
        assert list(locks.iterdir()) == []  # removed when the fetch ends
        f.fetch(remote, tmp_path / "y.bin")
    assert (tmp_path / "y.bin").read_bytes() == data
    assert list(locks.iterdir()) == []


def test_a_lock_file_removed_by_its_last_holder_is_not_trusted(tmp_path: Path) -> None:
    """A taker that opened the lock file just before the holder removed it would lock a dead inode
    while a third fetch locks the new file: the inode is checked after locking, and retried."""
    import os

    locks = tmp_path / "locks"
    cfg = replace(CFG, lock_dir=str(locks))
    dest = tmp_path / "z.bin"
    real_open = os.open
    raced = {"done": False}

    def open_then_lose_the_file(path: object, flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)  # type: ignore[arg-type]
        if not raced["done"] and str(path).endswith(".lock"):
            raced["done"] = True
            os.unlink(str(path))  # the previous holder finished and removed it, after our open
        return fd

    with HttpFetcher(cfg) as f:
        os_open = os.open
        try:
            os.open = open_then_lose_the_file  # type: ignore[assignment]
            with f.destination_lock(dest):
                os.open = os_open  # type: ignore[assignment]
                (name,) = [p.name for p in locks.iterdir()]  # holding the file that is at the path
                with pytest.raises(FetchLockedError), HttpFetcher(cfg) as other, other.destination_lock(dest):
                    pass
        finally:
            os.open = os_open  # type: ignore[assignment]
    assert name.endswith(".lock")
    assert list(locks.iterdir()) == []


def test_user_agent_with_an_email_address_is_refused() -> None:
    with pytest.raises(ValueError, match="@"):
        FetchConfig(user_agent="Whisipedia/1.0 (contact: someone@example.org)")


# --- aria2 supervision -------------------------------------------------------------------------------


aria2 = pytest.importorskip("wsp_fetch.aria2")
needs_aria2 = pytest.mark.skipif(aria2.find_aria2c() is None, reason="aria2c not installed")


@needs_aria2
def test_aria2_hands_files_without_piece_hashes_to_the_http_engine(
    servers: Servers, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = payload(200_000, seed=10)
    a = servers()
    remote = RemoteFile(a.add("x.bin", data), "x.bin", len(data), (sha256_checksum(data),))

    def no_aria2(*args: object) -> tuple[int, str]:
        raise AssertionError("aria2c must not run without piece hashes")

    with aria2.Aria2Fetcher(CFG) as f:
        monkeypatch.setattr(f, "_run", no_aria2)
        f.fetch(remote, tmp_path / "x.bin")
    assert (tmp_path / "x.bin").read_bytes() == data


@needs_aria2
def test_aria2_cancel_and_stall_kill_the_process(tmp_path: Path) -> None:
    with aria2.Aria2Fetcher(replace(CFG, stall_timeout=1.0)) as f:
        t0 = time.monotonic()
        with pytest.raises(FetchError, match="no progress"):
            f._supervise(["sleep", "30"], tmp_path / "out", tmp_path / "log")  # pyright: ignore[reportPrivateUsage]
        assert time.monotonic() - t0 < 5
        threading.Timer(0.5, f.cancel).start()
        t0 = time.monotonic()
        with pytest.raises(FetchCancelledError):
            f._supervise(["sleep", "30"], tmp_path / "out", tmp_path / "log")  # pyright: ignore[reportPrivateUsage]
        assert time.monotonic() - t0 < 5


def test_aria2_checksum_failure_keeps_the_newest_stage_as_evidence(tmp_path: Path) -> None:
    dest = tmp_path / "big.zim"
    old = tmp_path / "big.zim.rejected-20260101T000000Z.d"
    old.mkdir()
    stage = aria2.staging_dir(dest)
    stage.mkdir()
    (stage / "big.zim").write_bytes(b"250 GB, in spirit")
    kept = aria2._keep_evidence(stage, dest)  # pyright: ignore[reportPrivateUsage]
    assert (kept / "big.zim").read_bytes() == b"250 GB, in spirit"
    assert not old.exists()
    assert not stage.exists()


def test_a_guarded_redirect_is_refused_before_anything_comes_from_its_target(tmp_path: Path) -> None:
    """2026-10-23: BODC's GEBCO link redirected to CEDA, whose robots.txt disallows us; 7.5 GB came
    from CEDA before the collection's checks ran. A guard set around the download refuses the hop
    itself, in the probe threads and the piece workers alike."""
    data = payload(5 * PIECE)
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request.url.host)
        if request.url.host == "origin.example":
            return httpx.Response(302, headers={"Location": "https://cdn.example/x.bin"})
        return _ranged(data, request)

    seen: list[tuple[str, str]] = []

    def refuse_cdn(url: str) -> None:
        seen.append((threading.current_thread().name, url))
        if "cdn.example" in url:
            raise RedirectRefusedError(f"redirected to {url}: not allowed")

    remote = RemoteFile(
        "https://origin.example/x.bin", "x.bin", size=len(data), checksums=(sha256_checksum(data),)
    )
    with HttpFetcher(CFG, transport=httpx.MockTransport(handler)) as f:
        with redirect_guard(refuse_cdn), pytest.raises(FetchError, match="not allowed"):
            f.fetch(remote, tmp_path / "x.bin")
        assert "cdn.example" not in reached  # the hop was never followed
        assert not (tmp_path / "x.bin").exists()
        assert any(name != "MainThread" for name, _ in seen)  # the fetcher's own threads were guarded

        # Allowed: the same download completes, every hop checked; outside the block nothing is.
        reached.clear()
        seen.clear()
        with redirect_guard(lambda url: seen.append(("", url))):
            f.fetch(remote, tmp_path / "ok.bin")
        assert (tmp_path / "ok.bin").read_bytes() == data
        assert seen
        seen.clear()
        f.fetch(remote, tmp_path / "again.bin")
        assert seen == []


def test_guards_nest_and_a_guard_s_own_requests_are_not_guarded() -> None:
    calls: list[str] = []
    with (
        redirect_guard(lambda u: calls.append(f"outer {u}")),
        redirect_guard(lambda u: calls.append(f"inner {u}")),
    ):
        guard = current_guard()
        assert guard is not None
        guard("https://x.example/")
        with unguarded():
            assert current_guard() is None
    assert calls == ["outer https://x.example/", "inner https://x.example/"]
    assert current_guard() is None


def _robots_world(data: bytes) -> tuple[httpx.MockTransport, list[str]]:
    """good.example allows us, bad.example disallows everything, open.example has no robots.txt."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(f"{request.url.host}{request.url.path}")
        if request.url.path == "/robots.txt":
            if request.url.host == "bad.example":
                return httpx.Response(200, stream=httpx.ByteStream(b"User-agent: *\nDisallow: /\n"))
            if request.url.host == "good.example":
                return httpx.Response(200, stream=httpx.ByteStream(b"User-agent: *\nDisallow: /private/\n"))
            return httpx.Response(404, stream=httpx.ByteStream(b""))
        if request.url.host == "jump.example":
            return httpx.Response(302, headers={"Location": "https://bad.example/x.bin"})
        return _ranged(data, request)

    return httpx.MockTransport(handler), asked


def test_every_request_mirror_and_redirect_honours_robots_txt(tmp_path: Path) -> None:
    """2026-10-23: only document collections checked robots.txt; PubMed's file host, the OSM planet
    and a Kiwix mirror disallow us. The fetcher itself now refuses them, and drops such a mirror."""
    from wsp_core.errors import RedirectRefusedError, RobotsDisallowedError

    data = payload(3 * PIECE)
    transport, asked = _robots_world(data)
    cfg = replace(CFG, robots=True)
    with HttpFetcher(cfg, transport=transport) as f:
        with pytest.raises(RobotsDisallowedError):
            f.get_bytes("https://bad.example/x.bin")
        assert "bad.example/x.bin" not in asked
        with pytest.raises(RobotsDisallowedError):
            f.get_bytes("https://good.example/private/notes.txt")
        assert f.get_bytes("https://good.example/x.bin") == data
        assert f.get_bytes("https://open.example/x.bin") == data  # no robots.txt: no rules
        # A mirror that disallows us is dropped; the download comes from the others.
        remote = RemoteFile(
            "https://bad.example/x.bin",
            "x.bin",
            size=len(data),
            checksums=(sha256_checksum(data),),
            mirrors=("https://good.example/x.bin",),
        )
        f.fetch(remote, tmp_path / "x.bin")
        assert (tmp_path / "x.bin").read_bytes() == data
        assert "bad.example/x.bin" not in asked
        # A redirect onto a disallowing host is not followed.
        with pytest.raises((RedirectRefusedError, FetchError), match="disallowed"):
            f.get_bytes("https://jump.example/x.bin")
        assert "bad.example/x.bin" not in asked
        assert asked.count("good.example/robots.txt") == 1  # read once per host


def test_robots_txt_can_be_turned_off_for_tests() -> None:
    data = payload(PIECE)
    transport, asked = _robots_world(data)
    with HttpFetcher(CFG, transport=transport) as f:  # CFG: robots=False
        assert f.get_bytes("https://bad.example/x.bin") == data
    assert not [a for a in asked if a.endswith("/robots.txt")]


def test_an_unreachable_robots_txt_disallows_until_it_is_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """RFC 9309 2.3.1 (2026-10-26 review): a 5xx or 429 robots.txt is "unreachable", so everything is
    disallowed, tried again after 10 minutes; a 4xx (403, 404) is "unavailable", so there are no rules.
    A file is kept 24 hours."""
    import time

    from wsp_core.errors import RobotsDisallowedError

    data = payload(PIECE)
    answers = {"down.example": 503, "busy.example": 429, "closed.example": 403}
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(f"{request.url.host}{request.url.path}")
        if request.url.path == "/robots.txt":
            code = answers.get(request.url.host, 200)
            return httpx.Response(code, stream=httpx.ByteStream(b"User-agent: *\nDisallow:\n"))
        return _ranged(data, request)

    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    with HttpFetcher(replace(CFG, robots=True), transport=httpx.MockTransport(handler)) as f:
        for host in ("down.example", "busy.example"):
            with pytest.raises(RobotsDisallowedError):
                f.get_bytes(f"https://{host}/x.bin")
        assert f.get_bytes("https://closed.example/x.bin") == data  # 403: no rules
        assert "down.example/x.bin" not in asked
        answers["down.example"] = 200  # the site recovers
        with pytest.raises(RobotsDisallowedError):
            f.get_bytes("https://down.example/x.bin")  # still the cached answer
        clock[0] += 601
        assert f.get_bytes("https://down.example/x.bin") == data
        n = asked.count("down.example/robots.txt")
        clock[0] += 3600
        f.get_bytes("https://down.example/x.bin")
        assert asked.count("down.example/robots.txt") == n  # a file is kept 24 hours
        clock[0] += 24 * 3600
        f.get_bytes("https://down.example/x.bin")
        assert asked.count("down.example/robots.txt") == n + 1
