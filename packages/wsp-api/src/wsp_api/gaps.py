"""Reference :class:`~wsp_api.seams.GapLog`: one JSON object per line, appended to a local file.

The gap miner (PLAN §5.3) reads ``<index root>/gaps.jsonl`` once per cycle and aggregates by ``href``.
The file lives on local disk next to the index, never on the vault (ADR-0002: no live writes there).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from wsp_core.model import Gap


class JsonlGapLog:
    def __init__(self, path: Path) -> None:
        self._path = path
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
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
