"""Where the API's memory is: ``GET /v1/system/memory`` (and ``POST /v1/system/memory/trim``).

2026-10-23: the API held 4.3 GB of anonymous memory after 26 hours, with every cache it keeps
bounded, and nothing said which part was growing. The report splits it: the process (resident,
anonymous, swapped), glibc's heap (in use, and free but kept: fragmentation), Arrow's memory pool,
the document readers it holds open and what they are estimated to cost, the garbage collector, and,
when tracing was turned on at start (``WSP_DIAG_TRACEMALLOC=<frames>``), the lines that allocated
the most Python memory. ``trim`` returns glibc's and Arrow's free memory to the system and says how
much the process shrank: a large drop is fragmentation, not a leak.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import os
import time
import tracemalloc
from collections import Counter
from collections.abc import Mapping
from typing import Any, cast

TRACE_ENV = "WSP_DIAG_TRACEMALLOC"


def start_tracing_from_env(environ: Mapping[str, str] | None = None) -> bool:
    """Start ``tracemalloc`` when ``WSP_DIAG_TRACEMALLOC`` names a frame count (a few: it costs memory
    and time on every allocation). Only allocations after the start are seen."""
    value = (os.environ if environ is None else environ).get(TRACE_ENV, "").strip()
    if not value.isdigit() or int(value) <= 0 or tracemalloc.is_tracing():
        return False
    tracemalloc.start(min(int(value), 25))
    return True


class _MallInfo2(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_size_t)
        for name in (
            "arena",
            "ordblks",
            "smblks",
            "hblks",
            "hblkhd",
            "usmblks",
            "fsmblks",
            "uordblks",
            "fordblks",
            "keepcost",
        )
    ]


def _libc() -> Any:
    name = ctypes.util.find_library("c")
    return None if name is None else ctypes.CDLL(name)


def _status() -> dict[str, int]:
    """``/proc/self/status`` sizes in MB."""
    out: dict[str, int] = {}
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("VmRSS", "RssAnon", "RssFile", "RssShmem", "VmSwap", "VmHWM"):
                    out[key] = int(rest.split()[0]) // 1024
    except OSError:
        pass
    return out


def _glibc() -> dict[str, int] | None:
    libc = _libc()
    fn = getattr(libc, "mallinfo2", None) if libc is not None else None
    if fn is None:
        return None
    fn.restype = _MallInfo2
    info = cast(_MallInfo2, fn())
    mb = 1024 * 1024
    return {
        "heap_mb": int(info.arena) // mb,  # sbrk'd main heap and arenas
        "mmapped_mb": int(info.hblkhd) // mb,  # large blocks mapped on their own
        "in_use_mb": int(info.uordblks) // mb,
        "free_kept_mb": int(info.fordblks) // mb,  # freed but not returned: fragmentation
    }


def _arrow() -> dict[str, Any] | None:
    try:
        import pyarrow as pa
    except ImportError:
        return None
    pool = pa.default_memory_pool()
    return {
        "backend": pool.backend_name,
        "allocated_mb": pa.total_allocated_bytes() // (1024 * 1024),
        "peak_mb": None if (peak := pool.max_memory()) is None else peak // (1024 * 1024),
    }


def _readers(services: Any) -> dict[str, Any] | None:
    """The document store's open readers and their estimated cost (wsp_index.docstore)."""
    store = getattr(services, "documents", None)
    readers = getattr(store, "_readers", None)
    if not isinstance(readers, Mapping):
        return None
    from wsp_index.docstore import _reader_cost  # pyright: ignore[reportPrivateUsage]

    rows: list[tuple[int, str, str]] = sorted(
        (
            (_reader_cost(r) // (1024 * 1024), key[0], key[1])
            for key, r in cast(Mapping[tuple[str, str, bool], Any], readers).items()
        ),
        reverse=True,
    )
    return {
        "open": len(rows),
        "estimated_mb": sum(mb for mb, _, _ in rows),
        "budget_mb": int(getattr(store, "_budget", 0)) // (1024 * 1024),
        "largest": [{"source": src, "generation": gen, "estimated_mb": mb} for mb, src, gen in rows[:10]],
    }


def _python(top: int) -> dict[str, Any]:
    objects = gc.get_objects()
    # Live objects by type: two reports apart show which kind grows, without the cost of tracing
    # (2026-10-23: ~350,000 objects more every 30 minutes while the audit ran).
    by_type = Counter(type(o).__qualname__ for o in objects)
    out: dict[str, Any] = {
        "gc_counts": gc.get_count(),
        "objects": len(objects),
        "types": dict(by_type.most_common(top)),
    }
    del objects
    if tracemalloc.is_tracing():
        current, peak = tracemalloc.get_traced_memory()
        snap = tracemalloc.take_snapshot().filter_traces([tracemalloc.Filter(False, tracemalloc.__file__)])
        out["traced_mb"] = current // (1024 * 1024)
        out["traced_peak_mb"] = peak // (1024 * 1024)
        out["top"] = [
            {"where": str(stat.traceback[0]), "mb": round(stat.size / (1024 * 1024), 1), "blocks": stat.count}
            for stat in snap.statistics("lineno")[:top]
        ]
    else:
        out["tracing"] = f"off (start the API with {TRACE_ENV}=5 to see where Python memory goes)"
    return out


def report(services: Any, *, top: int = 25) -> dict[str, Any]:
    return {
        "process_mb": _status(),
        "glibc": _glibc(),
        "arrow": _arrow(),
        "readers": _readers(services),
        "python": _python(top),
    }


def trim() -> dict[str, Any]:
    """Give back what glibc and Arrow hold free; the process's size before and after."""
    before = _status()
    started = time.monotonic()
    libc = _libc()
    trimmed = bool(libc.malloc_trim(0)) if libc is not None and hasattr(libc, "malloc_trim") else None
    try:
        import pyarrow as pa

        pa.default_memory_pool().release_unused()
    except (ImportError, AttributeError):
        pass
    after = _status()
    return {
        "before_mb": before,
        "after_mb": after,
        "released": trimmed,
        "seconds": round(time.monotonic() - started, 3),
    }
