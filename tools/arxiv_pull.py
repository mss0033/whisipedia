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
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

# The library's RFC 9309 reader (wildcards, longest match). The job runs with ``python3 -I`` and without
# the packages installed, so their source in this checkout is put on the path by hand; it needs only
# the standard library.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "wsp-core" / "src"))
from wsp_core.robots import Robots
from wsp_core.robots import parse as parse_robots

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
        url = f"{API}?{urllib.parse.urlencode(query)}"
        if not ROBOTS.allows(url):
            raise RobotsDisallowedError(f"{url}: robots.txt disallows us (or could not be read)")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
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


ROBOTS_TTL = 24 * 3600.0  # RFC 9309 2.4: a robots.txt is not used for more than 24 hours
ROBOTS_RETRY = 600.0  # a failed re-read is tried again after 10 minutes, not on every object
DISALLOW_ALL = parse_robots(b"User-agent: *\nDisallow: /\n")


class RobotsDisallowedError(Exception):
    """robots.txt does not let us fetch a URL the pull needs (or could not be read)."""


class RobotsCache:
    """Each host's robots.txt as RFC 9309 reads it (2.3.1), by the library's reader: a file is obeyed;
    a 4xx other than 429 means no rules; a 5xx, a 429 or no answer disallows everything. A file is
    read again once it is 24 hours old (RFC 9309 2.4); if it cannot be read then, the copy we have
    stays in force (2.4 allows it) and the read is tried again 10 minutes later. 2026-10-26 review:
    the job read no robots.txt at all.

    2026-11-04 trawl: robots.txt was then read once per multi-day run, for the bucket's root only, with
    ``urllib.robotparser`` (which reads ``*`` and ``$`` literally and takes the first matching rule,
    not the longest; see :mod:`wsp_core.robots`); the objects aria2 fetched were never checked. A failed
    re-read must be timed too: a chunk checks thousands of objects, and a re-read tried at each would
    ask a failing host for robots.txt once per object, up to 60 s apiece (2026-11-04 review)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        # "scheme://host" -> (rules, read at); a failed read is dated so the next try is ROBOTS_RETRY on
        self._held: dict[str, tuple[Robots | None, float]] = {}

    @staticmethod
    def _read(site: str) -> tuple[bool, Robots | None]:
        """``(read, rules)``; ``read`` is False when robots.txt was unreachable."""
        req = urllib.request.Request(f"{site}/robots.txt", headers={"User-Agent": USER_AGENT})  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (fixed https endpoint)
                return True, parse_robots(resp.read(512 * 1024))
        except urllib.error.HTTPError as exc:
            return 400 <= exc.code < 500 and exc.code != 429, None
        except (urllib.error.URLError, OSError):
            return False, None

    def allows(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        site = f"{parts.scheme}://{parts.netloc.lower()}"
        now = self.clock()
        held = self._held.get(site)
        if held is None or now - held[1] >= ROBOTS_TTL:
            read, rules = self._read(site)
            if read:
                held = self._held[site] = (rules, now)
            else:  # keep the old copy (never read: everything is disallowed); read again in ROBOTS_RETRY
                kept = held[0] if held is not None else DISALLOW_ALL
                held = self._held[site] = (kept, now - ROBOTS_TTL + ROBOTS_RETRY)
        rules = held[0]
        return rules is None or rules.can_fetch(USER_AGENT, url)


ROBOTS = RobotsCache()


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
    disallowed = 0
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
            url = MEDIA + urllib.parse.quote(it["name"])
            if not ROBOTS.allows(url):
                disallowed += 1  # not fetched; the chunk is not finished, so the next run checks again
                continue
            lines += [
                url,
                f"  dir={target.parent}",
                f"  out={target.name}",
                f"  checksum=md5={md5}",
            ]
    if disallowed:
        print(f"{name}: {disallowed} file(s) disallowed by robots.txt, not fetched", flush=True)
    if not lines:
        return not disallowed
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
    return subprocess.run(cmd, check=False).returncode == 0 and not disallowed  # noqa: S603


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
        if not ROBOTS.allows(url):
            sys.exit(f"{url}: robots.txt disallows us (or could not be read); not pulling")
    chunks = chunk_prefixes()
    print(f"{len(chunks)} chunks, {len(done)} already done", flush=True)
    for i, prefix in enumerate(chunks, 1):
        name = chunk_name(prefix)
        if name in done:
            continue
        try:
            ok = run_chunk(prefix, dest, manifests, args.parallel, aria2, args.rate)
        except RobotsDisallowedError as exc:  # the listing is no longer allowed: stop, nothing marked done
            sys.exit(f"[{i}/{len(chunks)}] {name}: {exc}; not pulling")
        except Exception as exc:  # one chunk's listing failing for good must not end the whole pull
            print(f"[{i}/{len(chunks)}] {name}: listing failed ({exc}); next run tries it again", flush=True)
            continue
        print(f"[{i}/{len(chunks)}] {name}: {'ok' if ok else 'INCOMPLETE (will retry next run)'}", flush=True)
        if ok:
            with done_file.open("a", encoding="utf-8") as fh:
                fh.write(name + "\n")


if __name__ == "__main__":
    main()
