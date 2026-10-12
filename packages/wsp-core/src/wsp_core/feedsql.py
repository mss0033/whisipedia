"""SQL shared by the change feed's writer (``wsp_index``) and the API's read-only reader (``wsp_api``)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import cast

# The source id of a change (``wsp:<source>/<native id>``), as an indexed expression: a site's recent
# changes are one index range read instead of a scan of the newest rows (40 concurrent site pages,
# each scanning 200,000 rows under the GIL, starved the API's thread pool on 2026-10-11).
SOURCE_EXPR = "substr(doc_id, 5, instr(substr(doc_id, 5), '/') - 1)"
SOURCE_INDEX = f"CREATE INDEX IF NOT EXISTS changes_by_source ON changes({SOURCE_EXPR}, seq)"
COLUMNS = "seq, doc_id, kind, generation_id, old_hash, new_hash, renamed_from"
UNINDEXED_SCAN = 20_000  # rows examined per filtered tail on a feed without the source index


def has_source_index(db: sqlite3.Connection) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'changes_by_source'"
    ).fetchone()
    return row is not None


def tail_rows(
    db: sqlite3.Connection, limit: int, sources: Sequence[str] | None, *, indexed: bool
) -> list[tuple[object, ...]]:
    """The newest ``limit`` rows (of ``sources``, if given), newest first.

    With the source index each source is one reverse index range; the per-source lists are merged by
    ``seq``. Without it at most :data:`UNINDEXED_SCAN` newest rows are examined (a rare source may then
    show fewer changes than it has, never a slow page).
    """
    if not sources:
        return list(db.execute(f"SELECT {COLUMNS} FROM changes ORDER BY seq DESC LIMIT ?", (limit,)))  # noqa: S608 - constant
    if indexed:
        rows: list[tuple[object, ...]] = []
        for source in dict.fromkeys(sources):
            rows += db.execute(
                f"SELECT {COLUMNS} FROM changes WHERE {SOURCE_EXPR} = ? ORDER BY seq DESC LIMIT ?",  # noqa: S608 - constant
                (source, limit),
            ).fetchall()
        rows.sort(key=lambda r: -int(cast(int, r[0])))
        return rows[:limit]
    prefixes = tuple(f"wsp:{s}/" for s in sources)
    out: list[tuple[object, ...]] = []
    for r in db.execute(f"SELECT {COLUMNS} FROM changes ORDER BY seq DESC LIMIT ?", (UNINDEXED_SCAN,)):  # noqa: S608 - constant
        if str(r[1]).startswith(prefixes):
            out.append(tuple(r))
            if len(out) >= limit:
                break
    return out
