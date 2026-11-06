"""One source's failure does not end the nightly build (2026-11-06: an invalid id in Aozora Bunko's
refine ended the run after 45 minutes, and no other source was indexed)."""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

import pytest

from wsp_cli import ops
from wsp_cli.indexer import Indexer, SourceReport
from wsp_cli.locks import LockHeldError
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import DocRecord, Generation, GenerationStatus, SourceSpec, SourceStatus, Tier
from wsp_index import SqliteChangeFeed, TantivyIndex
from wsp_index.refinery import generation_dir, is_sealed
from wsp_vault.cancel import Interrupted

GID = "2026-10-27"
BROKEN, GOOD = "aaa-broken", "books"


class Vault:
    def __init__(self, root: Path) -> None:
        self.root = root

    def generations(self, source_id: str) -> list[Generation]:
        return [Generation(source_id, GID, GenerationStatus.SEALED)]

    def current(self, source_id: str) -> Generation | None:
        return self.generations(source_id)[0]

    def current_set(self, source_id: str) -> list[Generation]:
        return self.generations(source_id)

    def generation_path(self, source_id: str, generation_id: str) -> Path:
        return self.root / source_id / generation_id


class Adapter:
    """Two documents; ``fail`` (when set) is raised after the first of the broken source."""

    id = "fake"

    def __init__(self) -> None:
        self.fail: BaseException | None = None

    def documents(self, ctx: Any, generation_path: Path) -> Iterator[DocRecord]:
        sid = ctx.source.id
        for n, native in enumerate(("tides", "rocks")):
            if n and sid == BROKEN and self.fail is not None:
                raise self.fail
            text = f"{native} of {sid}"
            yield DocRecord(
                make_doc_id(sid, native),
                sid,
                native,
                native.title(),
                "en",
                "CC0-1.0",
                text,
                content_hash=content_hash(text),
            )


def spec(sid: str) -> SourceSpec:
    return SourceSpec(
        id=sid,
        name=sid,
        tier=Tier.T1,
        status=SourceStatus.ACTIVE,
        license="CC0-1.0",
        legal_basis="open-license",
        domain=("books",),
        adapter="fake",
        raw={"access": {"adapter": "fake"}},
    )


@contextlib.contextmanager
def indexer(tmp_path: Path, adapter: Adapter) -> Generator[Indexer]:
    root = tmp_path / "index"
    ix = Indexer(
        Vault(tmp_path / "vault"),  # type: ignore[arg-type]
        [spec(BROKEN), spec(GOOD)],
        {"fake": adapter},  # type: ignore[dict-item]
        root,
        TantivyIndex(root / "lexical"),
        SqliteChangeFeed(root / "changes.sqlite"),
    )
    try:
        yield ix
    finally:
        ix.close()


def test_a_source_that_raises_is_reported_and_the_others_are_indexed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from wsp_core.ids import InvalidDocIdError

    adapter = Adapter()
    adapter.fail = InvalidDocIdError("invalid native id: 'aozora:048068/chino_sakazuki '")
    with indexer(tmp_path, adapter) as ix, caplog.at_level(logging.INFO):
        broken, good = ix.build()  # the nightly run: every source
        assert broken.source_id == BROKEN
        assert broken.error == "InvalidDocIdError: invalid native id: 'aozora:048068/chino_sakazuki '"
        assert broken.indexed is None
        assert (good.source_id, good.error, good.indexed, good.upserted) == (GOOD, None, GID, 2)
        assert [h.doc_id for h in ix.index.search("tides")] == [make_doc_id(GOOD, "tides")]
        assert not ix.index.has_source(BROKEN)
        refined = generation_dir(ix.refinery, BROKEN, GID)
        assert not is_sealed(refined)  # its partial refine is not taken for a finished one
        errors = [
            r for r in caplog.records if getattr(r, "fields", {}).get("event") == "phase-error"
        ]  # the run log says which step failed, and the source
        assert {(r.fields["phase"], r.fields.get("source")) for r in errors} == {  # type: ignore[attr-defined]
            ("refine", BROKEN),
            ("index-source", BROKEN),
        }
        adapter.fail = None  # the next night: the partial refine is rebuilt from the vault
        broken, good = ix.build()
        assert (broken.error, broken.indexed, broken.refined) == (None, GID, ((GID, 2),))
        assert is_sealed(refined)
        assert good.refined == ()  # already refined and indexed
        assert ix.index.has_source(BROKEN)


@pytest.mark.parametrize(
    "stop",
    [MemoryError(), KeyboardInterrupt(), Interrupted("shutdown"), LockHeldError("index lock held")],
    ids=["memory", "keyboard", "interrupted", "lock"],
)
def test_a_stop_still_ends_the_build(tmp_path: Path, stop: BaseException) -> None:
    adapter = Adapter()
    adapter.fail = stop
    with indexer(tmp_path, adapter) as ix, pytest.raises(type(stop)):
        ix.build()
    assert not (tmp_path / "index" / "refinery" / GOOD).exists()  # nothing after the stop ran


def test_the_nightly_command_exits_non_zero_when_a_source_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Ix:
        defer_suggest = False
        finished = False

        def build(self, *_a: object, **_k: object) -> list[SourceReport]:
            return [SourceReport(BROKEN, error="InvalidDocIdError: bad id"), SourceReport(GOOD, indexed=GID)]

        def rank_if_due(self) -> bool:
            return False

        def finish(self) -> None:
            self.finished = True

        def spell_if_due(self) -> bool:
            return False

    ix = Ix()

    @contextlib.contextmanager
    def fake_indexer(*_a: object) -> Generator[Ix]:
        yield ix

    def context(_args: object) -> tuple[None, dict[str, Any], tuple[SourceSpec, ...]]:
        return None, {}, ()

    def writer(_cfg: object) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    monkeypatch.setattr(ops, "_context", context)
    monkeypatch.setattr(ops, "_index_writer", writer)
    monkeypatch.setattr(ops, "open_indexer", fake_indexer)
    out = io.StringIO()
    args = argparse.Namespace(source=None)
    assert ops.cmd_index_build(args, out) == 1
    text = out.getvalue()
    assert f"{BROKEN}: FAILED (InvalidDocIdError: bad id)" in text
    assert f"{GOOD}: refined nothing new; indexed {GID}" in text
    assert f"1 source(s) failed: {BROKEN}" in text
    assert ix.finished  # the rest of the run (rank, suggest, spell) still ran
