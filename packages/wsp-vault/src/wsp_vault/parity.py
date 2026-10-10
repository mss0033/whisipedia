"""``Par2Parity``: :class:`wsp_core.ports.ParityProvider` backed by the ``par2`` CLI (PLAN §8.1, §8.4).

Recovery data is created for all files of a generation at once, with the generation directory as
the par2 base path so file names are stored relative to it. ``par2 repair`` writes repaired files in
place and renames damaged originals to ``<name>.1``; callers move those aside (the vault quarantines
them rather than deleting them).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

INDEX_NAME = "recovery.par2"

Runner = Callable[[Sequence[str]], int]


def _subprocess_runner(args: Sequence[str]) -> int:
    return subprocess.run(list(args), capture_output=True, check=False).returncode  # noqa: S603


class ParityError(Exception):
    pass


def low_priority_prefix() -> tuple[str, ...]:
    """``nice``/``ionice`` wrappers (when installed) so parity work yields to everything else."""
    prefix: list[str] = []
    nice, ionice = shutil.which("nice"), shutil.which("ionice")
    if nice:
        prefix += [nice, "-n", "19"]
    if ionice:
        prefix += [ionice, "-c", "3"]
    return tuple(prefix)


class Par2Parity:
    """``low_priority`` runs par2 under ``nice -n 19 ionice -c 3``; ``threads`` caps par2's threads."""

    def __init__(
        self,
        *,
        par2: str | None = None,
        runner: Runner | None = None,
        low_priority: bool = False,
        threads: int | None = None,
    ) -> None:
        exe = par2 or shutil.which("par2")
        if exe is None and runner is None:
            raise ParityError("par2 not found; install par2cmdline")
        self._exe = exe or "par2"
        self._run = runner or _subprocess_runner
        self._prefix = low_priority_prefix() if low_priority else ()
        self._threads = threads

    def _cmd(self, verb: str) -> list[str]:
        cmd = [*self._prefix, self._exe, verb, "-q"]
        if self._threads:
            cmd.append(f"-t{self._threads}")
        return cmd

    @staticmethod
    def _base(files: Sequence[Path]) -> Path:
        if not files:
            raise ParityError("no files to protect")
        return Path(os.path.commonpath([f.parent for f in files]))

    def create(self, files: Sequence[Path], out_dir: Path, percent: int) -> Sequence[Path]:
        if not 1 <= percent <= 100:
            raise ParityError(f"redundancy must be 1..100 percent, got {percent}")
        out_dir.mkdir(parents=True, exist_ok=True)
        index = out_dir / INDEX_NAME
        before = set(out_dir.iterdir())
        # par2 needs at least one source block per file; keep the default 2000 unless there are more.
        blocks = [f"-b{min(32_000, 2 * len(files))}"] if len(files) > 1000 else []
        args = [*self._cmd("create"), f"-r{percent}", *blocks, f"-B{self._base(files)}", str(index)]
        if self._run([*args, *map(str, files)]) != 0:
            raise ParityError(f"par2 create failed for {out_dir}")
        return sorted(p for p in out_dir.iterdir() if p not in before and p.suffix == ".par2")

    def repair(self, files: Sequence[Path], parity_dir: Path) -> bool:
        index = parity_dir / INDEX_NAME
        if not index.exists():
            return False
        args = [*self._cmd("repair"), f"-B{self._base(files)}", str(index)]
        return self._run([*args, *map(str, files)]) == 0
