#!/usr/bin/env python3
"""Bulk pull of arXiv PDFs from the public ``arxiv-dataset`` Google Cloud Storage bucket (PLAN §4, D4).

The bucket is public and not requester-pays, so it costs nothing, unlike arXiv's requester-pays S3
bucket (about $0.09/GB of egress). Every object carries an MD5, which aria2 verifies before it
accepts a file.

The listing is split into chunks (one per archive and month) so aria2 never holds millions of
entries in memory. Each chunk writes a manifest of what it expected, and finished chunks are
recorded in local state, so the pull resumes cleanly after a restart.

Layout: ``<root>/_incoming/arxiv/<snapshot>/{pdf/<archive>/<yymm>/<file>.pdf,manifests/<chunk>.jsonl}``

Usage::

    tools/arxiv_pull.py --root /path/to/library [--snapshot 2026-10] [--parallel 32]
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

API = "https://storage.googleapis.com/storage/v1/b/arxiv-dataset/o"
MEDIA = "https://storage.googleapis.com/arxiv-dataset/"
USER_AGENT = "Whisipedia/0.1 (personal offline archive)"
STATE_DIR = Path.home() / ".local/state/whisipedia/arxiv"  # local disk, never the NAS


def _list(prefix: str, *, delimiter: str | None = None) -> Iterator[dict[str, Any]]:
    token: str | None = None
    while True:
        query = {
            "prefix": prefix,
            "maxResults": "1000",
            "fields": "items(name,size,md5Hash),prefixes,nextPageToken",
        }
        if delimiter:
            query["delimiter"] = delimiter
        if token:
            query["pageToken"] = token
        req = urllib.request.Request(  # noqa: S310
            f"{API}?{urllib.parse.urlencode(query)}", headers={"User-Agent": USER_AGENT}
        )
        page = _get_json(req)
        yield page
        token = page.get("nextPageToken")
        if not token:
            return


LIST_TRIES = 6  # a listing page is asked for again after 10, 20, 40, 80, 160 s


def _get_json(req: urllib.request.Request) -> dict[str, Any]:
    """One listing page, asked for again on a transient failure: one 503 on page 7 of a chunk ended
    the whole multi-day pull (2026-10-26 review)."""
    for attempt in range(1, LIST_TRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 (fixed https endpoint)
                return cast(dict[str, Any], json.load(resp))
        except urllib.error.HTTPError as exc:
            if (exc.code < 500 and exc.code != 429) or attempt == LIST_TRIES:
                raise
        except (urllib.error.URLError, OSError, ValueError):
            if attempt == LIST_TRIES:
                raise
        time.sleep(10 * 2 ** (attempt - 1))
    raise AssertionError("unreachable")


def robots_allows(url: str) -> bool:
    """The host's robots.txt for ``url``, as RFC 9309 2.3.1 reads it: a 4xx other than 429 means no
    rules; 5xx, 429 or no answer means everything is disallowed. This job runs without the library's
    packages, so it cannot use the fetcher's check (2026-10-26 review: it read none)."""
    import urllib.error
    import urllib.robotparser

    parts = urllib.parse.urlsplit(url)
    req = urllib.request.Request(  # noqa: S310
        f"{parts.scheme}://{parts.netloc}/robots.txt", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (fixed https endpoint)
            body = resp.read(512 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return 400 <= exc.code < 500 and exc.code != 429
    except (urllib.error.URLError, OSError):
        return False
    rules = urllib.robotparser.RobotFileParser()
    rules.parse(body.splitlines())
    return rules.can_fetch(USER_AGENT, url)


def chunk_prefixes() -> list[str]:
    """``arxiv/arxiv/pdf/<yymm>/`` for the modern scheme, ``arxiv/<archive>/pdf/<yymm>/`` for old archives."""
    archives = [p for page in _list("arxiv/", delimiter="/") for p in page.get("prefixes", [])]
    chunks: list[str] = []
    for archive in archives:
        if archive == "arxiv/pdf/":
            continue
        for page in _list(f"{archive}pdf/", delimiter="/"):
            chunks += page.get("prefixes", [])
    return sorted(chunks)


def chunk_name(prefix: str) -> str:
    _, archive, _, yymm = prefix.strip("/").split("/")
    return f"{archive}-{yymm}"


def run_chunk(prefix: str, dest: Path, manifests: Path, parallel: int, aria2: str, rate: str = "0") -> bool:
    name = chunk_name(prefix)
    items = [it for page in _list(prefix) for it in page.get("items", []) if it["name"].endswith(".pdf")]
    lines: list[str] = []
    with (manifests / f"{name}.jsonl").open("w", encoding="utf-8") as mf:
        for it in items:
            md5 = base64.b64decode(it["md5Hash"]).hex()
            # arxiv/<archive>/pdf/<yymm>/<file> -> pdf/<archive>/<yymm>/<file>
            _, archive, _, yymm, filename = it["name"].split("/")
            rel = f"{archive}/{yymm}/{filename}"
            mf.write(json.dumps({"path": f"pdf/{rel}", "size": int(it["size"]), "md5": md5}) + "\n")
            target = dest / "pdf" / rel
            if (
                target.exists()
                and target.stat().st_size == int(it["size"])
                and not Path(f"{target}.aria2").exists()
            ):
                continue
            lines += [
                MEDIA + urllib.parse.quote(it["name"]),
                f"  dir={target.parent}",
                f"  out={target.name}",
                f"  checksum=md5={md5}",
            ]
    if not lines:
        return True
    input_file = STATE_DIR / f"{name}.aria2"
    input_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [
        aria2,
        f"--input-file={input_file}",
        f"-j{parallel}",
        f"--max-overall-download-limit={rate}",  # the NAS link is shared and paced (0: no cap)
        "-x1",
        "-s1",
        "--continue=true",
        "--check-integrity=true",
        "--file-allocation=none",
        "--auto-file-renaming=false",
        "--allow-overwrite=false",
        "--max-tries=10",
        "--retry-wait=10",
        "--timeout=120",
        f"--user-agent={USER_AGENT}",
        "--console-log-level=error",
        "--summary-interval=0",
        # Its progress line and per-chunk results table went to the journal: thousands of lines a chunk.
        "--show-console-readout=false",
        "--download-result=hide",
        f"--log={STATE_DIR / 'aria2.log'}",
        "--log-level=warn",
    ]
    return subprocess.run(cmd, check=False).returncode == 0  # noqa: S603


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--snapshot", default="2026-10")
    ap.add_argument("--parallel", type=int, default=32)
    ap.add_argument(
        "--rate", default="25M", help="cap on the whole pull, aria2 syntax (25M = 25 MiB/s; 0: none)"
    )
    args = ap.parse_args()
    aria2 = shutil.which("aria2c")
    if not aria2:
        sys.exit("aria2c not found on PATH")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    dest = args.root / "_incoming/arxiv" / args.snapshot
    manifests = dest / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    done_file = STATE_DIR / f"done-{args.snapshot}.txt"
    done = set(done_file.read_text().split()) if done_file.exists() else set()

    for url in (API, MEDIA):
        if not robots_allows(url):
            sys.exit(f"{url}: robots.txt disallows us (or could not be read); not pulling")
    chunks = chunk_prefixes()
    print(f"{len(chunks)} chunks, {len(done)} already done", flush=True)
    for i, prefix in enumerate(chunks, 1):
        name = chunk_name(prefix)
        if name in done:
            continue
        try:
            ok = run_chunk(prefix, dest, manifests, args.parallel, aria2, args.rate)
        except Exception as exc:  # one chunk's listing failing for good must not end the whole pull
            print(f"[{i}/{len(chunks)}] {name}: listing failed ({exc}); next run tries it again", flush=True)
            continue
        print(f"[{i}/{len(chunks)}] {name}: {'ok' if ok else 'INCOMPLETE (will retry next run)'}", flush=True)
        if ok:
            with done_file.open("a", encoding="utf-8") as fh:
                fh.write(name + "\n")


if __name__ == "__main__":
    main()
