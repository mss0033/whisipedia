#!/usr/bin/env python3
"""Bulk pull of arXiv PDFs from the public ``arxiv-dataset`` Google Cloud Storage bucket (PLAN §4, D4).

The bucket is public and not requester-pays, so it costs nothing, unlike arXiv's requester-pays S3
bucket (about $0.09/GB of egress). Every object carries an MD5, which aria2 verifies before it
accepts a file.

The listing is split into chunks (one per archive and month) so aria2 never holds millions of
entries in memory. Each chunk writes a manifest of what it expected, and finished chunks are
recorded in local state, so the pull resumes cleanly after a restart.

A finished chunk is never listed again. When ``wsp ingest`` finds one PDF of a finished chunk that no
longer matches its MD5, it moves that file aside and names it in ``redo-<snapshot>.jsonl``; the pull
fetches exactly the named files again (see :func:`run_redo`), between chunks, so no restart is needed.

Layout: ``<root>/_incoming/arxiv/<snapshot>/{pdf/<archive>/<yymm>/<file>.pdf,manifests/<chunk>.jsonl}``

Usage::

    tools/arxiv_pull.py --root /path/to/library [--snapshot 2026-10] [--parallel 32]
    tools/arxiv_pull.py --root /path/to/library --redo-only   # only the files ingest asked for again
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
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
    return _aria2(aria2, input_file, parallel, rate, name) and not disallowed


BAD_OPTION_EXIT = 28  # aria2: "bad/unrecognized option or unexpected option argument"


def _aria2(aria2: str, input_file: Path, parallel: int, rate: str, label: str) -> bool:
    """Run aria2 over ``input_file``; True when every file in it arrived and matched its MD5.

    Redirects: aria2 follows them (up to 20) and has no option to refuse them; ``--max-redirect``,
    which the 2026-11-10 plan asked for, does not exist in aria2 1.37 (it ends at once with
    "unrecognized option", exit 28, and every chunk would have failed). So a redirect of the bucket
    to another host would still be followed without that host's robots.txt being read: open, see
    the notes of batch 5b-2. Every option below is one the real aria2c accepts (a test runs it)."""
    log = STATE_DIR / "aria2.log"
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
        f"--log={log}",
        "--log-level=warn",
    ]
    code = subprocess.run(cmd, check=False).returncode  # noqa: S603
    if code == BAD_OPTION_EXIT:
        # Not a download that failed: nothing was asked for at all, and every chunk will say the same.
        print(f"{label}: aria2 refused its options (exit {code}); nothing was downloaded", flush=True)
    return code == 0


# --- redo: single files of finished chunks that `wsp ingest` asks for again ---------------------------
#
# Two files in the state directory, each with ONE writer, so neither side can lose the other's entries
# (the pull runs for days; a file both sides rewrote would lose an ingest's entry to the pull's
# rewrite from an older view, 2026-11-10 review):
#
# * ``redo-<snapshot>.jsonl``: written only by ``wsp ingest`` (whole file, tmp + rename, under the
#   ingest lock). One line per file: ``{"chunk", "path", "tries", "failed", "reason"}``; ``path`` is the
#   manifest's (``pdf/<archive>/<yymm>/<file>``), ``tries`` counts the times it failed its MD5 at
#   adoption, ``failed`` is true once ingest gave up on it (then nothing is fetched).
# * ``refetched-<snapshot>.jsonl``: written only by this script, by appending one line
#   ``{"path", "tries"}`` once the file is back and matches the manifest's MD5.
#
# A redo entry is open while no refetched line has its path AND its tries. The pull reads both files
# again before each chunk it pulls (they are small and local), so an entry written during a run is seen
# without a restart; nothing here is ever rewritten, so an entry cannot be dropped by a stale view.

DAMAGED_DIR = "_damaged"  # under the snapshot folder; never under pdf/, so no manifest path reaches it
REDO_DOWNLOAD_TRIES = 3  # failed downloads of one redo entry in this run; then it waits for a restart


def _jsonl(path: Path) -> list[dict[str, Any]]:
    """The JSON objects of a state file; a missing file is empty, a line that does not parse (an append
    cut short, an edit by hand) is skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(cast(dict[str, Any], item))
    return out


def open_redo(snapshot: str) -> list[tuple[str, str, int]]:
    """``(chunk, path, tries)`` of every file ingest asked for again that has not been fetched since."""
    fetched = {
        (str(r.get("path")), r.get("tries")) for r in _jsonl(STATE_DIR / f"refetched-{snapshot}.jsonl")
    }
    out: list[tuple[str, str, int]] = []
    for r in _jsonl(STATE_DIR / f"redo-{snapshot}.jsonl"):
        chunk, path, tries = r.get("chunk"), r.get("path"), r.get("tries")
        if not (isinstance(chunk, str) and isinstance(path, str) and isinstance(tries, int)):
            continue
        if not _CHUNK_NAME.fullmatch(chunk):  # it names a manifest file: never a path of its own
            continue
        if not r.get("failed") and (path, tries) not in fetched:
            out.append((chunk, path, tries))
    return out


_CHUNK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")  # <archive>-<yymm>: no dot, no slash


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as fh:
        while block := fh.read(8 << 20):
            digest.update(block)
    return digest.hexdigest()


def _manifest_md5(manifests: Path, chunk: str) -> dict[str, str]:
    """``path -> md5`` of a chunk manifest (empty when it is not there)."""
    return {str(r.get("path")): str(r.get("md5")).lower() for r in _jsonl(manifests / f"{chunk}.jsonl")}


def _move_aside(dest: Path, path: str, tries: int) -> Path:
    """Move a damaged PDF out of ``pdf/`` to ``_damaged/<archive>/<yymm>/<file>.try<N>`` (never deleted:
    the bytes are evidence of what went wrong on the share)."""
    first = aside = dest / DAMAGED_DIR / f"{path.removeprefix('pdf/')}.try{tries}"
    n = 1
    while aside.exists():  # a rename would replace it: every damaged copy keeps a name of its own
        n += 1
        aside = first.with_name(f"{first.name}.{n}")
    aside.parent.mkdir(parents=True, exist_ok=True)
    os.rename(dest / path, aside)
    return aside


def run_redo(
    snapshot: str,
    dest: Path,
    parallel: int,
    aria2: str,
    rate: str = "0",
    attempts: dict[tuple[str, int], int] | None = None,
) -> int:
    """Fetch again the files ``wsp ingest`` named, and only those; returns how many are back.

    The chunk is NOT listed again: its other files were adopted into the vault and are gone from
    ``_incoming``, so "every manifest entry that is missing" would download the adopted part of the
    chunk a second time (2026-11-10 review). Each named file is looked up in the chunk's manifest for
    its MD5, fetched to its place under ``pdf/``, hashed here, and only then recorded as fetched, which
    is what lets ingest offer the chunk again. A file still in place with the wrong bytes (ingest
    could not move it) is moved aside first; one in place with the right bytes is recorded as it is.

    ``attempts`` (kept by the caller across calls) stops a file whose download keeps failing from being
    tried before every chunk: after :data:`REDO_DOWNLOAD_TRIES` it waits for the next start."""
    attempts = attempts if attempts is not None else {}
    manifests = dest / "manifests"
    known: dict[str, dict[str, str]] = {}
    lines: list[str] = []
    queued: list[tuple[str, int, Path, str]] = []
    back: list[tuple[str, int]] = []
    for chunk, path, tries in open_redo(snapshot):
        key = (path, tries)
        if attempts.get(key, 0) >= REDO_DOWNLOAD_TRIES:
            continue
        parts = path.split("/")
        if len(parts) != 4 or parts[0] != "pdf" or ".." in parts or not path.endswith(".pdf"):
            print(f"redo: {path!r} is not a PDF path of this mirror; not fetched", flush=True)
            attempts[key] = REDO_DOWNLOAD_TRIES
            continue
        if chunk not in known:
            known[chunk] = _manifest_md5(manifests, chunk)
        md5 = known[chunk].get(path)
        if md5 is None:
            print(f"redo: {path} is not in the manifest of {chunk}; not fetched", flush=True)
            attempts[key] = REDO_DOWNLOAD_TRIES
            continue
        target = dest / path
        if target.exists() and not Path(f"{target}.aria2").exists():
            if _md5(target) == md5:
                back.append(key)
                continue
            print(f"redo: {path} is damaged; moved to {_move_aside(dest, path, tries)}", flush=True)
        _, archive, yymm, filename = parts
        url = MEDIA + urllib.parse.quote(f"arxiv/{archive}/pdf/{yymm}/{filename}")
        if not ROBOTS.allows(url):
            print(f"redo: {path} is disallowed by robots.txt, not fetched", flush=True)
            continue
        lines += [url, f"  dir={target.parent}", f"  out={target.name}", f"  checksum=md5={md5}"]
        queued.append((path, tries, target, md5))
    if lines:
        input_file = STATE_DIR / f"redo-{snapshot}.aria2"
        input_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _aria2(aria2, input_file, parallel, rate, "redo")
    for path, tries, target, md5 in queued:
        key = (path, tries)
        if target.exists() and not Path(f"{target}.aria2").exists() and _md5(target) == md5:
            back.append(key)
            continue
        attempts[key] = attempts.get(key, 0) + 1
        left = REDO_DOWNLOAD_TRIES - attempts[key]
        then = (
            f"tried {left} more time(s) in this run"
            if left
            else "not tried again until the pull is restarted"
        )
        print(f"redo: {path} was NOT fetched (try {tries} of ingest's); {then}", flush=True)
    if back:
        with (STATE_DIR / f"refetched-{snapshot}.jsonl").open("a", encoding="utf-8") as fh:
            for path, tries in back:
                fh.write(json.dumps({"path": path, "tries": tries}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        for path, tries in back:
            print(
                f"redo: {path} fetched again and matches its MD5 (try {tries}); ingest can go on", flush=True
            )
    return len(back)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--snapshot", default="2026-10")
    ap.add_argument("--parallel", type=int, default=32)
    ap.add_argument(
        "--rate", default="25M", help="cap on the whole pull, aria2 syntax (25M = 25 MiB/s; 0: none)"
    )
    ap.add_argument(
        "--redo-only",
        action="store_true",
        help="fetch only the files `wsp ingest` asked for again (redo-<snapshot>.jsonl), list no chunk",
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
    done = set(done_file.read_text().split()) if done_file.exists() else set[str]()

    for url in (MEDIA,) if args.redo_only else (API, MEDIA):
        if not ROBOTS.allows(url):
            sys.exit(f"{url}: robots.txt disallows us (or could not be read); not pulling")
    # Files ingest asked for again: now, before every chunk that is pulled (so an entry written while
    # this runs, for days, is seen without a restart) and once more at the end.
    attempts: dict[tuple[str, int], int] = {}

    def redo() -> None:
        try:
            run_redo(args.snapshot, dest, args.parallel, aria2, args.rate, attempts)
        except OSError as exc:  # the share refused for a moment: the next call tries again
            print(f"redo: failed ({exc}); tried again before the next chunk", flush=True)

    redo()
    if args.redo_only:
        return
    chunks = chunk_prefixes()
    print(f"{len(chunks)} chunks, {len(done)} already done", flush=True)
    for i, prefix in enumerate(chunks, 1):
        name = chunk_name(prefix)
        if name in done:
            continue
        redo()
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
    redo()


if __name__ == "__main__":
    main()
