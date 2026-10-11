"""``Par2Parity``: :class:`wsp_core.ports.ParityProvider` backed by the ``par2`` CLI (PLAN §8.1, §8.4).

Recovery data is created for a group of files at once, with their common directory as the par2 base
path so file names are stored relative to it. ``par2 repair`` writes repaired files in place and
renames damaged originals to ``<name>.1``; callers move those aside (the vault quarantines them
rather than deleting them).

**Fresh output, published once** (vault-fetch audit #14). ``par2 create`` refuses to overwrite its
own files (exit 6), so a run killed half-way used to wedge every later attempt. Each run now writes
into a new private directory next to ``out_dir`` and that directory is renamed to ``out_dir`` only
when par2 succeeded; ``out_dir`` must be absent or empty.

**Bound to verified bytes** (audit #7). par2 reads the data files itself, after whoever verified
them. :meth:`Par2Parity.describe` parses the FileDescription packets par2 wrote (each packet's own
MD5 is checked) and :func:`check_parity_binding` requires that par2's whole-file MD5 and length of
every file equal the MD5 and size computed in the caller's verification pass. If the bytes changed
between verification and par2's read, the parity is rejected instead of encoding them.

**Limits.** par2 needs at least one source block per file and allows 32,768, so more than
:data:`MAX_FILES` files are refused (callers split them into sets). The command line carries one
path per file; a command longer than the system's ``ARG_MAX`` budget is refused up front rather
than failing inside ``exec``.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import signal
import struct
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Protocol, runtime_checkable

from wsp_vault.cancel import Interrupted, never

INDEX_NAME = "recovery.par2"
DEFAULT_THREADS = 2  # par2cmdline >= 0.8 "-t": keep the machine responsive (owner's browser first)
MAX_FILES = 32_000  # par2 allows 32768 source blocks and needs one per file
_MAGIC = b"PAR2\0PKT"
_FILE_DESC = b"PAR 2.0\0FileDesc"

Runner = Callable[[Sequence[str]], int]


def stoppable_runner(should_stop: Callable[[], bool] = never, *, poll: float = 0.2) -> Runner:
    """Run a command in its own process group; kill the group and raise :class:`Interrupted` on stop."""

    def run(args: Sequence[str]) -> int:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
        try:
            while True:
                try:
                    return proc.wait(timeout=poll)
                except subprocess.TimeoutExpired:
                    if should_stop():
                        raise Interrupted(f"stop requested while running {args[0]}") from None
        finally:
            if proc.poll() is None:
                _kill_group(proc)

    return run


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    for sig, grace in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        deadline = time.monotonic() + grace
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.poll() is not None:
            return


_subprocess_runner = stoppable_runner()


class ParityError(Exception):
    pass


def low_priority_prefix() -> tuple[str, ...]:
    """``nice``/``ionice`` wrappers (when installed) so parity work yields to everything else.

    ``ionice`` only affects block devices: on the CIFS share it changes nothing, and the byte-rate
    limiter of the caller is what keeps par2 from saturating the NAS link.
    """
    prefix: list[str] = []
    nice, ionice = shutil.which("nice"), shutil.which("ionice")
    if nice:
        prefix += [nice, "-n", "19"]
    if ionice:
        prefix += [ionice, "-c", "3"]
    return tuple(prefix)


def arg_budget() -> int:
    """Bytes of command line we allow ourselves: half of ``ARG_MAX`` (the environment uses the rest)."""
    try:
        limit = int(os.sysconf("SC_ARG_MAX"))
    except (ValueError, OSError):
        limit = 128 * 1024
    return max(32 * 1024, limit // 2)


def file_descriptions(par2_file: Path) -> dict[str, tuple[str, int]]:
    """``{relative name: (md5 hex, length)}`` from the FileDescription packets of one ``.par2`` file.

    Every packet's MD5 is checked; a damaged packet raises :class:`ParityError`.
    """
    data = par2_file.read_bytes()
    out: dict[str, tuple[str, int]] = {}
    i = 0
    while i + 64 <= len(data):
        if data[i : i + 8] != _MAGIC:
            raise ParityError(f"{par2_file}: no par2 packet at offset {i}")
        (length,) = struct.unpack_from("<Q", data, i + 8)
        if length < 64 or length % 4 or i + length > len(data):
            raise ParityError(f"{par2_file}: bad packet length {length} at offset {i}")
        if hashlib.md5(data[i + 32 : i + length], usedforsecurity=False).digest() != data[i + 16 : i + 32]:
            raise ParityError(f"{par2_file}: packet at offset {i} fails its MD5")
        if data[i + 48 : i + 64] == _FILE_DESC:
            body = data[i + 64 : i + length]
            md5 = body[16:32].hex()
            (size,) = struct.unpack_from("<Q", body, 48)
            name = body[56:].rstrip(b"\0").decode("utf-8", errors="surrogateescape")
            out[name] = (md5, size)
        i += length
    if i != len(data):
        raise ParityError(f"{par2_file}: trailing bytes after the last packet")
    return out


def check_parity_binding(
    described: Mapping[Path, tuple[str, int]], verified: Mapping[Path, tuple[str, int]]
) -> None:
    """Raise :class:`ParityError` unless par2 describes exactly the verified files with the verified
    MD5 and length (``{path: (md5 hex, size)}`` on both sides)."""
    missing = sorted(set(verified) - set(described))
    extra = sorted(set(described) - set(verified))
    if missing or extra:
        raise ParityError(
            f"par2 recovery set does not match the verified files: missing={missing[:3]} extra={extra[:3]}"
        )
    wrong = sorted(p for p, v in verified.items() if described[p] != v)
    if wrong:
        raise ParityError(
            f"par2 encoded bytes that differ from the verified ones (changed between verification and "
            f"par2's read?): {wrong[:3]}"
        )


@runtime_checkable
class DescribesParity(Protocol):
    """A parity provider that can say which file contents a recovery set encodes (for binding)."""

    def describe(self, created: Sequence[Path], files: Sequence[Path]) -> dict[Path, tuple[str, int]]: ...


class Par2Parity:
    """``low_priority`` runs par2 under ``nice -n 19 ionice -c 3``; ``threads`` caps par2's threads
    (``-t``, default 2; ``None`` leaves par2's default of one per core) and ``memory_mb`` its memory
    (``-m``). ``should_stop`` kills a running par2 (and raises :class:`Interrupted`) on shutdown."""

    def __init__(
        self,
        *,
        par2: str | None = None,
        runner: Runner | None = None,
        low_priority: bool = False,
        threads: int | None = DEFAULT_THREADS,
        memory_mb: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        exe = par2 or shutil.which("par2")
        if exe is None and runner is None:
            raise ParityError("par2 not found; install par2cmdline")
        self._exe = exe or "par2"
        self._run = runner or (stoppable_runner(should_stop) if should_stop else _subprocess_runner)
        self._prefix = low_priority_prefix() if low_priority else ()
        self._threads = threads
        self._memory = memory_mb

    def _cmd(self, verb: str) -> list[str]:
        cmd = [*self._prefix, self._exe, verb, "-q"]
        if self._threads:
            cmd.append(f"-t{self._threads}")
        if self._memory:
            cmd.append(f"-m{self._memory}")
        return cmd

    @staticmethod
    def _base(files: Sequence[Path]) -> Path:
        if not files:
            raise ParityError("no files to protect")
        return Path(os.path.commonpath([f.parent for f in files]))

    @staticmethod
    def _check_argv(argv: Sequence[str]) -> None:
        size = sum(len(os.fsencode(a)) + 1 + 8 for a in argv)  # bytes + NUL + pointer
        if size > arg_budget():
            raise ParityError(f"par2 command line would be {size} bytes (> {arg_budget()}); split the files")

    def create(self, files: Sequence[Path], out_dir: Path, percent: int) -> Sequence[Path]:
        if not 1 <= percent <= 100:
            raise ParityError(f"redundancy must be 1..100 percent, got {percent}")
        if len(files) > MAX_FILES:
            raise ParityError(f"{len(files)} files exceed par2's limit of {MAX_FILES}; split them into sets")
        if out_dir.exists() and any(out_dir.iterdir()):
            raise ParityError(f"{out_dir} is not empty; refusing to mix recovery sets")
        work = out_dir.with_name(f".{out_dir.name}.{secrets.token_hex(6)}.par2-tmp")
        work.mkdir(parents=True)
        try:
            # par2 needs at least one source block per file; keep the default 2000 unless there are more.
            blocks = [f"-b{min(MAX_FILES, 2 * len(files))}"] if len(files) > 1000 else []
            index = str(work / INDEX_NAME)
            argv = [*self._cmd("create"), f"-r{percent}", *blocks, f"-B{self._base(files)}", index]
            argv += map(str, files)
            self._check_argv(argv)
            if self._run(argv) != 0:
                raise ParityError(f"par2 create failed for {out_dir}")
            if out_dir.exists():
                out_dir.rmdir()  # empty (checked above): the finished set replaces it in one rename
            os.rename(work, out_dir)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        return sorted(p for p in out_dir.iterdir() if p.suffix == ".par2")

    def describe(self, created: Sequence[Path], files: Sequence[Path]) -> dict[Path, tuple[str, int]]:
        """What the recovery set made from ``files`` encodes: ``{file path: (md5 hex, length)}``.

        par2 stores names relative to its ``-B`` base (the common directory of ``files``); they are
        re-anchored on that base, so the keys compare equal to the paths given to :meth:`create`.
        """
        index = next((p for p in created if p.name == INDEX_NAME), None)
        if index is None:
            raise ParityError(f"no {INDEX_NAME} among the created files")
        base = self._base(files)
        return {base / name: v for name, v in file_descriptions(index).items()}

    def repair(self, files: Sequence[Path], parity_dir: Path) -> bool:
        index = parity_dir / INDEX_NAME
        if not index.exists():
            return False
        argv = [*self._cmd("repair"), f"-B{self._base(files)}", str(index), *map(str, files)]
        self._check_argv(argv)
        return self._run(argv) == 0
