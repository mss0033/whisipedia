"""Contract suite for the ``Fetcher`` port (PLAN §6.5, §12.2) and a fault-injecting Range server.

Usage::

    class TestMyFetcher(FetcherContract):
        @pytest.fixture
        def fetcher(self) -> Iterator[Fetcher]:
            yield MyFetcher()

``RangeServer`` is a stdlib, in-process HTTP/1.1 server bound to 127.0.0.1 that serves byte
strings with single-range support and per-server faults (latency, error statuses, ``Retry-After``,
corruption, ignored ranges, dropped connections). It records every request and the peak number
of concurrent requests so tests can check politeness caps.
"""

from __future__ import annotations

import hashlib
import random
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import blake3
import pytest

from wsp_core.errors import ChecksumMismatchError, NotFoundError
from wsp_core.model import Checksum, RemoteFile, VerdictStatus
from wsp_core.ports import Fetcher

_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


@dataclass
class Faults:
    """Mutable fault knobs; change them between (or during) downloads."""

    delay: float = 0.0  # seconds slept before each chunk of a response body
    chunk: int = 16 * 1024
    fail_first: int = 0  # answer this many GETs with ``fail_status`` first
    fail_status: int = 503
    retry_after: str | None = None
    missing: bool = False  # 404 everything
    ignore_range: bool = False  # always answer 200 with the full body
    corrupt_offsets: set[int] = field(default_factory=set[int])  # flip these absolute byte offsets
    drop_after: int | None = None  # close the connection after this many body bytes per response


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    range: str | None
    user_agent: str | None


class RangeServer:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.faults = Faults()
        self.requests: list[Request] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, args=(0.05,), daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/{path.lstrip('/')}"

    def add(self, path: str, data: bytes) -> str:
        self.files["/" + path.lstrip("/")] = data
        return self.url(path)

    def start(self) -> RangeServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> RangeServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def gets(self, path: str | None = None) -> list[Request]:
        with self._lock:
            return [
                r
                for r in self.requests
                if r.method == "GET" and (path is None or r.path == "/" + path.lstrip("/"))
            ]

    def body_bytes_requested(self, path: str, size: int) -> int:
        """Sum of bytes covered by GETs for ``path`` (whole file for non-range GETs)."""
        total = 0
        for r in self.gets(path):
            m = _RANGE.match(r.range or "")
            if not m:
                total += size
                continue
            start = int(m.group(1) or 0)
            end = int(m.group(2)) if m.group(2) else size - 1
            total += min(end, size - 1) - start + 1
        return total

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_HEAD(self) -> None:
                self._serve(head=True)

            def do_GET(self) -> None:
                self._serve(head=False)

            def _serve(self, *, head: bool) -> None:
                with server._lock:
                    server.requests.append(
                        Request(
                            self.command, self.path, self.headers.get("Range"), self.headers.get("User-Agent")
                        )
                    )
                    server.active += 1
                    server.max_active = max(server.max_active, server.active)
                    f = server.faults
                    fail = not head and f.fail_first > 0
                    if fail:
                        f.fail_first -= 1
                try:
                    self._respond(head, fail)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with server._lock:
                        server.active -= 1

            def _respond(self, head: bool, fail: bool) -> None:
                f = server.faults
                data = server.files.get(self.path.split("?", 1)[0])
                if f.missing or data is None:
                    self._status(404)
                    return
                if fail:
                    extra = {"Retry-After": f.retry_after} if f.retry_after else {}
                    self._status(f.fail_status, extra)
                    return
                size = len(data)
                start, end, status = 0, size - 1, 200
                rng = self.headers.get("Range")
                if rng and not f.ignore_range:
                    m = _RANGE.match(rng.strip())
                    if m is None:
                        self._status(416, {"Content-Range": f"bytes */{size}"})
                        return
                    if m.group(1):
                        start = int(m.group(1))
                        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                    else:  # suffix range
                        start, end = max(0, size - int(m.group(2))), size - 1
                    if start >= size or start > end:
                        self._status(416, {"Content-Range": f"bytes */{size}"})
                        return
                    status = 206
                self.send_response(status)
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", f'"{hashlib.sha256(data).hexdigest()[:16]}"')
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                if f.drop_after is not None:
                    self.send_header("Connection", "close")
                self.end_headers()
                if head:
                    return
                body = bytearray(data[start : end + 1])
                for off in f.corrupt_offsets:
                    if start <= off <= end:
                        body[off - start] ^= 0xFF
                sent = 0
                while sent < len(body):
                    if f.delay:
                        time.sleep(f.delay)
                    n = min(f.chunk, len(body) - sent)
                    if f.drop_after is not None and sent + n > f.drop_after:
                        self.wfile.write(body[sent : f.drop_after])
                        self.wfile.flush()
                        self.close_connection = True
                        return
                    self.wfile.write(body[sent : sent + n])
                    sent += n

            def _status(self, code: int, headers: dict[str, str] | None = None) -> None:
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "0")
                self.end_headers()

        return Handler


def payload(n: int, seed: int = 0) -> bytes:
    """Deterministic incompressible test bytes."""
    return random.Random(seed).randbytes(n)  # noqa: S311 - test data, not crypto


def sha256_checksum(data: bytes) -> Checksum:
    return Checksum("sha256", hashlib.sha256(data).hexdigest())


class FetcherContract:
    """Behaviour every ``Fetcher`` must show (PLAN §6.5, §8.1)."""

    mismatch_error: ClassVar[type[Exception]] = ChecksumMismatchError

    @pytest.fixture
    def fetcher(self) -> Iterator[Fetcher]:
        raise NotImplementedError("subclass must provide a fetcher fixture")

    @pytest.fixture
    def server(self) -> Iterator[RangeServer]:
        with RangeServer() as s:
            yield s

    @pytest.fixture
    def mirror_server(self) -> Iterator[RangeServer]:
        with RangeServer() as s:
            yield s

    def test_is_fetcher(self, fetcher: Fetcher) -> None:
        assert isinstance(fetcher, Fetcher)

    def test_download_verified_and_atomic(
        self, fetcher: Fetcher, server: RangeServer, tmp_path: Path
    ) -> None:
        data = payload(300_000, seed=1)
        url = server.add("a/file.bin", data)
        dest = tmp_path / "out" / "file.bin"
        remote = RemoteFile(url, "file.bin", len(data), (sha256_checksum(data),))
        result = fetcher.download(remote, dest)
        assert result.path == dest
        assert result.size == len(data)
        assert result.sha256 == hashlib.sha256(data).hexdigest()
        assert result.blake3 == blake3.blake3(data).hexdigest()
        assert result.verdict.status is VerdictStatus.OK
        assert dest.read_bytes() == data
        assert sorted(p.name for p in dest.parent.iterdir()) == ["file.bin"]

    def test_upstream_md5_and_sha1(self, fetcher: Fetcher, server: RangeServer, tmp_path: Path) -> None:
        data = payload(70_000, seed=2)
        url = server.add("b.bin", data)
        sums = (
            Checksum("md5", hashlib.md5(data).hexdigest()),  # noqa: S324 - upstream digest
            Checksum("sha1", hashlib.sha1(data).hexdigest()),  # noqa: S324 - upstream digest
        )
        fetcher.download(RemoteFile(url, "b.bin", None, sums), tmp_path / "b.bin")
        assert (tmp_path / "b.bin").read_bytes() == data

    def test_checksum_mismatch_raises_and_publishes_nothing(
        self, fetcher: Fetcher, server: RangeServer, tmp_path: Path
    ) -> None:
        data = payload(50_000, seed=3)
        url = server.add("c.bin", data)
        dest = tmp_path / "c.bin"
        with pytest.raises(self.mismatch_error):
            fetcher.download(RemoteFile(url, "c.bin", len(data), (Checksum("sha256", "0" * 64),)), dest)
        assert not dest.exists()

    def test_uses_mirror_when_primary_fails(
        self, fetcher: Fetcher, server: RangeServer, mirror_server: RangeServer, tmp_path: Path
    ) -> None:
        data = payload(120_000, seed=4)
        server.add("d.bin", data)
        server.faults.missing = True
        murl = mirror_server.add("d.bin", data)
        remote = RemoteFile(server.url("d.bin"), "d.bin", len(data), (sha256_checksum(data),), (murl,))
        fetcher.download(remote, tmp_path / "d.bin")
        assert (tmp_path / "d.bin").read_bytes() == data

    def test_without_checksum_still_downloads(
        self, fetcher: Fetcher, server: RangeServer, tmp_path: Path
    ) -> None:
        data = payload(10_000, seed=5)
        url = server.add("e.bin", data)
        result = fetcher.download(RemoteFile(url, "e.bin"), tmp_path / "e.bin")
        assert (tmp_path / "e.bin").read_bytes() == data
        assert result.verdict.status is VerdictStatus.UNVERIFIABLE
        assert result.sha256 == hashlib.sha256(data).hexdigest()

    def test_missing_resource_raises_not_found(self, fetcher: Fetcher, server: RangeServer) -> None:
        with pytest.raises(NotFoundError):
            fetcher.get_bytes(server.url("nope.json"))

    def test_get_bytes_and_cap(self, fetcher: Fetcher, server: RangeServer) -> None:
        url = server.add("meta.json", b'{"ok": true}')
        assert fetcher.get_bytes(url) == b'{"ok": true}'
        big = server.add("big.json", b"x" * 5000)
        with pytest.raises(Exception):  # noqa: B017, PT011 - error type is implementation-defined
            fetcher.get_bytes(big, max_bytes=1000)
