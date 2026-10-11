"""Shared test helpers for wsp-vault (imported by conftest and test modules)."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

SOURCE = "test-src"
GEN = "2026-10-01"

FILES: dict[str, bytes] = {
    "upstream/alpha.zim": bytes(range(256)) * 64,
    "upstream/beta.txt": b"hello whisipedia\n" * 50,
    "upstream/sub/gamma.json": b'{"k": "v"}\n',
}


class FakeParity:
    """ParityProvider test double: keeps whole copies and repairs like par2 (damaged -> ``*.1``)."""

    def __init__(self) -> None:
        self.repair_calls = 0

    def create(self, files: Sequence[Path], out_dir: Path, percent: int) -> Sequence[Path]:
        index: dict[str, str] = {}
        out: list[Path] = []
        for i, f in enumerate(files):
            copy = out_dir / f"vol{i:03d}.fakepar"
            shutil.copyfile(f, copy)
            index[f.name] = copy.name
            out.append(copy)
        idx = out_dir / "index.fakepar"
        idx.write_text(json.dumps({"percent": percent, "files": index}))
        return [idx, *out]

    def repair(self, files: Sequence[Path], parity_dir: Path) -> bool:
        self.repair_calls += 1
        index: dict[str, str] = json.loads((parity_dir / "index.fakepar").read_text())["files"]
        for f in files:
            good = (parity_dir / index[f.name]).read_bytes()
            if not f.exists() or f.read_bytes() != good:
                if f.exists():
                    os.rename(f, f.with_name(f.name + ".1"))
                f.write_bytes(good)
        return True


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


def corrupt(path: Path, offset: int = 10) -> None:
    """Flip one bit of a (possibly read-only) file in place."""
    os.chmod(path, 0o644)
    raw = bytearray(path.read_bytes())
    raw[offset] ^= 0x01
    path.write_bytes(bytes(raw))


SealFn = Callable[..., Path]
"""Signature of the ``seal`` fixture: seals a generation built from in-memory files."""
