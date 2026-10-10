"""The in-memory fakes must honour the same port contracts as the real implementations."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from wsp_api.testing.fakes import (
    InMemoryChangeFeed,
    InMemoryDocumentStore,
    InMemoryLedger,
    InMemoryLexicalIndex,
)
from wsp_core.model import DocRecord
from wsp_core.ports import ChangeFeed, DocumentStore, Ledger, LexicalIndex
from wsp_core.testing.change_feed_contract import ChangeFeedContract
from wsp_core.testing.document_store_contract import DocumentStoreContract, StoreFactory
from wsp_core.testing.ledger_contract import LedgerContract
from wsp_core.testing.lexical_index_contract import LexicalIndexContract


class TestInMemoryLedgerContract(LedgerContract):
    def make_ledger(self, directory: Path) -> Ledger:
        return InMemoryLedger()


class TestInMemoryChangeFeedContract(ChangeFeedContract):
    @pytest.fixture
    def feed(self, tmp_path: Path, request: pytest.FixtureRequest) -> ChangeFeed:
        return InMemoryChangeFeed()

    # The fake's cursors are record counts, so "0" is a cursor it really issues.
    @pytest.mark.parametrize("bad", ["garbage", "99", "x.y.z", "-1", "01"])
    def test_foreign_cursor_rejected(self, feed: ChangeFeed, bad: str) -> None:
        super().test_foreign_cursor_rejected(feed, bad)


class TestInMemoryLexicalIndexContract(LexicalIndexContract):
    @pytest.fixture
    def index(self, tmp_path: Path, request: pytest.FixtureRequest) -> LexicalIndex:
        return InMemoryLexicalIndex()


class TestInMemoryDocumentStoreContract(DocumentStoreContract):
    @pytest.fixture
    def make_store(self, tmp_path: Path, request: pytest.FixtureRequest) -> StoreFactory:
        def build(generations: Sequence[tuple[str, Sequence[DocRecord]]]) -> DocumentStore:
            store = InMemoryDocumentStore()
            previous: set[str] = set()
            for gen, docs in generations:
                for doc in docs:
                    store.put(gen, doc)
                for gone in previous - {d.doc_id for d in docs}:
                    store.remove(gen, gone)  # pyright: ignore[reportArgumentType]  # ids come from DocRecords
                previous = {d.doc_id for d in docs}
            return store

        return build
