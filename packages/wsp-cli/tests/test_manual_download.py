"""tools/manual-download: the owner's download of one large file from a host that drops connections
(the Crystallography Open Database's 26.6 GB archive stalled in the browser, 2026-11-02)."""

from __future__ import annotations

import http.server
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_cli_autonomy import ROOT

TOOL = ROOT / "tools" / "manual-download"
BODY = bytes(range(256)) * 4096  # 1 MiB


class Flaky(http.server.BaseHTTPRequestHandler):
    """Serves BODY with byte ranges; each GET sends at most ``cut`` bytes, then drops the connection.
    ``etag`` changes to simulate a file replaced upstream."""

    cut = 300_000
    etag = '"v1"'
    body = BODY
    gets: list[str] = []  # noqa: RUF012 - shared by the test and the server thread

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.send_header("ETag", self.etag)
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self) -> None:
        rng = self.headers.get("Range") or ""
        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        Flaky.gets.append(rng)
        if rng and self.headers.get("If-Range") not in (None, self.etag):
            start = 0  # the validator no longer matches: the whole file, as a 200
        if start:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(self.body) - 1}/{len(self.body)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(self.body) - start))
        self.send_header("ETag", self.etag)
        self.end_headers()
        self.wfile.write(self.body[start : start + self.cut])
        self.wfile.flush()
        self.close_connection = True  # dropped: fewer bytes than promised


@pytest.fixture
def server() -> Iterator[str]:
    Flaky.gets = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/archives/cod-cifs-mysql.tgz"
    finally:
        httpd.shutdown()


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "WSP_DL_WAIT_MIN": "0", "WSP_DL_TIMEOUT": "5", "WSP_DL_TRIES": "20"}
    return subprocess.run(
        [sys.executable, "-I", str(TOOL), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_a_download_the_host_keeps_dropping_is_continued_to_the_end(server: str, tmp_path: Path) -> None:
    (tmp_path / "cod-cifs-mysql.tgz").touch()  # a browser's empty placeholder
    done = run(server, str(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "cod-cifs-mysql.tgz").read_bytes() == BODY
    assert not (tmp_path / "cod-cifs-mysql.tgz.part").exists()
    assert Flaky.gets[0] == "bytes=0-"
    assert Flaky.gets[1] == "bytes=300000-"  # continued, not begun again
    assert len(Flaky.gets) == 4


def test_a_browsers_partial_is_adopted_and_a_changed_file_begins_again(server: str, tmp_path: Path) -> None:
    firefox = tmp_path / "cod-cifs-mysql.Fgs3pWR3.tgz.part"
    firefox.write_bytes(BODY[:500_000])
    Flaky.cut = 10_000_000
    try:
        done = run(server, str(tmp_path), "--adopt", str(firefox))
        assert done.returncode == 0, done.stdout + done.stderr
        assert (tmp_path / "cod-cifs-mysql.tgz").read_bytes() == BODY
        assert Flaky.gets == ["bytes=500000-"]
        # The same name again, but the host's file changed meanwhile: the bytes kept are not mixed in.
        (tmp_path / "cod-cifs-mysql.tgz").unlink()
        (tmp_path / "cod-cifs-mysql.tgz.part").write_bytes(b"old" * 1000)
        (tmp_path / ".cod-cifs-mysql.tgz.validator").write_text('"v1"\n')  # left by the run that began it
        Flaky.etag, Flaky.gets = '"v2"', []
        Flaky.body = BODY[::-1]
        done = run(server, str(tmp_path))
        assert done.returncode == 0, done.stdout + done.stderr
        assert (tmp_path / "cod-cifs-mysql.tgz").read_bytes() == BODY[::-1]
    finally:
        Flaky.cut, Flaky.etag, Flaky.body = 300_000, '"v1"', BODY
