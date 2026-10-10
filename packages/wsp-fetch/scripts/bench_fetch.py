"""Benchmark HttpFetcher vs Aria2Fetcher on a real public file (NETWORK; not run in CI).

Downloads the file once per fetcher into a temporary directory on local disk (never the NAS), verifies
it against the Kiwix Metalink digests and prints MB/s. The default is a ~118 MB Kiwix ZIM whose
``.meta4`` provides mirrors and SHA-1 piece hashes::

    uv run python packages/wsp-fetch/scripts/bench_fetch.py
    uv run python packages/wsp-fetch/scripts/bench_fetch.py --url <file-url> --only http
"""

from __future__ import annotations

import argparse
import logging
import tempfile
import time
from pathlib import Path

from wsp_core.model import RemoteFile
from wsp_fetch import Aria2Fetcher, FetchConfig, HttpFetcher, find_aria2c, load_wikimedia_catalog
from wsp_fetch.config import host_key
from wsp_fetch.finish import FetchReport

DEFAULT_URL = "https://download.kiwix.org/zim/wikipedia/wikipedia_ar_history_nopic_2026-04.zim"


def run(name: str, fetcher: HttpFetcher | Aria2Fetcher, remote: RemoteFile, dest: Path) -> FetchReport:
    t0 = time.monotonic()
    result = fetcher.fetch(remote, dest)
    secs = time.monotonic() - t0
    mb = result.size / 1e6
    print(f"{name:6s} {mb:9.1f} MB in {secs:6.2f} s = {mb / secs:7.1f} MB/s  verdict={result.verdict.status}")
    for url, n in sorted(result.per_mirror_bytes.items(), key=lambda kv: -kv[1]):
        if n:
            print(f"         {n / 1e6:8.1f} MB  {url}")
    dest.unlink()
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--only", choices=["http", "aria2"])
    ap.add_argument("--connections", type=int, default=2, help="default per-host cap")
    ap.add_argument("--metalink", action="store_true", help="probe <url>.meta4 on this URL's host too")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    url = str(args.url)
    host = host_key(url)
    catalog = load_wikimedia_catalog()
    cfg = FetchConfig(
        max_connections_per_host=args.connections,
        host_connections=catalog.host_connections(),
        metalink_hosts=("download.kiwix.org", host) if args.metalink else ("download.kiwix.org",),
    )
    remote = catalog.with_mirrors(RemoteFile(url, url.rsplit("/", 1)[-1]))
    with tempfile.TemporaryDirectory(prefix="wsp-bench-") as tmp:
        dest = Path(tmp) / remote.name
        if args.only in (None, "http"):
            with HttpFetcher(cfg) as f:
                run("http", f, remote, dest)
        if args.only in (None, "aria2"):
            if find_aria2c() is None:
                print("aria2  skipped: aria2c not on PATH")
            else:
                with Aria2Fetcher(cfg) as f:
                    run("aria2", f, remote, dest)


if __name__ == "__main__":
    main()
