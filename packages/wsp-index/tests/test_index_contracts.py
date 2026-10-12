"""Port contract suites run against the wsp-index reference implementations."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from wsp_core.model import DocRecord
from wsp_core.ports import ChangeFeed, LexicalIndex
from wsp_core.testing.change_feed_contract import ChangeFeedContract
from wsp_core.testing.document_store_contract import DocumentStoreContract, StoreFactory
from wsp_core.testing.lexical_index_contract import LexicalIndexContract
from wsp_index import ParquetDocumentStore, RefineryWriter, SqliteChangeFeed, TantivyConfig
from wsp_index_helpers import SnippetIndex

SMALL = TantivyConfig(writer_heap_bytes=20_000_000, writer_threads=1)


class TestTantivyIndexContract(LexicalIndexContract):
    @pytest.fixture
    def index(self, tmp_path: Path, request: pytest.FixtureRequest) -> LexicalIndex:
        idx = SnippetIndex(tmp_path / "ix", SMALL)
        request.addfinalizer(idx.close)  # noqa: PT021  # base fixture returns, not yields
        return idx


class TestParquetDocumentStoreContract(DocumentStoreContract):
    @pytest.fixture
    def make_store(self, tmp_path: Path, request: pytest.FixtureRequest) -> StoreFactory:
        def factory(generations: Sequence[tuple[str, Sequence[DocRecord]]]) -> ParquetDocumentStore:
            root = tmp_path / "refinery"
            for gen, docs in generations:
                source = docs[0].source_id if docs else "contract-src"
                # Tiny files and row groups so lookups cross file and row-group boundaries.
                with RefineryWriter(root, source, gen, row_group_rows=1, docs_per_file=2) as w:
                    w.write(docs)
            return ParquetDocumentStore(root)

        return factory


class TestSqliteChangeFeedContract(ChangeFeedContract):
    @pytest.fixture
    def feed(self, tmp_path: Path, request: pytest.FixtureRequest) -> ChangeFeed:
        f = SqliteChangeFeed(tmp_path / "feed.sqlite")
        request.addfinalizer(f.close)  # noqa: PT021  # base fixture returns, not yields
        return f
