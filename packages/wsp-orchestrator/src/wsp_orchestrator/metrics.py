"""Minimal Prometheus text-format metrics, written atomically to a file (node_exporter textfile style).

No HTTP listener: the orchestrator exposes nothing on the network beyond what acquisition needs.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from pathlib import Path

Labels = tuple[tuple[str, str], ...]


def _labels(labels: Mapping[str, str] | None) -> Labels:
    return tuple(sorted((labels or {}).items()))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


class Metrics:
    def __init__(self, prefix: str = "wsp_orchestrator") -> None:
        self.prefix = prefix
        self._lock = threading.Lock()
        self._types: dict[str, tuple[str, str]] = {}
        self._values: dict[str, dict[Labels, float]] = {}

    def _declare(self, name: str, kind: str, help_text: str) -> str:
        full = f"{self.prefix}_{name}"
        known = self._types.get(full)
        if known is not None and known[0] != kind:
            raise ValueError(f"metric {full} already declared as {known[0]}")
        self._types.setdefault(full, (kind, help_text))
        self._values.setdefault(full, {})
        return full

    def inc(
        self, name: str, help_text: str, labels: Mapping[str, str] | None = None, by: float = 1.0
    ) -> None:
        with self._lock:
            full = self._declare(name, "counter", help_text)
            key = _labels(labels)
            self._values[full][key] = self._values[full].get(key, 0.0) + by

    def set(self, name: str, help_text: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        with self._lock:
            full = self._declare(name, "gauge", help_text)
            self._values[full][_labels(labels)] = value

    def reset_gauge(self, name: str) -> None:
        """Drop all label sets of a gauge (before re-populating it from current state)."""
        with self._lock:
            self._values.get(f"{self.prefix}_{name}", {}).clear()

    def get(self, name: str, labels: Mapping[str, str] | None = None) -> float | None:
        with self._lock:
            return self._values.get(f"{self.prefix}_{name}", {}).get(_labels(labels))

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for full in sorted(self._types):
                kind, help_text = self._types[full]
                lines.append(f"# HELP {full} {help_text}")
                lines.append(f"# TYPE {full} {kind}")
                for labels, value in sorted(self._values[full].items()):
                    label_txt = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
                    series = f"{full}{{{label_txt}}}" if label_txt else full
                    lines.append(f"{series} {_fmt(value)}")
        return "\n".join(lines) + "\n"

    def write(self, path: Path) -> None:
        """Atomic write: temp file, fsync, rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(self.render())
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
