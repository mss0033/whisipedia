"""``wsp bulk fetch``: download a large source's release straight into ``<vault>/_incoming``.

The orchestrator fetches a release into the local work disk and copies it into the vault: right for
most sources, impossible for one larger than the local disk (American Stories, 340 GB). ``bulk fetch
--source S`` asks the source's adapter for its newest release (``discover``), writes the ingest plan
``_incoming/_plans/targets.d/<S>-<version>.json`` (every file with its upstream checksum and size)
and downloads the files one by one into ``_incoming/<S>/<version>/`` with the library's fetcher
(resumable, checked against upstream, paced like every write into the vault). ``wsp ingest --source
S --version V`` then adopts them by rename, never a copy. A file already there is not fetched again,
so a rerun continues where a failure stopped. ``--only`` fetches the files whose names match a glob.

``--partition P`` fetches one partition of a partitioned source (ADR-0010) whose adapter lists them
(``discover_partition``: PubMed Central, 50,000 PMC ids a partition); its version is ``<day>.<P>``.
``--parallel N`` downloads N files at once (a release of many small files: PMC's 60,000 a partition).
The adapter works in ``<state>/bulk/<source>``, kept between runs (PMC's inventory is read once a day).

**Packed releases.** An adapter with ``pack_name(version)`` (PubMed Central) has its release's many
small files downloaded to the local disk (``<state>/bulk/<source>/pack/<version>/``), stored in one
zip of that name, and only the zip written into ``_incoming`` (paced like every write into the
vault); the plan names the zip with its SHA-256. 45,000 files a partition written one by one into the
NAS made a hundred file creations a second, and every page of the library waited behind them
(2026-10-19: Wikipedia pages failed after 18 s until the lane stopped). Loose files an earlier run
left in ``_incoming/<source>/*.<partition>`` are moved into the local staging first, not fetched again.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import sys
import zipfile
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

from wsp_cli import bootstrap
from wsp_core.errors import FetchError
from wsp_core.model import Checksum, ReleaseRef, RemoteFile, SourceSpec
from wsp_core.ports import Ctx

PLANS_SUBDIR = "targets.d"
LANE = "collections"  # renamed into place only when complete: ingest needs no settle wait
EXIT_OK, EXIT_REFUSED, EXIT_FAILED, EXIT_NOTHING_NEW = 0, 2, 6, 7
LOUD_FILES = 500  # beyond this many files to fetch, progress is reported per thousand
_ALGO = {"sha256": "sha-256", "sha1": "sha-1", "md5": "md5"}


def _usage(args: argparse.Namespace, out: TextIO) -> int:
    print("usage: wsp bulk fetch --source S", file=sys.stderr)
    return EXIT_REFUSED


def plan_targets(release: ReleaseRef) -> list[dict[str, Any]]:
    """The ingest plan (the ``targets.d`` format :mod:`wsp_cli.ingest` reads) of a release."""
    targets: list[dict[str, Any]] = []
    for f in release.files:
        best = next((c for c in f.checksums if c.algorithm in _ALGO), None)
        targets.append(
            {
                "source_id": release.source_id,
                "version": release.version,
                "name": f.name,
                "uris": [f.url],
                "lane": LANE,
                "checksum": [_ALGO[best.algorithm], best.hexdigest] if best else None,
                "size": f.size,
                "meta": {},
            }
        )
    return targets


def write_plan(incoming: Path, release: ReleaseRef) -> Path:
    plans = incoming / "_plans" / PLANS_SUBDIR
    plans.mkdir(parents=True, exist_ok=True)
    path = plans / f"{release.source_id}-{release.version}.json"
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps(plan_targets(release), indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def _spec(cfg: Mapping[str, Any], lay: object, source: str) -> SourceSpec:
    specs = {s.id: s for s in bootstrap.load_sources(cfg, lay)}  # pyright: ignore[reportArgumentType]
    spec = specs.get(source)
    if spec is None:
        raise bootstrap.ConfigError(f"unknown source {source!r}")
    return spec


def _gb(n: int | None) -> str:
    return "?" if n is None else f"{n / 1e9:,.2f} GB"


def cmd_fetch(args: argparse.Namespace, out: TextIO) -> int:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    spec = _spec(cfg, lay, args.source)
    adapter = bootstrap.load_adapters().get(spec.adapter or "")
    if adapter is None:
        print(f"error: {spec.id}: no adapter {spec.adapter!r}", file=sys.stderr)
        return EXIT_REFUSED
    vault_root = bootstrap.paths(cfg).require_vault()
    fetcher = bootstrap.build_fetcher(cfg, pace=bootstrap.ingest_pace(cfg))
    work = bootstrap.paths(cfg).require_state() / "bulk" / spec.id
    work.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(source=spec, fetcher=fetcher, workdir=work, user_agent=bootstrap.user_agent(cfg))
    partition = getattr(args, "partition", None)
    if partition:
        by_partition = getattr(adapter, "discover_partition", None)
        if by_partition is None:
            print(f"error: {spec.id}: its adapter has no partitions", file=sys.stderr)
            return EXIT_REFUSED
        releases = list(by_partition(ctx, partition))
        if not releases:
            out.write(f"{spec.id}: partition {partition} holds nothing; nothing new\n")
            return EXIT_NOTHING_NEW
    else:
        releases = list(adapter.discover(ctx))
    if not releases:
        print(f"error: {spec.id}: discovery found no release", file=sys.stderr)
        return EXIT_FAILED
    release = max(releases, key=lambda r: r.version)
    if (vault_root / "library" / spec.vault_domain / spec.id / release.version / "MANIFEST.json").is_file():
        out.write(f"{spec.id}/{release.version}: already sealed; nothing new\n")
        return EXIT_NOTHING_NEW
    incoming = vault_root / "_incoming"
    dest_dir = incoming / spec.id / release.version
    pack_name = getattr(adapter, "pack_name", None)
    if callable(pack_name):
        return _fetch_packed(
            spec,
            release,
            str(pack_name(release.version)),
            incoming,
            work,
            bootstrap.build_fetcher(cfg),  # into the local disk: the NAS write is paced below
            bootstrap.ingest_pace(cfg),
            max(1, int(getattr(args, "parallel", 1) or 1)),
            partition,
            out,
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    write_plan(incoming, release)
    wanted: list[RemoteFile] = [
        f for f in release.files if not args.only or any(fnmatch.fnmatch(f.name, g) for g in args.only)
    ]
    total = sum(f.size or 0 for f in release.files)
    out.write(
        f"{spec.id}/{release.version}: {len(release.files)} files, {_gb(total)}; fetching {len(wanted)}\n"
    )
    todo = [(i, r) for i, r in enumerate(wanted, 1) if not _whole(dest_dir / r.name, r)]
    parallel = max(1, int(getattr(args, "parallel", 1) or 1))
    quiet = len(todo) > LOUD_FILES  # 60,000 small files: a line a thousand, not a line a file

    def one(item: tuple[int, RemoteFile]) -> tuple[bool, str | None]:
        """Fetch one file: (it failed, the line to print)."""
        i, remote = item
        dest = dest_dir / remote.name
        try:
            fetcher.download(remote, dest)
        except (FetchError, OSError) as exc:  # OSError: the NAS refused a write (EFAULT, 2026-10-18)
            return True, f"  {remote.name}: FAILED {exc}"
        return False, None if quiet else f"  [{i}/{len(wanted)}] {remote.name} {_gb(dest.stat().st_size)}"

    failed = done = 0
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for bad, line in pool.map(one, todo):
            done += 1
            failed += bad
            if line is not None:
                out.write(line + "\n")
            if quiet and done % 1000 == 0:
                out.write(f"  {done:,}/{len(todo):,} fetched\n")
            out.flush()
    if failed:
        out.write(f"{spec.id}/{release.version}: {failed} file(s) failed; run again to continue\n")
        return EXIT_FAILED
    out.write(f"next: wsp ingest --source {spec.id} --version {release.version}\n")
    return EXIT_OK


PACK_CHUNK = 4 * 1024 * 1024


def _fetch_packed(
    spec: SourceSpec,
    release: ReleaseRef,
    name: str,
    incoming: Path,
    work: Path,
    fetcher: Any,
    pace: Callable[[int], None] | None,
    parallel: int,
    partition: str | None,
    out: TextIO,
) -> int:
    """Fetch a release's files into the local disk, pack them into one zip, write that into
    ``_incoming`` (see the module docstring). Resumable: files already staged are not fetched again,
    and a zip already in ``_incoming`` with its planned size is not written again."""
    dest_dir = incoming / spec.id / release.version
    final = dest_dir / name
    plan = incoming / "_plans" / PLANS_SUBDIR / f"{release.source_id}-{release.version}.json"
    if final.is_file() and plan.is_file():
        planned = json.loads(plan.read_text(encoding="utf-8"))
        if [t.get("name") for t in planned] == [name] and planned[0].get("size") == final.stat().st_size:
            out.write(f"{spec.id}/{release.version}: {name} already written\n")
            out.write(f"next: wsp ingest --source {spec.id} --version {release.version}\n")
            return EXIT_OK
    staging = work / "pack" / release.version
    staging.mkdir(parents=True, exist_ok=True)
    wanted = {f.name: f for f in release.files}
    moved = _adopt_loose(incoming / spec.id, partition, release.version, wanted, staging)
    if moved:
        out.write(f"{spec.id}/{release.version}: {moved:,} file(s) an earlier run left in _incoming staged\n")
    todo = [f for f in release.files if not _whole(staging / f.name, f)]
    out.write(
        f"{spec.id}/{release.version}: {len(release.files):,} files packed as {name}; "
        f"fetching {len(todo):,}\n"
    )

    def one(remote: RemoteFile) -> str | None:
        try:
            fetcher.download(remote, staging / remote.name)
        except (FetchError, OSError) as exc:
            return f"  {remote.name}: FAILED {exc}"
        return None

    failed = done = 0
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for line in pool.map(one, todo):
            done += 1
            if line is not None:
                failed += 1
                out.write(line + "\n")
            if done % 1000 == 0:
                out.write(f"  {done:,}/{len(todo):,} fetched\n")
            out.flush()
    if failed:
        out.write(f"{spec.id}/{release.version}: {failed} file(s) failed; run again to continue\n")
        return EXIT_FAILED
    local_zip = staging.parent / f"{release.version}.zip"
    tmp = local_zip.with_suffix(".zip.partial")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(release.files, key=lambda f: f.name):
            z.write(staging / f.name, f.name)
    tmp.replace(local_zip)
    digest, size = _sha256(local_zip), local_zip.stat().st_size
    packed = replace(
        release,
        files=(RemoteFile(f"packed:{spec.id}/{release.version}", name, size, (Checksum("sha256", digest),)),),
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not (final.is_file() and final.stat().st_size == size):
        _paced_copy(local_zip, final, pace)
    write_plan(incoming, packed)
    shutil.rmtree(staging, ignore_errors=True)
    local_zip.unlink(missing_ok=True)
    out.write(f"{spec.id}/{release.version}: {name} {_gb(size)} written\n")
    out.write(f"next: wsp ingest --source {spec.id} --version {release.version}\n")
    return EXIT_OK


def _whole(path: Path, remote: RemoteFile) -> bool:
    """Whether the staged file is complete. A file with no published size or checksum (PMC's) must at
    least read: not empty, a ``.json`` that parses, a ``.txt`` in UTF-8. Being there was enough, and a
    copy cut short was packed and sealed under its own checksum (2026-10-26 review)."""
    if not path.is_file():
        return False
    if remote.size is not None:
        return path.stat().st_size == remote.size
    if remote.checksums:
        return True  # verified when it was fetched
    return _reads(path)


def _reads(path: Path) -> bool:
    """Not empty; a ``.json`` or ``.txt`` (PMC's small article files) also parses. Other files are
    never read whole here (a bulk file can be many gigabytes)."""
    try:
        if path.suffix not in (".json", ".txt"):
            return path.stat().st_size > 0
        data = path.read_bytes()
    except OSError:
        return False
    if not data:
        return False
    try:
        if path.suffix == ".json":
            json.loads(data)
        elif path.suffix == ".txt":
            data.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    return True


def _adopt_loose(
    source_dir: Path, partition: str | None, version: str, wanted: Mapping[str, RemoteFile], staging: Path
) -> int:
    """Move into ``staging`` the release's files an earlier, unpacked run left loose in ``_incoming``
    (any version of the same partition), and remove the folders that empties."""
    if not source_dir.is_dir():
        return 0
    moved = 0
    for folder in sorted(source_dir.iterdir()):
        same = folder.name == version or (partition is not None and folder.name.endswith(f".{partition}"))
        if not (folder.is_dir() and same):
            continue
        for path in folder.iterdir():
            if path.name in wanted and path.is_file() and not (staging / path.name).is_file():
                # Under a temporary name first: a copy cut short (a kill, the share) left a truncated
                # file under the final name, taken as whole on the next run (2026-10-26 review). Only
                # a file that reads is adopted; the others are fetched again.
                part = staging / (path.name + ".partial")
                try:  # the bytes only: copying the NAS's extended attributes failed (EAGAIN, 2026-10-19)
                    shutil.copyfile(path, part)
                    if not _whole(part, wanted[path.name]):
                        part.unlink(missing_ok=True)
                        continue
                    os.replace(part, staging / path.name)
                    path.unlink()
                except OSError:
                    part.unlink(missing_ok=True)  # fetched instead
                    continue
                moved += 1
        if folder.name != version and not any(folder.iterdir()):
            folder.rmdir()  # and its plan: nothing of that version is left to ingest
            plan = source_dir.parent / "_plans" / PLANS_SUBDIR / f"{source_dir.name}-{folder.name}.json"
            plan.unlink(missing_ok=True)
    return moved


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(PACK_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _paced_copy(src: Path, dest: Path, pace: Callable[[int], None] | None) -> None:
    """Copy into the vault at the library's write budget, renamed into place when whole."""
    tmp = dest.with_name(dest.name + ".partial")
    with src.open("rb") as fin, tmp.open("wb") as fout:
        while chunk := fin.read(PACK_CHUNK):
            if pace is not None:
                pace(len(chunk))
            fout.write(chunk)
    tmp.replace(dest)


def register(sub: Any, add: Any) -> None:
    """Add ``wsp bulk ...`` to the CLI parser (called from :func:`wsp_cli.main.build_parser`)."""
    bu = add("bulk", _usage, "Large releases fetched straight into the vault's _incoming.")
    bu_sub = bu.add_subparsers(dest="bulk_command", required=True, metavar="COMMAND")
    s = add(
        "fetch",
        cmd_fetch,
        "Fetch a source's newest release into <vault>/_incoming (then: wsp ingest).",
        bu_sub,
    )
    s.add_argument("--source", required=True)
    s.add_argument(
        "--only", action="append", default=[], help="fetch only files matching this glob (repeatable)"
    )
    s.add_argument("--partition", help="one partition of a partitioned source (ADR-0010), e.g. p0193")
    s.add_argument("--parallel", type=int, default=1, help="files downloaded at once (default 1)")
