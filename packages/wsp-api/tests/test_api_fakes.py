from pathlib import Path

import pytest

from wsp_api.testing.fakes import (
    InMemoryChangeFeed,
    InMemoryDocumentStore,
    InMemoryLedger,
    InMemoryLexicalIndex,
    InMemoryVault,
    InvalidCursorError,
    sample_services,
)
from wsp_core.ids import make_doc_id
from wsp_core.model import GenerationStatus
from wsp_core.ports import ChangeFeed, DocumentStore, Ledger, LexicalIndex, Vault


def test_fakes_satisfy_the_ports() -> None:
    assert isinstance(InMemoryLexicalIndex(), LexicalIndex)
    assert isinstance(InMemoryDocumentStore(), DocumentStore)
    assert isinstance(InMemoryChangeFeed(), ChangeFeed)
    assert isinstance(InMemoryVault(), Vault)
    assert isinstance(InMemoryLedger(), Ledger)


def test_index_upsert_is_invisible_until_commit_and_delete_works() -> None:
    services = sample_services()
    index = services.index
    doc = services.documents.get(make_doc_id("rfc", "9110"))
    assert doc is not None
    assert index.search("caching")
    index.delete([doc.doc_id])
    assert index.search("caching")
    index.commit()
    assert not index.search("caching")
    assert index.search("   ") == ()
    assert index.suggest("") == ()


def test_change_feed_rejects_foreign_cursor() -> None:
    feed = InMemoryChangeFeed()
    with pytest.raises(InvalidCursorError):
        feed.since("1")
    with pytest.raises(InvalidCursorError):
        feed.since("abc")
    assert feed.since(None) == ((), "0")


def test_vault_lifecycle(tmp_path: Path) -> None:
    vault = InMemoryVault()
    vault.stage("rfc", "2026-11-01")
    f = tmp_path / "x.txt"
    f.write_bytes(b"hello")
    art = vault.ingest("rfc", "2026-11-01", f, "upstream/x.txt")
    assert art.size == 5
    assert art.sha256.startswith("2cf24dba")
    with pytest.raises(PermissionError):
        vault.promote("rfc", "2026-11-01")
    gen = vault.seal("rfc", "2026-11-01")
    assert gen.status is GenerationStatus.SEALED
    with pytest.raises(PermissionError):
        vault.ingest("rfc", "2026-11-01", f, "upstream/y.txt")
    vault.promote("rfc", "2026-11-01")
    current = vault.current("rfc")
    assert current is not None
    assert current.artifacts == (art,)
    assert vault.generation_path("rfc", "2026-11-01").parts[-2:] == ("rfc", "2026-11-01")


def test_ledger_chain_detects_tampering() -> None:
    ledger = InMemoryLedger()
    ledger.append("a", "s", {"n": 1})
    ledger.append("b", "s", {"n": 2})
    assert ledger.verify()
    assert [e.seq for e in ledger.events(since_seq=1)] == [2]
    ledger.tamper(1, n=99)
    assert not ledger.verify()
