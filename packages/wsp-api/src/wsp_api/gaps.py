"""Reference :class:`~wsp_api.seams.GapLog`: one JSON object per line, appended to a local file.

The gap miner (PLAN §5.3) reads ``<index root>/gaps.jsonl`` once per cycle and aggregates by ``href``.
The file lives on local disk next to the index, never on the vault (ADR-0002: no live writes there).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from wsp_core.model import Gap

MAX_BYTES = 8 * 1024 * 1024
KEEP = 5


def rotate(path: Path, *, max_bytes: int = MAX_BYTES, keep: int = KEEP) -> None:
    """``path`` -> ``path.1`` -> ... -> ``path.<keep>`` (the oldest is dropped) once it is too big."""
    try:
        if path.stat().st_size < max_bytes:
            return
    except FileNotFoundError:
        return
    for i in range(keep - 1, 0, -1):
        older = path.with_name(f"{path.name}.{i}")
        if older.exists():
            os.replace(older, path.with_name(f"{path.name}.{i + 1}"))
    os.replace(path, path.with_name(f"{path.name}.1"))


class JsonlQueryLog:
    """Local search log ``{"at", "q", "total", "low"}`` per line, rotated; never leaves this computer."""

    def __init__(self, path: Path, *, max_bytes: int = MAX_BYTES, keep: int = KEEP) -> None:
        self._path = path
        self._max = max_bytes
        self._keep = keep
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def record(self, at: datetime, query: str, total: int, low_confidence: bool) -> None:
        line = json.dumps(
            {"at": at.isoformat(), "q": query[:500], "total": total, "low": low_confidence},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            rotate(self._path, max_bytes=self._max, keep=self._keep)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


class JsonlGapLog:
    def __init__(self, path: Path, *, max_bytes: int = MAX_BYTES, keep: int = KEEP) -> None:
        self._path = path
        self._max = max_bytes
        self._keep = keep
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def record(self, gap: Gap) -> None:
        line = json.dumps(
            {"at": gap.at.isoformat(), "href": gap.href, "from_doc_id": gap.from_doc_id},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            rotate(self._path, max_bytes=self._max, keep=self._keep)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
