"""Registry changes from proposals (PLAN §5.1, §5.4).

* **Auto-promotions** go to a *local overlay*, ``config/registry.local.yaml`` (git-ignored): same
  format as ``registry/sources.yaml``; an entry there replaces the committed entry with the same id
  or adds a new source. The committed registry is never edited by the daemon.
* **Accepted proposals** (``wsp proposals accept``) are written into ``registry/sources.yaml`` in the
  working tree as a text edit: the entry's block is replaced (or appended), every other line,
  comment and formatting choice is kept, so the owner reviews a small diff and commits it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

import yaml

OVERLAY_HEADER = """\
# Local registry overlay (git-ignored): auto-promotions made by the Curator's policy gate (PLAN §5.4).
# Entries here replace the committed registry/sources.yaml entry with the same id, or add a source.
# Review with `wsp proposals list`; make one permanent with `wsp proposals accept <id>` and commit.
"""


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in cast(Mapping[object, object], value).items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in cast(Sequence[object], value)]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def dump_entry(entry: Mapping[str, object]) -> str:
    """One registry entry as a ``sources:`` list item, indented like ``registry/sources.yaml``."""
    text = yaml.safe_dump(
        [_plain(entry)], sort_keys=False, allow_unicode=True, width=110, default_flow_style=None
    )
    return "".join(("  " + line) if line.strip() else line for line in text.splitlines(keepends=True))


_ENTRY = re.compile(r"^  - id:\s*(\S+)\s*$")


def apply_to_registry_text(text: str, entry: Mapping[str, object]) -> str:
    """Replace the block of ``entry["id"]`` in a registry file's text, or append it."""
    source_id = str(entry["id"])
    block = dump_entry(entry)
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if (m := _ENTRY.match(ln)) and m[1] == source_id), None)
    if start is None:
        body = text if text.endswith("\n") else text + "\n"
        return body + "\n" + block
    end = start + 1
    # The entry's own lines are indented four spaces or more; the next entry, a section comment
    # ("  # --- Section ---") or a top-level key is not (a comment of the next section was deleted
    # with the entry: 2026-10-26 review #58).
    while end < len(lines) and not re.match(r"^ {0,3}\S", lines[end]):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():  # keep the blank separator lines
        end -= 1
    return "".join(lines[:start]) + block + "".join(lines[end:])


def load_overlay(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return []
    sources: object = cast(dict[str, object], data).get("sources")
    if not isinstance(sources, list):
        return []
    return [cast(dict[str, Any], s) for s in cast(list[object], sources) if isinstance(s, dict)]


def write_overlay(path: Path, entries: Sequence[Mapping[str, object]]) -> None:
    body = OVERLAY_HEADER + "schema_version: 1\nsources:\n"
    body += "".join(dump_entry(e) for e in sorted(entries, key=lambda e: str(e["id"])))
    if not entries:
        body = OVERLAY_HEADER + "schema_version: 1\nsources: []\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)


def upsert_overlay(path: Path, entry: Mapping[str, object]) -> None:
    entries = [e for e in load_overlay(path) if e.get("id") != entry["id"]]
    write_overlay(path, [*entries, dict(entry)])


def remove_from_overlay(path: Path, source_id: str) -> bool:
    entries = load_overlay(path)
    kept = [e for e in entries if e.get("id") != source_id]
    if len(kept) == len(entries):
        return False
    write_overlay(path, kept)
    return True
