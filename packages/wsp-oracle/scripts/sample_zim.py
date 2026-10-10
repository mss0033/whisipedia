"""Write a benchmark sample of a Wikipedia ZIM as a Refinery generation (local disk).

    uv run python packages/wsp-oracle/scripts/sample_zim.py --zim <file.zim> --out <dir> --docs 50000

The sample contains every document of the gold set (``eval/gold/queries.jsonl``), up to
``--neighbours`` articles linked from each gold document (topical distractors, which make the
retrieval task realistic), and contiguous runs of articles from random positions to fill up to
``--docs``. Documents are extracted exactly like the ``kiwix-opds`` adapter does, so doc ids match
the real index. Output: ``<out>/refinery/<source>/sample/`` plus ``<out>/sample.json``.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import time
import urllib.parse
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from libzim.reader import Archive  # pyright: ignore[reportMissingTypeStubs] - no stubs upstream

from wsp_adapters.html_text import html_to_text
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import DocRecord
from wsp_index import RefineryWriter

HERE = Path(__file__).resolve().parent
GOLD = HERE.parent / "eval" / "gold" / "queries.jsonl"
_HREF = re.compile(rb'href="([^"#?]+)')
_archive: Archive | None = None
_zim_path = ""


def _open(path: str) -> Archive:
    global _archive, _zim_path
    if _archive is None or _zim_path != path:
        _archive, _zim_path = Archive(Path(path)), path
    return _archive


def _record(archive: Archive, entry_path: str, source: str) -> tuple[DocRecord, list[str]] | None:
    if not archive.has_entry_by_path(entry_path):
        return None
    entry = archive.get_entry_by_path(entry_path)
    for _ in range(8):
        if not entry.is_redirect:
            break
        entry = entry.get_redirect_entry()
    item = entry.get_item()
    if not str(item.mimetype).startswith("text/html"):
        return None
    raw = bytes(item.content)
    text = html_to_text(raw)
    links = [urllib.parse.unquote(m.decode("utf-8", "replace")) for m in _HREF.findall(raw)]
    native = str(entry.path)
    rec = DocRecord(
        doc_id=make_doc_id(source, native),
        source_id=source,
        native_id=native,
        title=str(entry.title) or native,
        lang="en",
        license="CC-BY-SA-4.0",
        text=text,
        html_ref=f"zim:{native}",
        content_hash=content_hash(text),
    )
    return rec, links


def _by_paths(args: tuple[str, list[str], str]) -> list[tuple[DocRecord, list[str]]]:
    zim, paths, source = args
    archive = _open(zim)
    out: list[tuple[DocRecord, list[str]]] = []
    for p in paths:
        try:
            r = _record(archive, p, source)
        except (KeyError, RuntimeError):
            r = None
        if r is not None:
            out.append(r)
    return out


def _by_range(args: tuple[str, int, int, str]) -> list[DocRecord]:
    zim, start, count, source = args
    archive = _open(zim)
    out: list[DocRecord] = []
    for i in range(start, min(start + count, archive.entry_count)):
        entry = archive._get_entry_by_id(i)  # pyright: ignore[reportPrivateUsage] - only id-ordered API
        if entry.is_redirect or not entry.path or entry.path != entry.path.strip():
            continue
        item = entry.get_item()
        if not str(item.mimetype).startswith("text/html"):
            continue
        text = html_to_text(bytes(item.content))
        if len(text) < 200:
            continue
        native = str(entry.path)
        out.append(
            DocRecord(
                doc_id=make_doc_id(source, native),
                source_id=source,
                native_id=native,
                title=str(entry.title) or native,
                lang="en",
                license="CC-BY-SA-4.0",
                text=text,
                html_ref=f"zim:{native}",
                content_hash=content_hash(text),
            )
        )
    return out


def _gold_paths(source: str) -> list[str]:
    prefix = f"wsp:{source}/"
    paths: list[str] = []
    for line in GOLD.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rel: dict[str, int] = json.loads(line)["relevant"]
            paths += [d.removeprefix(prefix) for d in rel if d.startswith(prefix)]
    return list(dict.fromkeys(paths))


def _chunks(items: list[str], n: int) -> Iterator[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--zim", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--source", default="wikipedia-en-zim")
    ap.add_argument("--docs", type=int, default=50_000)
    ap.add_argument("--neighbours", type=int, default=25)
    ap.add_argument("--run", type=int, default=400, help="contiguous entries per random position")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    t0 = time.monotonic()
    zim, source = str(args.zim), args.source
    rng = random.Random(args.seed)  # noqa: S311 - benchmark sampling, not security
    docs: dict[str, DocRecord] = {}
    gold = _gold_paths(source)
    with ProcessPoolExecutor(args.workers) as pool:
        links: list[str] = []
        for batch in pool.map(_by_paths, [(zim, c, source) for c in _chunks(gold, 8)]):
            for rec, out_links in batch:
                docs[rec.doc_id] = rec
                pool_links = [x for x in dict.fromkeys(out_links) if "/" not in x and ":" not in x]
                rng.shuffle(pool_links)
                links += pool_links[: args.neighbours]
        print(f"gold: {len(docs)} docs ({time.monotonic() - t0:.0f}s)", flush=True)
        links = [p for p in dict.fromkeys(links) if f"wsp:{source}/{p}" not in docs]
        for batch in pool.map(_by_paths, [(zim, c, source) for c in _chunks(links, 16)]):
            for rec, _ in batch:
                docs.setdefault(rec.doc_id, rec)
        print(f"+neighbours: {len(docs)} docs ({time.monotonic() - t0:.0f}s)", flush=True)
        total = Archive(args.zim).entry_count
        while len(docs) < args.docs:
            starts = [rng.randrange(0, total) for _ in range(args.workers * 4)]
            for batch in pool.map(_by_range, [(zim, s, args.run, source) for s in starts]):
                for rec in batch:
                    docs.setdefault(rec.doc_id, rec)
            print(f"+random: {len(docs)} docs ({time.monotonic() - t0:.0f}s)", flush=True)
    keep = sorted(docs.values(), key=lambda r: r.doc_id)
    gold_ids = {f"wsp:{source}/{p}" for p in gold}
    filler = [r for r in keep if r.doc_id not in gold_ids]
    rng.shuffle(filler)
    budget = max(0, args.docs - len(gold_ids))
    keep = sorted([r for r in keep if r.doc_id in gold_ids] + filler[:budget], key=lambda r: r.doc_id)
    with RefineryWriter(args.out / "refinery", source, "sample") as w:
        w.write(keep)
    words = sum(len(r.text.split()) for r in keep)
    meta = {"zim": args.zim.name, "docs": len(keep), "words": words, "seed": args.seed}
    (args.out / "sample.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(keep)} docs, {words} words in {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
