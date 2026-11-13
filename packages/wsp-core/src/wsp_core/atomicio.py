"""Whole-or-nothing writes of small text files (state, guards, ledgers).

A bare ``path.write_text`` that is cut short (a kill, a full disk) leaves a truncated file under the real
name, and a reader that treats "unreadable" as "empty" then loses what the file guarded: PubMed's
baseline-size guard read a torn file as "no baseline known" and compared nothing (trawl 2026-11-10).
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from pathlib import Path

PARTIAL_SUFFIX = ".partial"
# Between the file's name and the random part of a temporary name, so the clean-up below can tell this
# module's leftovers from every other ``<name>.<something>.partial`` (a download of ``<name>.zip``).
MARK = ".wspatomic-"
STALE_PARTIAL_S = 24 * 3600.0  # a writer killed mid-write leaves its temporary file; older ones are removed


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` so that a reader sees the old file or the new one, never a part.

    Temporary file beside ``path`` -> flush -> fsync -> ``os.replace`` -> fsync of the directory (best
    effort: the rename is already in place, and some file systems refuse a directory fsync). The
    temporary file is removed if anything fails, and the old file is then untouched.

    The temporary name is unique per call (``<name>.wspatomic-<random>.partial``), not per process:
    two threads of one daemon writing the same path each rename their own whole file, the later rename wins.
    Writers are not serialized beyond that: a caller that reads, changes and writes can still lose
    another writer's update, and must be able to live with that or hold its own lock.

    The file gets the mode a plain ``write_text`` would give it (the old file's mode, else 0666 less
    the umask), not ``mkstemp``'s 0600, so other readers of the file are not locked out.
    """
    _drop_stale_partials(path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + MARK, suffix=PARTIAL_SUFFIX)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            with contextlib.suppress(OSError):  # some CIFS mounts refuse it; not worth the write
                os.fchmod(fh.fileno(), _mode_for(path))
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    with contextlib.suppress(OSError):
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _mode_for(path: Path) -> int:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return 0o666 & ~_umask()


def _umask() -> int:
    """The process umask. Read from /proc where it can be: ``os.umask`` can only read by setting, and
    for that instant another thread would create files with the wrong mode."""
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("Umask:"):
                return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    umask = os.umask(0o022)
    os.umask(umask)
    return umask


def _drop_stale_partials(path: Path) -> None:
    """Remove this path's ``<name>.wspatomic-<random>.partial`` files older than a day: a killed writer's
    leftovers, which no later call would ever name again. Younger ones may belong to a writer that is
    running. Only this module's own names are matched: ``<name>.partial`` and ``<name>.zip.partial``
    (a download in progress, another writer's temporary name) are not."""
    cutoff = time.time() - STALE_PARTIAL_S
    prefix = path.name + MARK
    with contextlib.suppress(OSError):
        for entry in os.scandir(path.parent):
            name = entry.name
            if not (name.startswith(prefix) and name.endswith(PARTIAL_SUFFIX)):
                continue
            middle = name[len(prefix) : -len(PARTIAL_SUFFIX)]
            if not middle or "." in middle:
                continue
            with contextlib.suppress(OSError):
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(entry.path)
