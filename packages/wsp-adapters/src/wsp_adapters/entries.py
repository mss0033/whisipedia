"""Entries richer than ``DocRecord``: the redirects that snapshot diffing uses to detect renames.

``SourceAdapter.documents`` yields articles only. Adapters in this package also implement
:class:`EntrySource`, which yields articles *and* redirects in one decoding pass, so a diff does not
need to read a multi-gigabyte dump twice.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from wsp_core.model import DocRecord
from wsp_core.ports import Ctx


@dataclass(frozen=True, slots=True)
class Redirect:
    """``native_id`` is an alias of ``target`` (both native ids of the same source)."""

    native_id: str
    target: str


type Entry = DocRecord | Redirect


@runtime_checkable
class EntrySource(Protocol):
    def entries(self, ctx: Ctx, generation_dir: Path) -> Iterator[Entry]:
        """Articles and redirects of a sealed generation, in upstream order."""
        ...
