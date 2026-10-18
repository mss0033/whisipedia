"""``wsp bulk fetch``: download a large source's release straight into ``<vault>/_incoming``.

The orchestrator fetches a release into the local work disk and copies it into the vault: right for
most sources, impossible for one larger than the local disk (American Stories, 340 GB). ``bulk fetch
--source S`` asks the source's adapter for its newest release (``discover``), writes the ingest plan
``_incoming/_plans/targets.d/<S>-<version>.json`` (every file with its upstream checksum and size)
and downloads the files one by one into ``_incoming/<S>/<version>/`` with the library's fetcher
(resumable, checked against upstream, paced like every write into the vault). ``wsp ingest --source
S --version V`` then adopts them by rename, never a copy. A file already there is not fetched again,
so a rerun continues where a failure stopped. ``--only`` fetches the files whose names match a glob.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from wsp_cli import bootstrap
from wsp_core.errors import FetchError
from wsp_core.model import ReleaseRef, RemoteFile, SourceSpec
from wsp_core.ports import Ctx

PLANS_SUBDIR = "targets.d"
LANE = "collections"  # renamed into place only when complete: ingest needs no settle wait
EXIT_OK, EXIT_REFUSED, EXIT_FAILED, EXIT_NOTHING_NEW = 0, 2, 6, 7
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
    with tempfile.TemporaryDirectory(prefix="wsp-bulk-") as work:
        ctx = Ctx(source=spec, fetcher=fetcher, workdir=Path(work), user_agent=bootstrap.user_agent(cfg))
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
    dest_dir.mkdir(parents=True, exist_ok=True)
    write_plan(incoming, release)
    wanted: list[RemoteFile] = [
        f for f in release.files if not args.only or any(fnmatch.fnmatch(f.name, g) for g in args.only)
    ]
    total = sum(f.size or 0 for f in release.files)
    out.write(
        f"{spec.id}/{release.version}: {len(release.files)} files, {_gb(total)}; fetching {len(wanted)}\n"
    )
    failed = 0
    for i, remote in enumerate(wanted, 1):
        dest = dest_dir / remote.name
        if dest.is_file() and (remote.size is None or dest.stat().st_size == remote.size):
            continue
        try:
            fetcher.download(remote, dest)
        except FetchError as exc:
            failed += 1
            out.write(f"  {remote.name}: FAILED {exc}\n")
            continue
        out.write(f"  [{i}/{len(wanted)}] {remote.name} {_gb(dest.stat().st_size)}\n")
        out.flush()
    if failed:
        out.write(f"{spec.id}/{release.version}: {failed} file(s) failed; run again to continue\n")
        return EXIT_FAILED
    out.write(f"next: wsp ingest --source {spec.id} --version {release.version}\n")
    return EXIT_OK


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
