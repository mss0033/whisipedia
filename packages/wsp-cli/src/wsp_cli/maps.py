"""``wsp maps``: list and fetch the parts of the Atlas's map sources (``docs/components/maps.md``).

``parts --source S`` lists what the source's discovery offers now: one partition per archive (or per
cut of one), with sizes. ``fetch --source S --partition P`` downloads that part into
``<vault>/_incoming/<S>/<cycle>.<P>/`` and writes the ingest plan
``_incoming/_plans/targets.d/<S>-<cycle>.<P>.json``; ``wsp ingest --source S --version <cycle>.<P>``
then seals it. ``<cycle>`` is upstream's version when it has one (a Protomaps build date), else today.

A whole archive is downloaded by the library's fetcher (resumable, MD5-checked against upstream,
paced like every write into the vault). A cut is read chunk by chunk with
``Range`` requests into the local work disk (:mod:`wsp_adapters.mapcut`: resumable, a rerun continues
where a failure stopped), read back (:func:`wsp_adapters.maptiles.check_cut`), then copied into
``_incoming`` with its SHA-256, which the ingest checks again. Either way the file appears under its
final name only when it is whole.

``gazetteer`` builds the Atlas's place index (:mod:`wsp_core.gazetteer`) from the OpenStreetMap planet
in the vault: named nodes that are a settlement or locality or carry a Wikidata id. The planet is read
through a FIFO at ``[index] refinery_read_mb_per_sec`` (an unpaced 95 GB read over the NAS link makes
searches time out), and the file is written on the local disk next to the search index.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast

from wsp_adapters.maptiles import Cut, MapConfigError, MapPart, check_cut, map_parts, map_source
from wsp_cli import bootstrap
from wsp_cli.nasretry import retrying
from wsp_core.errors import FetchError
from wsp_core.gazetteer import Place, place_rank, write_gazetteer
from wsp_core.model import Checksum, RemoteFile, SourceSpec
from wsp_core.partitions import PartitionError, check_partition, partition_generation_id

PLANS_SUBDIR = "targets.d"
LANE = "collections"  # renamed into place only when complete: ingest needs no settle wait
EXIT_OK, EXIT_REFUSED, EXIT_FAILED, EXIT_NOTHING_NEW = 0, 2, 6, 7
COPY_CHUNK = 8 << 20


def _spec(cfg: Mapping[str, Any], lay: object, source: str) -> SourceSpec:
    specs = {s.id: s for s in bootstrap.load_sources(cfg, cast(Any, lay))}
    spec = specs.get(source)
    if spec is None:
        raise bootstrap.ConfigError(f"unknown source {source!r}")
    if map_source(cast(Mapping[str, Any], spec.raw.get("access") or {})) is None:
        raise bootstrap.ConfigError(f"{source} is not a map source (access.adapter: map-tiles)")
    return spec


def _parts(cfg: Mapping[str, Any], spec: SourceSpec) -> list[MapPart]:
    fetcher = bootstrap.build_fetcher(cfg)
    return map_parts(cast(Mapping[str, Any], spec.raw["access"]), lambda url: fetcher.get_bytes(url))


def _gb(n: int | None) -> str:
    return "?" if n is None else f"{n / 1e9:,.1f} GB"


def cmd_parts(args: argparse.Namespace, out: TextIO) -> int:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    spec = _spec(cfg, lay, args.source)
    parts = _parts(cfg, spec)
    if args.json:
        out.write(json.dumps([_part_json(p) for p in parts], indent=1) + "\n")
        return EXIT_OK
    total = 0
    for p in parts:
        total += p.size or 0
        how = "whole, md5" if p.cut is None else "cut " + " ".join(p.cut.args())
        out.write(f"{p.partition:24} {_gb(p.size):>12}  {p.name}  ({how})\n")
    out.write(f"{len(parts)} parts, upstream {_gb(total)} (a cut keeps a share of its upstream)\n")
    return EXIT_OK


def _part_json(p: MapPart) -> dict[str, object]:
    return {
        "partition": p.partition,
        "name": p.name,
        "url": p.url,
        "size": p.size,
        "md5": p.md5,
        "cut": None if p.cut is None else p.cut.args(),
        "version": p.version,
        "bounds": p.bounds,
        "zooms": p.zooms,
    }


def _sealed(vault_root: Path, spec: SourceSpec, version: str) -> bool:
    return (vault_root / "library" / spec.vault_domain / spec.id / version / "MANIFEST.json").is_file()


def write_plan(
    incoming: Path, spec: SourceSpec, version: str, part: MapPart, checksum: Checksum | None, size: int | None
) -> Path:
    """The ingest plan of one part (``targets.d``, the format :mod:`wsp_cli.ingest` reads)."""
    plans = incoming / "_plans" / PLANS_SUBDIR
    plans.mkdir(parents=True, exist_ok=True)
    algo = {"sha256": "sha-256", "md5": "md5"}
    target = {
        "source_id": spec.id,
        "version": version,
        "name": part.name,
        "uris": [part.url],
        "lane": LANE,
        "checksum": [algo[checksum.algorithm], checksum.hexdigest] if checksum else None,
        "size": size,
        "meta": {"partition": part.partition, "cut": None if part.cut is None else part.cut.args()},
    }
    path = plans / f"{spec.id}-{version}.json"
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps([target], indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def reusable_cut(local: Path, cut: Cut) -> bool:
    """A finished cut in the work directory (renamed into place, so whole) that fits ``cut``."""
    building = local.with_name(local.name + ".building")
    return local.is_file() and not building.exists() and check_cut(local, cut, None) is None


def copy_hashed(src: Path, dest: Path, pace: Callable[[int], None] | None) -> str:
    """Copy ``src`` to ``dest`` (via ``dest.partial``, renamed when whole); its SHA-256. Started again
    when the NAS drops the connection for a moment (a part's fsync, 2026-10-24: :mod:`wsp_cli.nasretry`)."""
    return retrying(f"copy {src.name}", lambda: _copy_hashed_once(src, dest, pace))


def _copy_hashed_once(src: Path, dest: Path, pace: Callable[[int], None] | None) -> str:
    tmp = dest.with_name(dest.name + ".partial")
    digest = hashlib.sha256()
    with src.open("rb") as fin, tmp.open("wb") as fout:
        while chunk := fin.read(COPY_CHUNK):
            digest.update(chunk)
            fout.write(chunk)
            if pace is not None:
                pace(len(chunk))
        fout.flush()
        os.fsync(fout.fileno())
    tmp.replace(dest)
    return digest.hexdigest()


def cmd_fetch(args: argparse.Namespace, out: TextIO) -> int:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    spec = _spec(cfg, lay, args.source)
    try:
        partition = check_partition(args.partition.lower())
    except PartitionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        parts = {p.partition: p for p in _parts(cfg, spec)}
    except MapConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    part = parts.get(partition)
    if part is None:
        print(
            f"error: {spec.id} lists no partition {partition!r} (wsp maps parts --source {spec.id})",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    paths = bootstrap.paths(cfg)
    vault_root = paths.require_vault()
    cycle = args.version or part.version or datetime.now(UTC).strftime("%Y-%m-%d")
    version = partition_generation_id(cycle, partition)
    if _sealed(vault_root, spec, version):
        out.write(f"{spec.id}/{version}: already sealed; nothing new\n")
        return EXIT_NOTHING_NEW
    incoming = vault_root / "_incoming"
    dest_dir = incoming / spec.id / version
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / part.name
    pace = bootstrap.ingest_pace(cfg, pool="maps")
    if part.cut is None:
        checksum = Checksum("md5", part.md5) if part.md5 else None
        write_plan(incoming, spec, version, part, checksum, part.size)
        if not dest.is_file():
            fetcher = bootstrap.build_fetcher(cfg, pace=pace)
            remote = RemoteFile(
                url=part.url, name=part.name, size=part.size, checksums=(checksum,) if checksum else ()
            )
            try:
                fetcher.download(remote, dest)
            except FetchError as exc:  # a host's 520, its mirrors exhausted: the lane's next pass retries
                print(f"error: {spec.id}/{partition}: {exc}", file=sys.stderr)
                return EXIT_FAILED
        out.write(f"{spec.id}/{version}: {part.name} {_gb(dest.stat().st_size)} downloaded\n")
    elif not dest.is_file():
        from wsp_adapters.mapcut import Window, cut_archive

        work = (paths.workdir or Path.home() / ".cache" / "whisipedia") / "maps"
        local = work / part.name
        cut = part.cut
        window = Window(cut.min_zoom, cut.max_zoom, cut.bbox)
        fetcher = bootstrap.build_fetcher(cfg)
        get_range = getattr(fetcher, "get_range", None)
        if not callable(get_range):
            print("error: this fetcher cannot read byte ranges (acquirer.fetcher)", file=sys.stderr)
            return EXIT_REFUSED
        out.write(f"{spec.id}/{version}: cutting {' '.join(cut.args())} from {part.url}\n")
        out.flush()
        last = [0.0]

        def progress(done: int, total: int, nbytes: int) -> None:
            now = time.monotonic()
            if now - last[0] >= 60 or done == total:
                last[0] = now
                out.write(f"  {done}/{total} chunks, {_gb(nbytes)}\n")
                out.flush()

        entries: int | None = None
        if reusable_cut(local, cut):
            # Cut by an earlier run that stopped while copying it into _incoming: only the copy is left.
            out.write(f"  {local.name}: cut by an earlier run, {_gb(local.stat().st_size)}\n")
        else:
            try:
                result = cut_archive(
                    lambda off, n: cast(Callable[[str, int, int], bytes], get_range)(part.url, off, n),
                    window,
                    local,
                    work,
                    progress=progress,
                )
            except (OSError, ValueError, FetchError) as exc:
                out.write(f"failed: {part.name}: {exc} (a rerun continues where this stopped)\n")
                return EXIT_FAILED
            entries = result.entries
        problem = check_cut(local, part.cut, entries)
        if problem:
            out.write(f"failed: {part.name}: {problem}\n")
            local.unlink(missing_ok=True)
            return EXIT_FAILED
        size = local.stat().st_size
        sha = copy_hashed(local, dest, pace)
        local.unlink()
        write_plan(incoming, spec, version, part, Checksum("sha256", sha), size)
        out.write(f"{spec.id}/{version}: {part.name} {_gb(size)} cut and copied (sha256 {sha[:16]}...)\n")
    out.write(f"next: wsp ingest --source {spec.id} --version {version}\n")
    return EXIT_OK


GAZETTEER_FILE = Path("gazetteer") / "places.sqlite"
ALIAS_LANGS = ("en", "de", "fr", "es", "it", "pt", "ru", "zh", "ja", "ar", "hi", "nl", "pl", "uk", "ko", "tr")
ALIAS_KEYS = ("int_name", "alt_name", "official_name", "old_name", "short_name")
_QID = re.compile(r"^Q\d+$")


def gazetteer_path(cfg: Mapping[str, Any]) -> Path:
    return bootstrap.paths(cfg).require_index() / GAZETTEER_FILE


# The pump runs in its own process: libosmium blocks in open() and while waiting for data without
# releasing the GIL, so a pump thread in this process would never get to write (a deadlock).
_PUMP = """
import sys, time
src, dst, rate = sys.argv[1], sys.argv[2], float(sys.argv[3]) * 1e6
t0, sent = time.monotonic(), 0
with open(src, "rb") as fin, open(dst, "wb") as fout:
    while chunk := fin.read(8 << 20):
        try:
            fout.write(chunk)
        except BrokenPipeError:
            break
        sent += len(chunk)
        if rate > 0:
            ahead = sent / rate - (time.monotonic() - t0)
            if ahead > 0:
                time.sleep(ahead)
"""


def _paced_fifo(src: Path, fifo: Path, rate_mb: float) -> subprocess.Popen[bytes]:
    """A process copying ``src`` into the FIFO ``fifo`` at ``rate_mb`` MB/s (0: unpaced)."""
    return subprocess.Popen(  # noqa: S603 - this interpreter, our own script
        [sys.executable, "-I", "-c", _PUMP, str(src), str(fifo), str(rate_mb)],
        stdin=subprocess.DEVNULL,
    )


def read_gazetteer_places(pbf: str) -> Iterator[Place]:
    """Named nodes of an OSM file that are a settlement or locality, or carry a Wikidata id."""
    import osmium
    import osmium.filter
    import osmium.io
    import osmium.osm

    from wsp_adapters.osm import _KIND_KEYS, PLACE_KINDS  # pyright: ignore[reportPrivateUsage]

    source = osmium.io.File(pbf, "pbf")
    processor = osmium.FileProcessor(source, osmium.osm.NODE).with_filter(osmium.filter.KeyFilter("name"))
    for node in processor:
        if not isinstance(node, osmium.osm.Node) or not node.location.valid():
            continue
        tags = {t.k: t.v for t in node.tags}
        place = tags.get("place", "")
        qid = tags.get("wikidata", "").strip()
        if not (place in PLACE_KINDS or _QID.match(qid)):
            continue
        name = tags["name"].strip()
        if not name:
            continue
        kind = (
            f"place={place}"
            if place in PLACE_KINDS
            else next((f"{k}={tags[k]}" for k in _KIND_KEYS if k in tags), "node")
        )
        try:
            population = int(re.sub(r"[ ,.]", "", tags.get("population", "")) or 0) or None
        except ValueError:
            population = None
        aliases = [tags[f"name:{lang}"] for lang in ALIAS_LANGS if tags.get(f"name:{lang}")]
        aliases += [tags[k] for k in ALIAS_KEYS if tags.get(k)]
        wiki = tags.get("wikipedia", "").strip() or None
        yield Place(
            osm=f"node/{node.id}",
            name=name,
            kind=kind,
            lat=float(node.location.lat),
            lon=float(node.location.lon),
            rank=place_rank(kind, population, bool(wiki)),
            population=population,
            qid=qid if _QID.match(qid) else None,
            wikipedia=wiki,
            aliases=tuple(a for a in dict.fromkeys(aliases) if a != name),
        )


def cmd_gazetteer(args: argparse.Namespace, out: TextIO) -> int:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    target = gazetteer_path(cfg)
    if args.pbf:
        pbf = Path(args.pbf)
    else:
        specs = bootstrap.load_sources(cfg, lay)
        vault = bootstrap.build_vault(cfg, specs, bootstrap.load_signer(cfg, lay, create=False), ledger=False)
        gen = vault.current("osm-planet")
        if gen is None:
            print("error: no current osm-planet generation in the vault", file=sys.stderr)
            return EXIT_REFUSED
        root = Path(vault.generation_path("osm-planet", gen.generation_id))
        found = [root / Path(str(a.relpath)) for a in gen.artifacts if str(a.relpath).endswith(".osm.pbf")]
        if not found:
            print(f"error: osm-planet/{gen.generation_id} holds no .osm.pbf", file=sys.stderr)
            return EXIT_REFUSED
        pbf = found[0]
    configured = bootstrap.section(cfg, "index").get("refinery_read_mb_per_sec", 30)
    rate = float(args.read_mb_per_sec if args.read_mb_per_sec is not None else configured)
    out.write(f"gazetteer: reading {pbf} at {rate:g} MB/s into {target}\n")
    out.flush()
    with tempfile.TemporaryDirectory(prefix="wsp-gazetteer-") as tmp:
        fifo = Path(tmp) / "planet.osm.pbf"
        os.mkfifo(fifo)
        pump = _paced_fifo(pbf, fifo, rate)
        t0 = time.monotonic()
        try:
            count = write_gazetteer(target, _progress(read_gazetteer_places(str(fifo)), out))
        finally:
            if pump.poll() is None:
                pump.terminate()
            pump.wait(timeout=30)
    out.write(f"gazetteer: {count:,} places in {(time.monotonic() - t0) / 60:.1f} min -> {target}\n")
    return EXIT_OK


def _progress(places: Iterator[Place], out: TextIO, every: int = 500_000) -> Iterator[Place]:
    for i, p in enumerate(places, 1):
        if i % every == 0:
            out.write(f"  {i:,} places (last: {p.name})\n")
            out.flush()
        yield p


def _usage(args: argparse.Namespace, out: TextIO) -> int:
    out.write("usage: wsp maps {parts,fetch,gazetteer} ...\n")
    return 2


def register(sub: Any, add: Any) -> None:
    """Add ``wsp maps ...`` to the CLI parser (called from :func:`wsp_cli.main.build_parser`)."""
    ma = add("maps", _usage, "Map sources of the Atlas: list and fetch their parts.")
    ma_sub = ma.add_subparsers(dest="maps_command", required=True, metavar="COMMAND")
    s = add("parts", cmd_parts, "List a map source's parts (partitions) and their sizes.", ma_sub)
    s.add_argument("--source", required=True)
    s.add_argument("--json", action="store_true")
    s = add("fetch", cmd_fetch, "Fetch one part into <vault>/_incoming (then: wsp ingest).", ma_sub)
    s.add_argument("--source", required=True)
    s.add_argument("--partition", required=True)
    s.add_argument("--version", default=None, help="the cycle (default: upstream's version, else today)")
    s = add(
        "gazetteer", cmd_gazetteer, "Build the Atlas's place index from the OSM planet in the vault.", ma_sub
    )
    s.add_argument("--pbf", default=None, help="an .osm.pbf to read instead of the vault's planet")
    s.add_argument(
        "--read-mb-per-sec",
        type=float,
        default=None,
        help="pace the read (default: refinery_read_mb_per_sec)",
    )
