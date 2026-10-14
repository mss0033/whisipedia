"""Memory as the kernel accounts it to a job, and page-cache hygiene for big sequential files.

A systemd unit's ``MemoryHigh`` counts the file pages a job reads or writes, not only its heap.
The 2026-10-14 nightly build spent 3 CPU minutes in 70 and was still charged 8.2 GB against a
6 GB ceiling: every vault file read and every Refinery part written on the NAS stayed in the
page cache of its cgroup, and the kernel swapped the job's heap to make room. :func:`drop_file_cache`
hands a file's clean pages back once a job is done with them; :func:`memory_line` says what the
cgroup holds, heap and cache apart, so a run's log diagnoses itself.
"""

from __future__ import annotations

import os
import resource
from contextlib import suppress
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_CGROUP = Path("/proc/self/cgroup")
_STAT_KEYS = ("anon", "file", "file_dirty")


def drop_file_cache(path: Path | str) -> None:
    """Best effort: release the page cache of ``path`` (clean pages only; dirty ones stay).

    Harmless for a file another process still maps or reads: it only re-reads from storage.
    """
    if not hasattr(os, "posix_fadvise"):
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        with suppress(OSError):
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


def own_cgroup(cgroup_root: Path = CGROUP_ROOT, proc: Path = PROC_CGROUP) -> Path | None:
    """This process's cgroup v2 directory, or ``None`` when there is none to read."""
    try:
        own = proc.read_text(encoding="utf-8").strip().split("::", 1)[-1].lstrip("/")
    except OSError:
        return None
    return cgroup_root / own


def cgroup_memory(cgroup: Path | None = None) -> dict[str, int]:
    """``anon``, ``file``, ``file_dirty``, ``current`` and ``swap`` of the cgroup, in bytes.

    Empty when the cgroup cannot be read (no cgroup v2, a container without the files).
    """
    here = own_cgroup() if cgroup is None else cgroup
    if here is None:
        return {}
    out: dict[str, int] = {}
    try:
        for line in (here / "memory.stat").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(" ")
            if key in _STAT_KEYS and value.isdigit():
                out[key] = int(value)
    except OSError:
        return {}
    for name, key in (("memory.current", "current"), ("memory.swap.current", "swap")):
        with suppress(OSError, ValueError):
            out[key] = int((here / name).read_text(encoding="utf-8").strip())
    return out


def peak_rss_mib() -> float:
    """This process's peak resident set size so far (MiB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def memory_line(cgroup: Path | None = None) -> str:
    """One log line: this process's peak RSS, then the cgroup's heap, cache, dirty cache and swap.

    ``mem: peak rss 2,809 MiB; cgroup anon 1,581 file 5,440 file_dirty 2 current 7,167 swap 575 MiB``
    """
    parts = [f"peak rss {peak_rss_mib():,.0f} MiB"]
    mem = cgroup_memory(cgroup)
    if mem:
        parts.append("cgroup " + " ".join(f"{k} {v / 2**20:,.0f}" for k, v in mem.items()) + " MiB")
    return "mem: " + "; ".join(parts)
