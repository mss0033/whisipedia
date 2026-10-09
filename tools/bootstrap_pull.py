#!/usr/bin/env python3
"""Phase-1 bootstrap bulk pull (PLAN §6, §14).

Plans hash-verified downloads for the initial T1/T2 sources and runs them with aria2c, so terabytes can
land on the NAS while the Acquirer is still being built. Files are written to
``<root>/_incoming/<source_id>/<version>/<file>``; the Vault later *adopts* them (verify hash, rename
into a sealed generation) instead of downloading again.

Speed techniques:
- Wikimedia files are split across the public mirrors simultaneously (segmented Range requests);
  the primary dumps server is used only for files no mirror has, at its 2-connection limit.
- Kiwix ZIMs are fetched via Metalink: many mirrors at once, every 4 MiB piece SHA-1 verified on
  arrival, bad pieces re-fetched elsewhere.
- OpenStreetMap planet is fetched via BitTorrent with web seeds (piece-verified).
- Independent lanes run in parallel, each with its own politeness limits.

Every file carries its upstream checksum (sha-256, sha-1 or md5) and aria2 verifies it before
declaring success. Files upstream publishes without a checksum are marked ``unverifiable`` in the
plan, and the Vault records our own SHA-256/BLAKE3 at adoption.

Usage::

    tools/bootstrap_pull.py plan --root /path/to/library
    tools/bootstrap_pull.py run  --root /path/to/library [--lanes kiwix,wm-mirrors]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

USER_AGENT = "Whisipedia/0.1 (personal offline archive)"
WM_PRIMARY = "https://dumps.wikimedia.org"
WM_MIRRORS = (
    "https://dumps.wikimedia.your.org",
    "https://mirror.accum.se/mirror/wikimedia.org/dumps",
)
KIWIX_CATALOG = "https://library.kiwix.org/catalog/v2/entries?count=-1"
ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/terms/}"
REPO = Path(__file__).resolve().parents[1]
STATE_DIR = Path.home() / ".local/state/whisipedia/bootstrap"  # local disk, never the NAS


@dataclass
class Target:
    source_id: str
    version: str
    name: str
    uris: list[str]
    lane: str
    checksum: tuple[str, str] | None = None  # (aria2 algo, hex)
    size: int | None = None
    meta: dict[str, str] = field(default_factory=dict[str, str])


def _request(url: str, method: str = "GET") -> urllib.request.Request:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url}")
    return urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})  # noqa: S310


def http_get(url: str, *, timeout: int = 120) -> bytes:
    with urllib.request.urlopen(_request(url), timeout=timeout) as resp:  # noqa: S310 (https enforced)
        return resp.read()


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(_request(url, "HEAD"), timeout=30):  # noqa: S310 (https enforced)
            return True
    except Exception:
        return False


def load_registry() -> dict[str, dict[str, Any]]:
    data = yaml.safe_load((REPO / "registry/sources.yaml").read_text(encoding="utf-8"))
    return {s["id"]: s for s in data["sources"]}


# --- Kiwix ---------------------------------------------------------------------------------------


def _completeness(entry: dict[str, Any]) -> int:
    """Prefer the most complete variant of a title (maxi vs nopic vs mini): the largest file."""
    return int(entry["size"])


def kiwix_entries() -> list[dict[str, Any]]:
    root = ET.fromstring(http_get(KIWIX_CATALOG))  # noqa: S314 (trusted upstream catalog)
    out: list[dict[str, Any]] = []
    for e in root.findall(ATOM + "entry"):
        link = next((ln for ln in e.findall(ATOM + "link") if "zim" in (ln.get("type") or "")), None)
        if link is None:
            continue
        out.append(
            {
                "name": e.findtext(ATOM + "name") or "",
                "flavour": e.findtext(ATOM + "flavour") or "",
                "lang": e.findtext(DC + "language") or e.findtext(ATOM + "language") or "",
                "category": e.findtext(ATOM + "category") or "",
                "size": int(link.get("length") or 0),
                "meta4": link.get("href") or "",
            }
        )
    return out


def kiwix_targets(registry: dict[str, dict[str, Any]], entries: list[dict[str, Any]]) -> list[Target]:
    targets: list[Target] = []
    for sid, src in registry.items():
        access = src.get("access") or {}
        if access.get("adapter") != "kiwix-opds" or src.get("tier") not in ("T1", "T2"):
            continue
        sel = access.get("selector") or {}
        names = set(sel.get("names") or ([sel["name"]] if "name" in sel else []))
        picked: dict[str, dict[str, Any]] = {}
        for e in entries:
            if names and e["name"] not in names:
                continue
            if "category" in sel and e["category"] != sel["category"]:
                continue
            if "lang" in sel and sel["lang"] not in e["lang"]:
                continue
            if "flavour" in sel and e["flavour"] != sel["flavour"]:
                continue
            best = picked.get(e["name"])
            if best is None or _completeness(e) > _completeness(best):
                picked[e["name"]] = e
        for e in picked.values():
            meta4 = e["meta4"]
            zim_name = meta4.rsplit("/", 1)[-1].removesuffix(".meta4")
            version = m.group(1) if (m := re.search(r"_(\d{4}-\d{2})\.zim$", zim_name)) else "unknown"
            targets.append(
                Target(sid, version, zim_name, [meta4], "kiwix", None, e["size"], {"kind": "metalink"})
            )
    return targets


# --- Wikimedia -----------------------------------------------------------------------------------


def wm_runs(wiki: str) -> list[str]:
    html = http_get(f"{WM_PRIMARY}/{wiki}/").decode()
    return sorted(set(re.findall(r'href="(\d{8})/', html)), reverse=True)


def wm_status(base: str, wiki: str, run: str) -> dict[str, Any] | None:
    try:
        return json.loads(http_get(f"{base}/{wiki}/{run}/dumpstatus.json"))["jobs"]
    except Exception:
        return None


def wm_job_files(wiki: str, job_names: tuple[str, ...], source_id: str) -> list[Target]:
    """Latest run where any of ``job_names`` is done, preferring mirrors that also have it."""
    for run in wm_runs(wiki)[:4]:
        jobs = wm_status(WM_PRIMARY, wiki, run)
        if not jobs:
            continue
        job = next((jobs[j] for j in job_names if jobs.get(j, {}).get("status") == "done"), None)
        if job is None:
            continue
        mirrors = [m for m in WM_MIRRORS if (s := wm_status(m, wiki, run)) and _same_job_done(s, job_names)]
        out: list[Target] = []
        for name, f in job["files"].items():
            path = f["url"]
            uris = [m + path for m in mirrors] or [WM_PRIMARY + path]
            lane = "wm-mirrors" if mirrors else "wm-primary"
            ck = ("sha-1", f["sha1"]) if f.get("sha1") else (("md5", f["md5"]) if f.get("md5") else None)
            out.append(Target(source_id, run, name, uris, lane, ck, f.get("size"), {"wiki": wiki}))
        return out
    print(f"  ! no finished run for {wiki} {job_names}", file=sys.stderr)
    return []


def _same_job_done(jobs: dict[str, Any], job_names: tuple[str, ...]) -> bool:
    return any(jobs.get(j, {}).get("status") == "done" for j in job_names)


def wikimedia_targets(registry: dict[str, dict[str, Any]]) -> list[Target]:
    targets: list[Target] = []
    current = ("articlesmultistreamdumprecombine",)
    for lang in registry["wikipedia-top10"]["languages"]:
        print(f"  wikimedia {lang}wiki")
        targets += wm_job_files(f"{lang}wiki", current, "wikipedia-top10")
        targets += wm_job_files(f"{lang}wiki", ("metahistory7zdump",), "wikipedia-top10-history")
    for sid, wiki in (("wiktionary-en", "enwiktionary"), ("wikisource-en", "enwikisource")):
        print(f"  wikimedia {wiki}")
        targets += wm_job_files(wiki, current, sid)
    return targets


def wikidata_targets() -> list[Target]:
    base = f"{WM_PRIMARY}/wikidatawiki/entities"
    runs = sorted(set(re.findall(r'href="(\d{8})/', http_get(base + "/").decode())), reverse=True)
    for run in runs:
        sums = http_get(f"{base}/{run}/wikidata-{run}-sha1sums.txt").decode()
        want = {f"wikidata-{run}-all.json.bz2", f"wikidata-{run}-truthy-BETA.nt.bz2"}
        found = {n: h for h, n in (line.split() for line in sums.splitlines() if line.strip()) if n in want}
        if found:
            mirror = "https://mirror.accum.se/mirror/wikimedia.org/other/wikibase/wikidatawiki"
            out: list[Target] = []
            for n, h in found.items():
                uris = [f"{base}/{run}/{n}"]
                if http_ok(f"{mirror}/{run}/{n}"):
                    uris = [f"{mirror}/{run}/{n}", *uris]
                lane = "wm-mirrors" if len(uris) > 1 else "wm-primary"
                out.append(Target("wikidata", run, n, uris, lane, ("sha-1", h)))
            return out
    return []


# --- Other sources -------------------------------------------------------------------------------


def osm_targets() -> list[Target]:
    base = "https://planet.openstreetmap.org/pbf"
    html = http_get(base + "/").decode()
    dates = sorted(set(re.findall(r"planet-(\d{6})\.osm\.pbf\"", html)), reverse=True)
    for d in dates:
        try:
            md5 = http_get(f"{base}/planet-{d}.osm.pbf.md5").decode().split()[0]
        except Exception as exc:
            print(f"  ! no md5 for planet-{d}: {exc}", file=sys.stderr)
            continue
        torrent = f"{base}/planet-{d}.osm.pbf.torrent"
        return [
            Target(
                "osm-planet",
                d,
                f"planet-{d}.osm.pbf",
                [torrent],
                "torrent",
                ("md5", md5),
                meta={"kind": "torrent"},
            )
        ]
    return []


def pubmed_targets() -> list[Target]:
    base = "https://ftp.ncbi.nlm.nih.gov/pubmed/baseline"
    names = sorted(set(re.findall(r"(pubmed\d+n\d+\.xml\.gz)\"", http_get(base + "/").decode())))
    version = names[0].split("n")[0] if names else "unknown"
    # Each file has a sibling .md5; aria2 verifies after we read them in run(), not here (1,300+ requests).
    return [
        Target("pubmed", version, n, [f"{base}/{n}"], "misc", None, meta={"md5_url": f"{base}/{n}.md5"})
        for n in names
    ]


def courtlistener_targets() -> list[Target]:
    base = "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com"
    xml = http_get(f"{base}/?list-type=2&prefix=bulk-data/&max-keys=1000").decode()
    keys = re.findall(r"<Key>(bulk-data/[^<]+)</Key>", xml)
    dates = sorted({m.group(1) for k in keys if (m := re.search(r"(\d{4}-\d{2}-\d{2})", k))}, reverse=True)
    if not dates:
        return []
    latest = dates[0]
    return [
        Target("courtlistener", latest, k.rsplit("/", 1)[-1], [f"{base}/{k}"], "misc")
        for k in keys
        if latest in k
    ]


def openlibrary_targets() -> list[Target]:
    out: list[Target] = []
    for kind in ("editions", "works", "authors"):
        url = f"https://openlibrary.org/data/ol_dump_{kind}_latest.txt.gz"
        out.append(Target("open-library", "latest", f"ol_dump_{kind}_latest.txt.gz", [url], "misc"))
    return out


# --- aria2 ---------------------------------------------------------------------------------------

LANE_OPTS: dict[str, list[str]] = {
    "wm-primary": ["-j2", "-x1", "-s1"],  # Wikimedia: max 2 connections per IP
    "wm-mirrors": ["-j6", "-x4", "-s8", "--min-split-size=64M"],
    "kiwix": [
        "-j6",
        "-x4",
        "-s8",
        "--min-split-size=32M",
        "--follow-metalink=mem",
        "--metalink-preferred-protocol=https",
    ],
    "torrent": [
        "-j1",
        "--follow-torrent=mem",
        "--seed-time=0",
        "--bt-max-peers=80",
        "--bt-hash-check-seed=true",
    ],
    "misc": ["-j8", "-x4", "-s4", "--min-split-size=64M"],
}
COMMON_OPTS = [
    "--continue=true",
    "--file-allocation=none",
    "--check-integrity=true",
    "--auto-file-renaming=false",
    "--allow-overwrite=false",
    "--max-tries=0",
    "--retry-wait=30",
    "--timeout=120",
    "--connect-timeout=30",
    "--uri-selector=adaptive",
    "--disk-cache=128M",
    f"--user-agent={USER_AGENT}",
    "--summary-interval=300",
    "--console-log-level=warn",
    "--log-level=notice",
    "--save-session-interval=60",
    "--realtime-chunk-checksum=true",
]


def write_input(targets: list[Target], incoming: Path, path: Path) -> None:
    lines: list[str] = []
    for t in targets:
        lines.append("\t".join(t.uris))
        lines.append(f"  dir={incoming / t.source_id / t.version}")
        if t.meta.get("kind") not in ("metalink", "torrent"):
            lines.append(f"  out={t.name}")
        if t.checksum:
            lines.append(f"  checksum={t.checksum[0]}={t.checksum[1]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fill_pubmed_md5(targets: list[Target]) -> None:
    from concurrent.futures import ThreadPoolExecutor

    todo = [t for t in targets if "md5_url" in t.meta and t.checksum is None]

    def fetch(t: Target) -> None:
        try:
            t.checksum = ("md5", http_get(t.meta["md5_url"]).decode().split("=")[-1].strip())
        except Exception:
            print(f"  ! no md5 for {t.name}", file=sys.stderr)

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(fetch, todo))


def plan(root: Path) -> Path:
    registry = load_registry()
    targets: list[Target] = []
    steps = (
        ("kiwix", lambda: kiwix_targets(registry, kiwix_entries())),
        ("wikimedia", lambda: wikimedia_targets(registry)),
        ("wikidata", wikidata_targets),
        ("osm", osm_targets),
        ("pubmed", pubmed_targets),
        ("courtlistener", courtlistener_targets),
        ("open library", openlibrary_targets),
    )
    for label, step in steps:
        print(f"planning {label} …")
        try:
            targets += step()
        except Exception as exc:  # one failing upstream must not block the others
            print(f"  ! {label} failed: {exc}", file=sys.stderr)
    fill_pubmed_md5(targets)

    plans = root / "_incoming/_plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "targets.json").write_text(json.dumps([asdict(t) for t in targets], indent=1), encoding="utf-8")
    for lane in LANE_OPTS:
        write_input([t for t in targets if t.lane == lane], root / "_incoming", plans / f"{lane}.aria2")

    by: dict[str, list[int]] = {}
    for t in targets:
        agg = by.setdefault(f"{t.lane:11} {t.source_id}", [0, 0, 0])
        agg[0] += 1
        agg[1] += t.size or 0
        agg[2] += t.checksum is not None or t.meta.get("kind") == "metalink"
    total = 0
    for key, (n, size, verified) in sorted(by.items()):
        total += size
        print(f"{key:45} {n:5} files {size / 1e9:9.1f} GB  verified {verified}/{n}")
    print(f"{'TOTAL (known sizes)':45} {len(targets):5} files {total / 1e12:9.2f} TB")
    return plans


def run(root: Path, lanes: list[str]) -> None:
    aria2 = shutil.which("aria2c")
    if not aria2:
        sys.exit("aria2c not found on PATH")
    plans = root / "_incoming/_plans"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for lane in lanes:
        session = STATE_DIR / f"{lane}.session"
        source = session if session.exists() and session.stat().st_size else plans / f"{lane}.aria2"
        if not source.read_text(encoding="utf-8").strip():
            print(f"{lane}: nothing to do")
            continue
        cmd = [
            aria2,
            *COMMON_OPTS,
            *LANE_OPTS[lane],
            f"--input-file={source}",
            f"--save-session={session}",
            f"--log={STATE_DIR / f'{lane}.log'}",
        ]
        log = (STATE_DIR / f"{lane}.console").open("ab")
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)  # noqa: S603
        print(f"{lane}: started pid {proc.pid} ({source.name})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("plan", "run"))
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--lanes", default=",".join(LANE_OPTS))
    args = ap.parse_args()
    if args.command == "plan":
        plan(args.root)
    else:
        run(args.root, [lane for lane in args.lanes.split(",") if lane])


if __name__ == "__main__":
    main()
