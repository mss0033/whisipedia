"""State store schema migrations."""

import sqlite3
from pathlib import Path

from wsp_orchestrator.store import StateStore

TS = "2026-10-01T00:00:00.000000Z"


def test_schema_v1_is_migrated_to_current(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '1');"
        "CREATE TABLE releases (source_id TEXT NOT NULL, version TEXT NOT NULL, state TEXT NOT NULL,"
        " attempts INTEGER NOT NULL DEFAULT 0, generation_id TEXT NOT NULL, ref_json TEXT NOT NULL,"
        " plan_json TEXT, local_files_json TEXT NOT NULL DEFAULT '[]', doc_count INTEGER, size_bytes INTEGER,"
        " hold_reason TEXT, approved INTEGER NOT NULL DEFAULT 0, last_error TEXT, next_attempt_at TEXT,"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (source_id, version));"
        "INSERT INTO releases(source_id, version, state, generation_id, ref_json, created_at, updated_at)"
        f" VALUES ('s', 'v', 'planned', 'v', '{{}}', '{TS}', '{TS}');"
    )
    db.commit()
    db.close()
    store = StateStore(path, check_local=False)
    rec = store.get_release("s", "v")
    assert rec is not None
    assert rec.fetched == {}
    assert store.get_meta("schema_version") == "4"
    assert store.offers("s") is None
    store.close()


def test_offers_and_proposal_status(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    store = StateStore(tmp_path / "state.sqlite3", check_local=False)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    store.put_offers("s", [{"generation_id": "2026-09", "version": "2026-09", "published": None}], now)
    offers = store.offers("s")
    assert offers is not None
    assert offers[0][0]["generation_id"] == "2026-09"
    assert offers[1] == now
    pid = store.add_proposal("k", "review", {"title": "t"}, now)
    assert store.proposal_keys() == {"k"}
    assert store.set_proposal_status(pid, "rejected")
    assert store.proposals() == []
    assert len(store.proposals(status=None)) == 1
    store.close()
