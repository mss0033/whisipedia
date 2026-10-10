"""``Aria2Fetcher``: the same ``Fetcher`` contract, driven by ``aria2c`` (PLAN §6.5).

Every download is described to aria2 as a Metalink v4 we generate from the resolved target (all
mirror URLs in priority order, size, upstream digests and, when Kiwix publishes them, piece hashes),
so aria2 does segmented multi-mirror transfer, real-time piece checks and resume (``.aria2`` control
file) natively. We never trust its verdict alone: after it exits the file is fsynced and re-hashed
by us (SHA-256 + BLAKE3 + upstream algorithms) and only then renamed into place.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from wsp_core.model import RemoteFile
from wsp_fetch.config import FetchConfig, host_key
from wsp_fetch.errors import Aria2Error, ChecksumMismatchError, FetchError
from wsp_fetch.finish import FetchResult, finalize
from wsp_fetch.hashing import hash_file
from wsp_fetch.http import HttpFetcher
from wsp_fetch.metalink import MetalinkFile, MetalinkUrl, build_metalink
from wsp_fetch.target import Target, resolve_target

log = logging.getLogger(__name__)

_EXIT_CHECKSUM = 32  # aria2: "checksum validation failed"
_BAD_PIECE_URI = re.compile(r"Download aborted\. URI=(\S+)")


def find_aria2c() -> str | None:
    return shutil.which("aria2c")


def staging_dir(dest: Path) -> Path:
    return dest.with_name(dest.name + ".partial.d")


class Aria2Fetcher:
    """``Fetcher`` backed by the ``aria2c`` binary found on ``PATH``."""

    def __init__(self, config: FetchConfig | None = None, *, executable: str | None = None) -> None:
        self.config = config or FetchConfig()
        exe = executable or find_aria2c()
        if exe is None:
            raise FetchError("aria2c not found on PATH")
        self.executable = exe
        self._http = HttpFetcher(self.config)  # small documents and Metalink discovery

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Aria2Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _run(
        self, target: Target, urls: list[str], stage: Path, name: str
    ) -> subprocess.CompletedProcess[str]:
        meta = MetalinkFile(
            name=name,
            size=target.size,
            checksums=target.checksums,
            pieces=target.pieces,
            urls=tuple(MetalinkUrl(u, priority=i + 1) for i, u in enumerate(urls)),
        )
        meta_path = stage / "request.meta4"
        meta_path.write_bytes(build_metalink([meta]))
        hosts = {host_key(u) for u in urls}
        per_host = min(self.config.connections_for(h) for h in hosts)
        split = sum(self.config.connections_for(h) for h in hosts)
        cmd = [
            self.executable,
            "--no-conf=true",
            f"--metalink-file={meta_path}",
            f"--dir={stage}",
            "--continue=true",
            "--allow-overwrite=false",
            "--auto-file-renaming=false",
            "--check-integrity=true",
            "--realtime-chunk-checksum=true",
            f"--max-connection-per-server={per_host}",
            f"--split={split}",
            f"--min-split-size={max(1 << 20, self.config.min_piece_size)}",
            "--uri-selector=adaptive",
            f"--user-agent={self.config.user_agent}",
            f"--max-tries={self.config.max_attempts}",
            f"--retry-wait={max(1, round(self.config.backoff_base * 4))}",
            f"--connect-timeout={round(self.config.connect_timeout)}",
            f"--timeout={round(self.config.read_timeout)}",
            "--file-allocation=none",
            "--auto-save-interval=1",
            "--console-log-level=warn",
            "--enable-color=false",
            "--summary-interval=0",
            "--download-result=hide",
            "--remote-time=false",
        ]
        if self.config.bandwidth_limit:
            cmd.append(f"--max-overall-download-limit={self.config.bandwidth_limit}")
        log.info("aria2c: %s from %d source(s)", name, len(urls))
        return subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603 - no shell

    def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        return self._http.get_bytes(url, max_bytes=max_bytes)

    def download(self, remote: RemoteFile, dest: Path) -> Path:
        return self.fetch(remote, dest).path

    def fetch(self, remote: RemoteFile, dest: Path) -> FetchResult:
        t0 = time.monotonic()
        target = resolve_target(remote, lambda u, n: self._http.get_bytes(u, max_bytes=n), self.config)
        stage = staging_dir(dest)
        stage.mkdir(parents=True, exist_ok=True)
        urls = list(target.urls)
        out = stage / dest.name
        for run in range(1, self.config.max_attempts + 1):
            proc = self._run(target, urls, stage, dest.name)
            if proc.returncode == 0 and out.is_file():
                break
            if proc.returncode == _EXIT_CHECKSUM:
                shutil.rmtree(stage, ignore_errors=True)
                algo = target.checksums[0].algorithm if target.checksums else "piece"
                want = target.checksums[0].hexdigest if target.checksums else ""
                raise ChecksumMismatchError(target.url, algo, want, "aria2c: checksum validation failed")
            log_text = proc.stderr or proc.stdout
            bad: set[str] = set(_BAD_PIECE_URI.findall(log_text)) if "checksum" in log_text.lower() else set()
            if not bad or run == self.config.max_attempts:
                raise Aria2Error(proc.returncode, log_text.strip()[-2000:])
            # aria2 aborts on a bad piece instead of switching mirror; resume without the culprit(s).
            log.warning("aria2c: bad piece from %s; resuming without it", ", ".join(sorted(bad)))
            urls = [u for u in urls if u not in bad] or urls
        with out.open("rb") as fh:
            os.fsync(fh.fileno())
        digests = hash_file(out, [c.algorithm for c in target.checksums])
        finalize(
            target.url,
            out,
            dest,
            expected_size=target.size,
            checksums=target.checksums,
            digests=digests,
            readback=self.config.verify_readback,
        )
        shutil.rmtree(stage, ignore_errors=True)
        size = dest.stat().st_size
        return FetchResult(dest, size, digests, target.checksums, size, 0, time.monotonic() - t0)
