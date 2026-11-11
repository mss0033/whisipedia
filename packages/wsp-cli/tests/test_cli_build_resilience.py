"""One source's failure does not end the nightly build (2026-11-06: an invalid id in Aozora Bunko's
refine ended the run after 45 minutes, and no other source was indexed)."""

from __future__ import annotations

import argparse
import contextlib
import errno
import io
import json
import logging
import os
from collections.abc import Callable, Generator, Iterator
from pathlib import Path
from typing import Any

import pytest

from wsp_cli import ops
from wsp_cli.indexer import Indexer, IndexStateError, SourceReport
from wsp_cli.locks import LockHeldError
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import DocRecord, Generation, GenerationStatus, SourceSpec, SourceStatus, Tier
from wsp_index import SqliteChangeFeed, TantivyIndex
from wsp_index.refinery import generation_dir, is_sealed
from wsp_orchestrator.errors import StorageUnavailableError
from wsp_vault.canary import MountUnavailableError
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
def indexer(
    tmp_path: Path, adapter: Adapter, storage_available: Callable[[], bool] | None = None
) -> Generator[Indexer]:
    root = tmp_path / "index"
    ix = Indexer(
        Vault(tmp_path / "vault"),  # type: ignore[arg-type]
        [spec(BROKEN), spec(GOOD)],
        {"fake": adapter},  # type: ignore[dict-item]
        root,
        TantivyIndex(root / "lexical"),
        SqliteChangeFeed(root / "changes.sqlite"),
        storage_available=storage_available,
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


def _wrapped_estale() -> RuntimeError:
    try:
        raise OSError(errno.ESTALE, "Stale file handle")
    except OSError as cause:
        try:
            raise RuntimeError("refinery part unreadable") from cause
        except RuntimeError as exc:
            return exc


@pytest.mark.parametrize(
    "stop",
    [
        MemoryError(),
        KeyboardInterrupt(),
        Interrupted("shutdown"),
        LockHeldError("index lock held"),
        OSError(errno.ESTALE, "Stale file handle"),  # the NAS went away: every source would "fail"
        OSError(errno.ENOSPC, "No space left on device"),
        MountUnavailableError("vault mount not answering"),
        StorageUnavailableError("NAS down"),
        IndexStateError("lexical index rebuilt alone"),
        _wrapped_estale(),  # raised from a storage error: still the storage
    ],
    ids=[
        "memory",
        "keyboard",
        "interrupted",
        "lock",
        "estale",
        "enospc",
        "mount",
        "storage",
        "state",
        "cause",
    ],
)
def test_a_stop_still_ends_the_build(tmp_path: Path, stop: BaseException) -> None:
    adapter = Adapter()
    adapter.fail = stop
    with indexer(tmp_path, adapter) as ix, pytest.raises(type(stop)):
        ix.build()
    assert not (tmp_path / "index" / "refinery" / GOOD).exists()  # nothing after the stop ran


def _from_text() -> ValueError:
    try:
        raise OSError(None, "IOError: Couldn't deserialize thrift")  # pyarrow: no errno
    except OSError as cause:
        try:
            raise ValueError("Failed to open file for read: The network name is no longer available")
        except ValueError as exc:
            exc.__cause__ = cause
            return exc


@pytest.mark.parametrize(
    "fault",
    [OSError(errno.EIO, "Input/output error"), OSError(errno.ETIMEDOUT, "timed out"), _from_text()],
    ids=["eio", "etimedout", "valueerror"],
)
def test_a_failure_while_the_storage_does_not_answer_ends_the_build(tmp_path: Path, fault: Exception) -> None:
    """A soft mount's EIO, or an error whose type says nothing, while the mounts do not answer: every
    later source would fail the same way, holding the index lock (2026-11-06 review)."""
    adapter = Adapter()
    adapter.fail = fault
    probes: list[bool] = []

    def gone() -> bool:
        probes.append(False)
        return False

    with indexer(tmp_path, adapter, gone) as ix, pytest.raises(type(fault)):
        ix.build()
    assert probes == [False]
    assert not (tmp_path / "index" / "refinery" / GOOD).exists()


@pytest.mark.parametrize(
    "fault",
    [OSError(errno.EIO, "Input/output error"), _from_text()],
    ids=["eio", "valueerror"],
)
def test_the_same_failure_with_the_storage_answering_is_the_sources_own(
    tmp_path: Path, fault: Exception
) -> None:
    adapter = Adapter()
    adapter.fail = fault
    with indexer(tmp_path, adapter, lambda: True) as ix:
        broken, good = ix.build()
    assert broken.error is not None
    assert good.indexed == GID


def test_a_probe_that_raises_counts_as_storage_gone(tmp_path: Path) -> None:
    adapter = Adapter()
    adapter.fail = RuntimeError("odd")

    def broken_probe() -> bool:
        raise OSError(errno.ENOTCONN, "not connected")

    with indexer(tmp_path, adapter, broken_probe) as ix, pytest.raises(RuntimeError, match="odd"):
        ix.build()


def test_a_rollback_that_fails_ends_the_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Writes that cannot be discarded would be published by the next source's commit."""
    adapter = Adapter()
    adapter.fail = RuntimeError("bad record")
    with indexer(tmp_path, adapter) as ix:
        ix.index.upsert([])  # open the index, as an earlier source would have

        def cannot() -> None:
            raise RuntimeError("writer poisoned")

        monkeypatch.setattr(ix.index, "rollback", cannot)
        with pytest.raises(RuntimeError, match="writer poisoned"):
            ix.build()
        assert not (tmp_path / "index" / "refinery" / GOOD).exists()


def test_a_failure_after_committed_parts_leaves_only_those_and_the_next_run_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refresh that committed a part and then failed: its uncommitted writes are rolled back (the
    next source's commit would have published them), its state is not saved, and the next run
    resumes into the whole generation."""
    from wsp_cli import indexer as indexer_module
    from wsp_index import refresh as real

    def partly(index: TantivyIndex, feed: Any, old: Any, new: Any, **kw: Any) -> Any:
        if new.source_id != BROKEN:
            return real(index, feed, old, new, **kw)
        docs = list(Adapter().documents(argparse.Namespace(source=spec(BROKEN)), Path()))
        index.upsert(docs[:1])
        index.commit(rebuild_suggest=False)  # a part committed
        index.upsert(docs[1:])  # the next part, half written
        raise RuntimeError("refinery part unreadable")

    monkeypatch.setattr(indexer_module, "refresh", partly)
    with indexer(tmp_path, Adapter()) as ix:
        broken, good = ix.build()
        assert broken.error == "RuntimeError: refinery part unreadable"
        assert good.indexed == GID  # its commit came after the failure
        assert [h.doc_id for h in ix.index.search(f"tides {BROKEN}")][:1] == [make_doc_id(BROKEN, "tides")]
        assert make_doc_id(BROKEN, "rocks") not in {h.doc_id for h in ix.index.search("rocks")}
        assert BROKEN not in json.loads((tmp_path / "index" / "index-state.json").read_text())
        monkeypatch.setattr(indexer_module, "refresh", real)
        broken, _ = ix.build()  # the next night
        assert (broken.error, broken.indexed) == (None, GID)
        assert make_doc_id(BROKEN, "rocks") in {h.doc_id for h in ix.index.search("rocks")}


def test_the_nightly_command_exits_non_zero_when_a_source_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Ix:
        defer_suggest = False
        shared: list[str] = []  # noqa: RUF012 - one instance
        root = tmp_path / "index"
        failing = True

        def build(self, *_a: object, **_k: object) -> list[SourceReport]:
            error = "InvalidDocIdError: bad id" if self.failing else None
            return [SourceReport(BROKEN, indexed=GID, error=error), SourceReport(GOOD, indexed=GID)]

        def rank_if_due(self) -> bool:
            self.shared.append("rank")
            return False

        def finish(self) -> None:
            self.shared.append("finish")

        def spell_if_due(self) -> bool:
            self.shared.append("spell")
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
    alerts: list[tuple[str, str | None]] = []

    def alert(_cfg: object, key: str, message: str | None) -> None:
        alerts.append((key, message))

    monkeypatch.setattr(ops, "_alert", alert)
    ix.root.mkdir()
    out = io.StringIO()
    args = argparse.Namespace(source=None)
    assert ops.cmd_index_build(args, out) == 1
    text = out.getvalue()
    assert f"{BROKEN}: FAILED (InvalidDocIdError: bad id)" in text
    assert f"{GOOD}: refined nothing new; indexed {GID}" in text
    assert f"1 source(s) failed: {BROKEN}" in text
    assert "rank, suggest and spell left as they were" in text
    assert ix.shared == []  # not rebuilt from a set missing a source: the last good files stay
    assert alerts == []  # the first night: the exit code says it

    # The night before was skipped too: from the second night in a row, an alert.
    skipped = ix.root / ops.SHARED_SKIPPED_FILE
    skipped.write_text(json.dumps({"days": ["2000-01-01"]}), encoding="utf-8")
    out = io.StringIO()
    assert ops.cmd_index_build(args, out) == 1
    ((key, message),) = alerts
    assert key == ops.SHARED_SKIPPED_ALERT
    assert message is not None
    assert "for 2 days (since 2000-01-01)" in message
    assert BROKEN in message
    assert "ALERT: rank, suggest and spell not rebuilt" in out.getvalue()

    # A lane's partial build (one source, no rank or spell), a reindex or a refine-again neither counts
    # a day nor clears the count: the shared phases were not the library's to run.
    days_before = skipped.read_text(encoding="utf-8")
    for partial in (
        argparse.Namespace(source=BROKEN, no_rank=True, no_spell=True),
        argparse.Namespace(source=None, no_rank=True),
        argparse.Namespace(source=None, no_suggest=True),
    ):
        ix.failing = True
        assert ops.cmd_index_build(partial, io.StringIO()) == 1
        ix.failing = False
        assert ops.cmd_index_build(partial, io.StringIO()) == 0
    assert skipped.read_text(encoding="utf-8") == days_before
    assert len(alerts) == 1  # neither raised again nor resolved

    ix.failing = False  # fixed: the full build runs the shared phases; the count and the alert are cleared
    ix.shared.clear()
    assert ops.cmd_index_build(args, io.StringIO()) == 0
    assert ix.shared == ["rank", "finish", "spell"]
    assert not skipped.exists()
    assert alerts[-1] == (ops.SHARED_SKIPPED_ALERT, None)
    # The counter gone (removed by hand) while the alert is open: a good full build still resolves it.
    alerts.clear()
    assert ops.cmd_index_build(args, io.StringIO()) == 0
    assert alerts == [(ops.SHARED_SKIPPED_ALERT, None)]


def test_the_storage_probe_gives_up_on_a_hung_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from wsp_cli import wiring

    (tmp_path / "vault" / "x").mkdir(parents=True)
    (tmp_path / "refinery").mkdir()
    assert wiring.storage_probes(tmp_path / "vault", tmp_path / "refinery")()
    assert not wiring.storage_probes(tmp_path / "vault", tmp_path / "gone")()
    assert not wiring.storage_probes(tmp_path / "empty-mount-point", None)()
    release = threading.Event()
    real = os.scandir

    def hung(path: Any) -> Any:
        release.wait(5)
        return real(path)

    monkeypatch.setattr(os, "scandir", hung)  # wiring calls os.scandir
    try:
        assert not wiring.storage_probes(tmp_path / "vault", None, timeout=0.2)()
    finally:
        release.set()


class PromotingVault(Vault):
    """CURRENT moves to ``r2`` (sealed by a lane) while the build refines the first generation."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.ids = [GID]
        self.current_id = GID

    def generations(self, source_id: str) -> list[Generation]:
        return [Generation(source_id, g, GenerationStatus.SEALED) for g in self.ids]

    def current(self, source_id: str) -> Generation | None:
        return Generation(source_id, self.current_id, GenerationStatus.SEALED)

    def current_set(self, source_id: str) -> list[Generation]:
        current = self.current(source_id)
        return [] if current is None else [current]


class PromotingAdapter(Adapter):
    def __init__(self, vault: PromotingVault) -> None:
        super().__init__()
        self.vault = vault
        self.promote = True

    def documents(self, ctx: Any, generation_path: Path) -> Iterator[DocRecord]:
        if self.promote and ctx.source.id == GOOD:
            self.promote = False
            self.vault.ids.append(f"{GID}r2")  # the collections lane seals and promotes r2 mid-refine
            self.vault.current_id = f"{GID}r2"
        yield from super().documents(ctx, generation_path)


def test_a_generation_promoted_during_the_refine_is_left_to_the_next_run(tmp_path: Path) -> None:
    # 2026-11-07: crs-reports/2026-10-20r2 was promoted 11 minutes into the refine of 2026-10-20, and
    # the build indexed r2, which had no Refinery copy: FileNotFoundError on its _SUCCESS.json.
    vault = PromotingVault(tmp_path / "vault")
    adapter = PromotingAdapter(vault)
    root = tmp_path / "index"
    ix = Indexer(
        vault,  # type: ignore[arg-type]
        [spec(GOOD)],
        {"fake": adapter},  # type: ignore[dict-item]
        root,
        TantivyIndex(root / "lexical"),
        SqliteChangeFeed(root / "changes.sqlite"),
    )
    try:
        (report,) = ix.build()
        assert (report.error, report.indexed, report.refined) == (None, GID, ((GID, 2),))
        (report,) = ix.build()  # the next night: r2 is refined and indexed
        assert (report.error, report.indexed, report.refined) == (None, f"{GID}r2", ((f"{GID}r2", 2),))
        assert is_sealed(generation_dir(ix.refinery, GOOD, f"{GID}r2"))
    finally:
        ix.close()


# --- the nightly build marks its start and its end (2026-11-10) ------------------------------------------
# Killed at 04:59 by a shutdown, the build did not run again until 04:43 the next day: nothing on disk
# said that the last full build had not finished.

FULL = argparse.Namespace(source=None)


class _MarkedIx:
    """The indexer `index build` drives, reduced to what the command calls."""

    defer_suggest = False

    def __init__(self, root: Path, mark: Path) -> None:
        self.root = root
        self.mark = mark
        self.error: str | None = None
        self.fail: BaseException | None = None
        self.during: dict[str, Any] | None = None  # the mark as the running build saw it

    def build(self, *_a: object, **_k: object) -> list[SourceReport]:
        self.during = json.loads(self.mark.read_text()) if self.mark.is_file() else None
        if self.fail is not None:
            raise self.fail
        return [SourceReport(GOOD, indexed=GID, error=self.error)]

    def reindex(self, source_id: str) -> SourceReport:
        return self.build()[0]

    def rank_if_due(self) -> bool:
        return False

    def finish(self) -> None:
        pass

    def spell_if_due(self) -> bool:
        return False


@pytest.fixture
def marked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _MarkedIx:
    state = tmp_path / "state"
    ix = _MarkedIx(tmp_path / "index", state / ops.FULL_BUILD_MARK)
    ix.root.mkdir()
    cfg = {"orchestrator": {"state_db": str(state / "orchestrator.sqlite3")}}

    @contextlib.contextmanager
    def fake_indexer(*_a: object) -> Generator[_MarkedIx]:
        yield ix

    def context(_args: object) -> tuple[None, dict[str, Any], tuple[SourceSpec, ...]]:
        return None, cfg, ()

    def writer(_cfg: object) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def alert(_cfg: object, _key: str, _message: str | None) -> None:
        pass

    monkeypatch.setattr(ops, "_context", context)
    monkeypatch.setattr(ops, "_index_writer", writer)
    monkeypatch.setattr(ops, "open_indexer", fake_indexer)
    monkeypatch.setattr(ops, "_alert", alert)
    return ix


def test_a_full_build_marks_its_start_and_its_end(marked: _MarkedIx) -> None:
    assert ops.cmd_index_build(FULL, io.StringIO()) == 0
    during = marked.during
    assert during is not None, "the start is marked before the build does anything"
    assert sorted(during) == ["pid", "since", "started"]  # no "ended": cut here, it reads "interrupted"
    assert during["pid"] == os.getpid()
    after = json.loads(marked.mark.read_text())
    assert sorted(after) == ["ended", "exit", "last_ok", "pid", "since", "started"]
    assert (after["exit"], after["started"], after["since"]) == (0, during["started"], during["started"])
    assert after["last_ok"] == after["ended"] >= after["started"]
    assert not list(marked.mark.parent.glob("*.partial"))


def test_a_build_with_a_failed_source_is_marked_ended(marked: _MarkedIx) -> None:
    assert ops.cmd_index_build(FULL, io.StringIO()) == 0
    clean = json.loads(marked.mark.read_text())
    marked.error = "InvalidDocIdError: bad id"
    assert ops.cmd_index_build(FULL, io.StringIO()) == 1
    failed = json.loads(marked.mark.read_text())
    assert failed["exit"] == 1
    assert "ended" in failed, "failed by itself: not started again automatically"
    # what tools/wsp-stall counts from: the last clean end, and the first mark
    assert (failed["last_ok"], failed["since"]) == (clean["ended"], clean["since"])
    assert marked.during is not None
    assert marked.during["last_ok"] == clean["ended"], "kept while the next build runs, and if it is cut"


def test_a_build_that_raises_is_marked_ended_with_what_it_raised(marked: _MarkedIx) -> None:
    marked.fail = MountUnavailableError("the share is gone")
    with pytest.raises(MountUnavailableError):
        ops.cmd_index_build(FULL, io.StringIO())
    doc = json.loads(marked.mark.read_text())
    assert doc["exit"] == "MountUnavailableError"
    assert "ended" in doc
    assert "last_ok" not in doc


def test_a_per_source_build_writes_no_marker(marked: _MarkedIx) -> None:
    for partial in (
        argparse.Namespace(source=GOOD),
        argparse.Namespace(source=None, no_rank=True),
        argparse.Namespace(source=GOOD, reindex=True),
    ):
        assert ops.cmd_index_build(partial, io.StringIO()) == 0
    assert not marked.mark.exists()
    assert marked.during is None


def test_a_busy_lock_writes_no_marker(marked: _MarkedIx, monkeypatch: pytest.MonkeyPatch) -> None:
    marked.mark.parent.mkdir(parents=True)
    interrupted = '{"pid": 7, "since": "2026-11-09T09:30:00Z", "started": "2026-11-10T09:33:00Z"}\n'
    marked.mark.write_text(interrupted)

    @contextlib.contextmanager
    def busy(_cfg: object) -> Generator[None]:
        raise LockHeldError("another 'heavy' is running (pid 7)")
        yield

    monkeypatch.setattr(ops, "_index_writer", busy)
    assert ops.cmd_index_build(FULL, io.StringIO()) == 3
    assert marked.mark.read_text() == interrupted, "the interrupted build's mark is left for the resume"


def test_a_mark_that_cannot_be_written_never_fails_the_build(
    marked: _MarkedIx, capsys: pytest.CaptureFixture[str]
) -> None:
    marked.mark.parent.mkdir(parents=True)
    marked.mark.mkdir()  # os.replace onto a directory fails
    assert ops.cmd_index_build(FULL, io.StringIO()) == 0
    assert "index-full-build.json not written" in capsys.readouterr().err
    assert [p.name for p in marked.mark.parent.iterdir()] == [ops.FULL_BUILD_MARK]


KILLED_BUILD = """
import contextlib, sys, time
from pathlib import Path
from wsp_cli import main, ops

tmp = Path(sys.argv[1])

class Ix:
    defer_suggest = False
    root = tmp / "index"
    def build(self, *a, **k):
        (tmp / "building").touch()
        time.sleep(600)  # the hook: an indexer that never returns; the test kills the process here
        return []

@contextlib.contextmanager
def fake_indexer(*a):
    yield Ix()

cfg = {"orchestrator": {"state_db": str(tmp / "state" / "orchestrator.sqlite3")}}
ops._context = lambda args: (None, cfg, ())
ops._index_writer = lambda cfg: contextlib.nullcontext()
ops.open_indexer = fake_indexer
sys.exit(main.main(["--config-dir", str(tmp / "config"), "index", "build"]))
"""


def test_a_killed_build_leaves_the_marker_open(tmp_path: Path) -> None:
    """The real command (`wsp index build` through wsp_cli.main) with an indexer that blocks: systemd's
    SIGTERM at a shutdown must not reach Python as an exception, or the mark would be closed and the
    build never started again. This fails the day someone installs a SIGTERM handler in the CLI."""
    import signal
    import subprocess
    import sys
    import time

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text("")
    proc = subprocess.Popen(
        [sys.executable, "-c", KILLED_BUILD, str(tmp_path)],
        env={**{k: v for k, v in os.environ.items() if not k.startswith("WSP_")}, "WSP_NO_LOCAL_CONFIG": "1"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not (tmp_path / "building").exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (tmp_path / "building").exists(), "the build never started"
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
        _, err = proc.communicate()
    assert code == -signal.SIGTERM, err
    doc = json.loads((tmp_path / "state" / ops.FULL_BUILD_MARK).read_text())
    assert sorted(doc) == ["pid", "since", "started"]
    assert doc["pid"] == proc.pid
