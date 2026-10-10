"""Contract suite for ``DocumentStore`` (PLAN §11.1: ``/v1/documents/{id}`` and ``/history``).

The port is read-only, so subclasses provide a ``make_store`` fixture: a factory that receives the
generations of one source, oldest first, as ``(generation_id, docs)`` pairs and returns a store over
them. Generation ids sort chronologically.

Semantics pinned here:

* ``get(id)`` reads the newest generation; a document missing there (tombstoned) returns ``None``.
* ``get(id, at_generation=g)`` reads the newest generation at or before ``g`` (``g`` need not exist).
* ``history(id)`` is ``(generation_id, content_hash)`` oldest first, one entry per content change
  (generations where the content did not change are not repeated; re-appearing after a removal is a
  new entry).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wsp_core.ids import DocId, content_hash, make_doc_id
from wsp_core.model import DocRecord
from wsp_core.ports import DocumentStore

SOURCE = "contract-src"

StoreFactory = Callable[[Sequence[tuple[str, Sequence[DocRecord]]]], DocumentStore]


def make_doc(native_id: str, text: str, **extra: object) -> DocRecord:
    fields: dict[str, object] = {
        "doc_id": make_doc_id(SOURCE, native_id),
        "source_id": SOURCE,
        "native_id": native_id,
        "title": native_id.replace("_", " "),
        "lang": "en",
        "license": "CC-BY-SA-4.0",
        "text": text,
        "content_hash": content_hash(text),
    }
    fields.update(extra)
    return DocRecord(**fields)  # pyright: ignore[reportArgumentType]  # test helper, fields known


A1 = make_doc(
    "Alpha",
    "alpha one",
    revision="r1",
    timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    url_native="https://example.org/Alpha",
    html_ref="zim:A/Alpha",
    entities=("Q1",),
    links=("wsp:contract-src/Beta",),
    aliases=("First letter",),
)
A2 = make_doc("Alpha", "alpha two")
B1 = make_doc("Beta", "beta one")
C1 = make_doc("Gamma", "gamma one")

GENERATIONS: tuple[tuple[str, tuple[DocRecord, ...]], ...] = (
    ("2026-01-01", (A1, B1, C1)),
    ("2026-02-01", (A1, B1)),  # Gamma removed
    ("2026-03-01", (A2, B1, C1)),  # Alpha modified, Gamma back
)


def _id(native: str) -> DocId:
    return make_doc_id(SOURCE, native)


class DocumentStoreContract:
    @pytest.fixture
    def make_store(self, tmp_path: Path, request: pytest.FixtureRequest) -> StoreFactory:
        """Build an empty instance under ``tmp_path``; clean up via ``request.addfinalizer``."""
        raise NotImplementedError("subclass must provide a `make_store` fixture")

    @pytest.fixture
    def store(self, make_store: StoreFactory) -> DocumentStore:
        return make_store(GENERATIONS)

    def test_satisfies_protocol(self, store: DocumentStore) -> None:
        assert isinstance(store, DocumentStore)

    def test_get_reads_newest_generation(self, store: DocumentStore) -> None:
        doc = store.get(_id("Alpha"))
        assert doc is not None
        assert doc.text == "alpha two"
        assert doc.content_hash == content_hash("alpha two")

    def test_get_at_generation(self, store: DocumentStore) -> None:
        assert store.get(_id("Alpha"), at_generation="2026-01-01") == A1
        assert store.get(_id("Gamma"), at_generation="2026-02-01") is None
        assert store.get(_id("Alpha"), at_generation="1999-01-01") is None

    def test_get_at_generation_between_generations(self, store: DocumentStore) -> None:
        assert store.get(_id("Alpha"), at_generation="2026-02-15") == A1
        assert store.get(_id("Gamma"), at_generation="2026-02-15") is None  # tombstoned then
        alpha = store.get(_id("Alpha"), at_generation="2099-12-31")
        assert alpha is not None
        assert alpha.text == "alpha two"

    def test_round_trips_every_field(self, store: DocumentStore) -> None:
        doc = store.get(_id("Alpha"), at_generation="2026-02-01")
        assert doc == A1
        assert doc is not None
        assert doc.timestamp == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    def test_unknown_and_malformed_ids(self, store: DocumentStore) -> None:
        assert store.get(_id("Nope")) is None
        assert store.get(make_doc_id("other-source", "Alpha")) is None
        assert store.get(DocId("not-a-doc-id")) is None
        assert list(store.history(DocId("not-a-doc-id"))) == []
        assert list(store.history(_id("Nope"))) == []

    def test_history_collapses_unchanged_generations(self, store: DocumentStore) -> None:
        assert list(store.history(_id("Alpha"))) == [
            ("2026-01-01", content_hash("alpha one")),
            ("2026-03-01", content_hash("alpha two")),
        ]
        assert list(store.history(_id("Beta"))) == [("2026-01-01", content_hash("beta one"))]

    def test_history_records_reappearance(self, store: DocumentStore) -> None:
        h = content_hash("gamma one")
        assert list(store.history(_id("Gamma"))) == [("2026-01-01", h), ("2026-03-01", h)]

    def test_tombstoned_document_returns_none_at_head(self, make_store: StoreFactory) -> None:
        store = make_store([("2026-01-01", (A1, B1)), ("2026-02-01", (B1,))])
        assert store.get(_id("Alpha")) is None
        assert store.get(_id("Alpha"), at_generation="2026-01-01") == A1
        assert list(store.history(_id("Alpha"))) == [("2026-01-01", A1.content_hash)]
