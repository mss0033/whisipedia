"""Resolve the gold set's titles to canonical doc ids against a Wikipedia ZIM.

    uv run python packages/wsp-oracle/eval/resolve_titles.py --zim <wikipedia_en_all_maxi.zim>

Reads ``gold/queries.src.tsv`` and writes ``gold/queries.jsonl``. Titles become ZIM paths
(spaces to ``_``); redirects are followed so every id names the article the adapter indexes
(``wsp:<source>/<path>``). Unresolvable titles are reported and left out; a query with no
resolvable primary title is dropped with a warning.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from libzim.reader import Archive  # pyright: ignore[reportMissingTypeStubs] - no stubs upstream

HERE = Path(__file__).resolve().parent


def canonical_path(archive: Archive, title: str) -> str | None:
    for path in (title.replace(" ", "_"), title):
        if not archive.has_entry_by_path(path):
            continue
        entry = archive.get_entry_by_path(path)
        for _ in range(8):
            if not entry.is_redirect:
                return str(entry.path)
            entry = entry.get_redirect_entry()
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--zim", type=Path, required=True)
    ap.add_argument("--source", default="wikipedia-en-zim")
    ap.add_argument("--src", type=Path, default=HERE / "gold" / "queries.src.tsv")
    ap.add_argument("--out", type=Path, default=HERE / "gold" / "queries.jsonl")
    args = ap.parse_args(argv)
    archive = Archive(args.zim)
    out: list[str] = []
    missing = 0
    for line in args.src.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        cols = line.split("\t")
        qid, domain, lang, query, primary = cols[:5]
        related = cols[5] if len(cols) > 5 else ""
        relevant: dict[str, int] = {}
        for grade, titles in ((2, primary), (1, related)):
            for title in filter(None, titles.split("|")):
                path = canonical_path(archive, title.strip())
                if path is None:
                    print(f"{qid}: cannot resolve {title!r}", file=sys.stderr)
                    missing += 1
                    continue
                relevant.setdefault(f"wsp:{args.source}/{path}", grade)
        if 2 not in relevant.values():
            print(f"{qid}: no primary document resolved; dropped", file=sys.stderr)
            continue
        row = {"id": qid, "query": query, "lang": lang, "domain": domain, "relevant": relevant}
        out.append(json.dumps(row, ensure_ascii=False))
    args.out.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {len(out)} queries to {args.out} ({missing} unresolved titles)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
