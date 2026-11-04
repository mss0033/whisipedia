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
    ``etag`` changes to simulate a file replaced upstream; ``then`` (body, etag) replaces it after the
    first GET; ``ranges`` False ignores Range; ``refuse`` answers every request with that status."""

    cut = 300_000
    etag = '"v1"'
    body = BODY
    then: tuple[bytes, str] | None = None
    ranges = True
    refuse = 0
    gets: list[str] = []  # noqa: RUF012 - shared by the test and the server thread
    if_ranges: list[str | None] = []  # noqa: RUF012
    heads = 0

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_HEAD(self) -> None:
        Flaky.heads += 1
        if self.refuse:
            self.send_error(self.refuse)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.send_header("ETag", self.etag)
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self) -> None:
        if self.refuse:
            self.send_error(self.refuse)
            return
        rng = self.headers.get("Range") or ""
        start = int(rng.split("=")[1].split("-")[0]) if rng and self.ranges else 0
        Flaky.gets.append(rng)
        if_range = self.headers.get("If-Range")
        Flaky.if_ranges.append(if_range)
        if rng and if_range is not None and (if_range != self.etag or if_range.startswith("W/")):
            start = 0  # the validator no longer matches (a weak one never does): the whole file, as a 200
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
        if self.then is not None:  # the host's file is replaced while the download runs
            Flaky.body, Flaky.etag = self.then
            Flaky.then = None


@pytest.fixture
def server() -> Iterator[str]:
    Flaky.gets, Flaky.if_ranges, Flaky.heads = [], [], 0
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/archives/cod-cifs-mysql.tgz"
    finally:
        httpd.shutdown()
        Flaky.cut, Flaky.etag, Flaky.body, Flaky.then = 300_000, '"v1"', BODY, None
        Flaky.ranges, Flaky.refuse = True, 0


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


def test_a_weak_etag_is_never_sent_as_if_range(server: str, tmp_path: Path) -> None:
    """2026-11-04 trawl: a weak ETag in If-Range never matches (RFC 9110 13.1.5), so every resume was
    the whole file from byte 0 again, and a host that drops connections was never got past."""
    Flaky.etag = 'W/"v1"'
    done = run(server, str(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "cod-cifs-mysql.tgz").read_bytes() == BODY
    assert Flaky.gets == ["bytes=0-", "bytes=300000-", "bytes=600000-", "bytes=900000-"]
    assert Flaky.if_ranges == [None] * 4


def test_a_file_replaced_during_the_run_is_followed_to_its_new_end(server: str, tmp_path: Path) -> None:
    """2026-11-04 trawl: the first HEAD's size and validator were kept for the whole run; once the
    host's file changed every resume got the whole file again and began from 0, for ever."""
    new = BODY[::-1] + b"x" * 1000  # larger: the old size was 'larger than the file' for ever after
    Flaky.then = (new, '"v2"')
    done = run(server, str(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "cod-cifs-mysql.tgz").read_bytes() == new
    assert Flaky.gets[:3] == ["bytes=0-", "bytes=300000-", "bytes=300000-"]  # the new file's start is kept
    assert Flaky.if_ranges[1:3] == ['"v1"', '"v2"']
    assert len(Flaky.gets) == 5


def test_an_adopted_partial_is_never_overwritten_by_a_whole_file(server: str, tmp_path: Path) -> None:
    """The owner's browser partial is theirs: a host answering with the whole file stops the tool."""
    firefox = tmp_path / "cod-cifs-mysql.Fgs3pWR3.tgz.part"
    firefox.write_bytes(BODY[:500_000])
    Flaky.ranges, Flaky.cut = False, 10_000_000
    done = run(server, str(tmp_path), "--adopt", str(firefox))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "adopted" in done.stdout
    assert (tmp_path / "cod-cifs-mysql.tgz.part").read_bytes() == BODY[:500_000]
    assert not (tmp_path / "cod-cifs-mysql.tgz").exists()


def test_a_refusal_stops_at_once(server: str, tmp_path: Path) -> None:
    """A 403 was retried for ever (WSP_DL_TRIES=0 by default); nothing a retry can change."""
    Flaky.refuse = 403
    done = run(server, str(tmp_path))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "HTTP 403" in done.stdout
    assert Flaky.heads == 1
