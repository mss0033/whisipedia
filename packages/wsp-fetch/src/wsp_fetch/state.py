"""Resume sidecar ``<dest>.partial.state.json``.

Written atomically (temp + fsync + rename) and only *after* ``fdatasync`` of the ``.partial`` file,
so every piece it lists was durable when it was recorded. Each piece also carries its own digest, so a
resumed piece is re-verified when it is read back for the full-file hash; a torn or rotted piece is
simply downloaded again.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

STATE_VERSION = 1


def partial_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".partial")


def state_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".partial.state.json")


@dataclass(slots=True)
class PartialState:
    url: str
    size: int
    piece_size: int
    piece_algorithm: str
    checksums: dict[str, str] = field(default_factory=dict[str, str])
    validator: str | None = None  # ETag or Last-Modified of the primary URL
    done: dict[int, str] = field(default_factory=dict[int, str])  # piece index -> piece digest

    def same_target(self, other: PartialState) -> bool:
        return (
            self.url == other.url
            and self.size == other.size
            and self.piece_size == other.piece_size
            and self.piece_algorithm == other.piece_algorithm
            and self.checksums == other.checksums
            and (self.validator is None or other.validator is None or self.validator == other.validator)
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": STATE_VERSION,
                "url": self.url,
                "size": self.size,
                "piece_size": self.piece_size,
                "piece_algorithm": self.piece_algorithm,
                "checksums": self.checksums,
                "validator": self.validator,
                "done": {str(k): v for k, v in sorted(self.done.items())},
            },
            indent=1,
        )

    @classmethod
    def from_json(cls, text: str) -> PartialState | None:
        try:
            raw = cast(dict[str, Any], json.loads(text))
            if raw.get("version") != STATE_VERSION:
                return None
            done = cast(dict[str, str], raw["done"])
            return cls(
                url=str(raw["url"]),
                size=int(raw["size"]),
                piece_size=int(raw["piece_size"]),
                piece_algorithm=str(raw["piece_algorithm"]),
                checksums={str(k): str(v) for k, v in cast(dict[str, str], raw["checksums"]).items()},
                validator=cast(str | None, raw.get("validator")),
                done={int(k): str(v) for k, v in done.items()},
            )
        except (ValueError, KeyError, TypeError):
            return None


def load_state(path: Path) -> PartialState | None:
    try:
        return PartialState.from_json(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def save_state(path: Path, state: PartialState) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(state.to_json())
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)
