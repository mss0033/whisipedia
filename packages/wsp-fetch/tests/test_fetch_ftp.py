"""ftp:// through HttpFetcher (owner decision D16, 2026-10-24): NCBI's bulk files over anonymous FTP,
which has no robots.txt, while the HTTPS host's robots.txt disallows every agent."""

from __future__ import annotations

import ftplib
import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from wsp_core.errors import NotFoundError
from wsp_core.model import Checksum, RemoteFile
from wsp_fetch import ChecksumMismatchError, FetchConfig, HttpFetcher
from wsp_fetch.state import partial_path

DATA = bytes(range(256)) * 4000  # 1 MB
FILES = {"/pubmed/baseline/pubmed26n0001.xml.gz": DATA, "/pubmed/baseline/README.txt": b"read me\n"}
CFG = FetchConfig(robots=True, user_agent="wsp-test/1.0", backoff_base=0.01, backoff_max=0.02, max_attempts=3)


class FakeFTP:
    """Just enough of ftplib.FTP: anonymous login, MLSD, SIZE, RETR with REST, a 550 for unknown
    paths, and ``drops`` transfers that end early (a dropped data connection)."""

    logins: ClassVar[list[tuple[str, str]]] = []
    drops: ClassVar[list[int]] = []  # each RETR pops one: stop after that many bytes
    rests: ClassVar[list[int]] = []

    def __init__(self, timeout: float = 0) -> None:
        self.dir = "/"

    def connect(self, host: str, port: int) -> str:
        assert host == "ftp.ncbi.example"
        return "220 hello"

    def login(self, user: str, passwd: str) -> str:
        FakeFTP.logins.append((user, passwd))
        return "230 ok"

    def voidcmd(self, cmd: str) -> str:
        return "200 ok"

    def cwd(self, path: str) -> str:
        if not any(k.startswith(path) for k in FILES):
            raise ftplib.error_perm("550 no such directory")
        self.dir = path
        return "250 ok"

    def mlsd(self, path: str = "", facts: Any = ()) -> Iterator[tuple[str, dict[str, str]]]:
        yield ".", {"type": "cdir"}
        for k, v in FILES.items():
            if k.startswith(self.dir):
                yield k.rsplit("/", 1)[1], {"type": "file", "size": str(len(v)), "modify": "20260130125412"}

    def size(self, path: str) -> int:
        if path not in FILES:
            raise ftplib.error_perm("550 no such file")
        return len(FILES[path])

    def retrbinary(
        self, cmd: str, callback: Callable[[bytes], None], blocksize: int = 8192, rest: Any = None
    ) -> str:
        path = cmd.removeprefix("RETR ")
        if path not in FILES:
            raise ftplib.error_perm("550 no such file")
        data = FILES[path][int(rest or 0) :]
        FakeFTP.rests.append(int(rest or 0))
        stop = FakeFTP.drops.pop(0) if FakeFTP.drops else None
        for i in range(0, len(data), 65536):
            if stop is not None and i >= stop:
                raise EOFError("data connection dropped")
            callback(data[i : i + 65536])
        return "226 done"

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_ftp(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeFTP.logins, FakeFTP.drops, FakeFTP.rests = [], [], []
    monkeypatch.setattr(ftplib, "FTP", FakeFTP)


BASE = "ftp://ftp.ncbi.example/pubmed/baseline/"


def test_a_directory_is_an_html_index_and_files_are_read_without_robots_or_an_email() -> None:
    with HttpFetcher(CFG) as f:
        index = f.get_bytes(BASE).decode()
        assert '<a href="README.txt">README.txt</a> 2026-01-30 12:54 8' in index
        assert (
            f'<a href="pubmed26n0001.xml.gz">pubmed26n0001.xml.gz</a> 2026-01-30 12:54 {len(DATA)}' in index
        )
        assert "cdir" not in index
        assert 'href="."' not in index
        assert f.get_bytes(BASE + "README.txt") == b"read me\n"
        assert f.head(BASE + "pubmed26n0001.xml.gz").size == len(DATA)
        with pytest.raises(NotFoundError):
            f.get_bytes(BASE + "missing.xml.gz")
    assert {pw for _user, pw in FakeFTP.logins} == {"anonymous@"}  # never an email address


def test_a_dropped_transfer_resumes_and_the_md5_is_checked(tmp_path: Path) -> None:
    md5 = hashlib.md5(DATA).hexdigest()  # the issuer's sidecar digest
    remote = RemoteFile(url=BASE + "pubmed26n0001.xml.gz", name="a.xml.gz", checksums=(Checksum("md5", md5),))
    FakeFTP.drops = [5 * 65536]  # the first transfer ends after 320 KiB
    dest = tmp_path / "a.xml.gz"
    with HttpFetcher(CFG) as f:
        report = f.download(remote, dest)
    assert dest.read_bytes() == DATA
    assert FakeFTP.rests == [0, 5 * 65536]  # the second transfer started where the first stopped
    assert report.size == len(DATA)
    assert not partial_path(dest).exists()
    bad = RemoteFile(url=remote.url, name="b.xml.gz", checksums=(Checksum("md5", "0" * 32),))
    with HttpFetcher(CFG) as f, pytest.raises(ChecksumMismatchError):
        f.download(bad, tmp_path / "b.xml.gz")
    assert not (tmp_path / "b.xml.gz").exists()


def test_a_file_missing_on_every_source_is_not_found(tmp_path: Path) -> None:
    remote = RemoteFile(url=BASE + "gone.xml.gz", name="g", checksums=(Checksum("md5", "0" * 32),))
    with HttpFetcher(CFG) as f, pytest.raises(NotFoundError):
        f.download(remote, tmp_path / "g")
