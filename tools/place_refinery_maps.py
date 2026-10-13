"""Copy the Refinery's id-map sidecars to the local map root after the Refinery moved (ADR-0011).

Usage::

    uv run python tools/place_refinery_maps.py OLD_REFINERY NEW_REFINERY MAP_ROOT

For every sealed generation of ``OLD_REFINERY`` (local disk) that has ``_ids.h.npy`` and ``_ids.loc.npy``,
and whose copy in ``NEW_REFINERY`` (the NAS) has the same seal marker, the two files are copied to
``id_map_dir(MAP_ROOT, <new generation>)``. That spares the API from rebuilding them over the
network on its first lookup in each generation. Nothing in either Refinery is changed.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

from wsp_index.refinery import IDS_MAP_H, IDS_MAP_LOC, PARTS_DIR, SUCCESS_FILE, id_map_dir, is_sealed


def generations(root: Path) -> Iterator[Path]:
    for source in sorted(p for p in root.iterdir() if p.is_dir()):
        for gen in sorted(p for p in source.iterdir() if p.is_dir()):
            if gen.name == PARTS_DIR:
                yield from sorted(p for p in gen.iterdir() if p.is_dir() and is_sealed(p))
            elif is_sealed(gen):
                yield gen


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    old_root, new_root, map_root = (Path(a) for a in argv)
    placed = skipped = 0
    for old in generations(old_root):
        if not ((old / IDS_MAP_H).is_file() and (old / IDS_MAP_LOC).is_file()):
            continue
        new = new_root / old.relative_to(old_root)
        if not is_sealed(new) or (new / SUCCESS_FILE).read_bytes() != (old / SUCCESS_FILE).read_bytes():
            print(f"skip {old.relative_to(old_root)}: not the same sealed generation on the new side")
            skipped += 1
            continue
        target = id_map_dir(map_root, new)
        target.mkdir(parents=True, exist_ok=True)
        for name in (IDS_MAP_LOC, IDS_MAP_H):  # loc first: h appearing last completes the pair
            tmp = target / (name + ".tmp")
            shutil.copyfile(old / name, tmp)
            with tmp.open("rb") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, target / name)
        placed += 1
    print(f"placed {placed} id maps under {map_root}; skipped {skipped}")
    return 0 if skipped == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
