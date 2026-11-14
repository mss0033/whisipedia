"""Source-integrity plumbing of the composition root: profiles, health reporting, interactive-use
signal (ADR-0009)."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import logging
import os
import shutil
import time
from collections.abc import Callable, Iterator, MutableMapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from libzim.writer import Creator, Hint, Item, StringProvider

from wsp_cli import integrity
from wsp_cli.integrity import (
    UNPACED,
    ProfileCheckpoint,
    Profiler,
    ReadPacer,
    channel_summary,
    checkpoint_fingerprint,
    read_integrity_state,
    zim_entries,
)
from wsp_cli.monitor import LibraryMonitor
from wsp_core.activity import ActivityMiddleware, client_path, recently_active, seconds_since
from wsp_core.model import Artifact, Generation, GenerationStatus
from wsp_orchestrator.integrity import ContentProfile, ProfileBuilder, compare_profiles
from wsp_orchestrator.jsonlog import configure_logging
from wsp_orchestrator.store import StateStore
from wsp_orchestrator.testing import make_spec

NOW = datetime(2026, 10, 15, tzinfo=UTC)


class _Item(Item):
    def __init__(self, path: str, mimetype: str, content: str) -> None:
        super().__init__()
        self._path, self._mimetype, self._content = path, mimetype, content

    def get_path(self) -> str:
        return self._path

    def get_title(self) -> str:
        return self._path

    def get_mimetype(self) -> str:
        return self._mimetype

    def get_contentprovider(self) -> StringProvider:
        return StringProvider(self._content)

    def get_hints(self) -> dict[Hint, int]:
        return {Hint.FRONT_ARTICLE: self._mimetype == "text/html"}


def _zim(path: Path, items: list[tuple[str, str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with Creator(path).config_indexing(False, "eng").config_verbose(False) as creator:
        creator.set_mainpath(items[0][0])
        for p, mime, body in items:
            creator.add_item(_Item(p, mime, body))
    return path


def test_zim_profile_sees_scripts_and_new_mime_types(tmp_path: Path) -> None:
    clean = [(f"A/doc{i}", "text/html", f"<p>article {i}</p>") for i in range(200)]
    tampered = [
        (p, m, b + ("<script>steal()</script>" * 3 if i % 2 == 0 else ""))
        for i, (p, m, b) in enumerate(clean)
    ]
    tampered.append(("X/payload.exe", "application/x-msdownload", "MZ fake"))
    old_zim = _zim(tmp_path / "old" / "upstream" / "w.zim", clean)
    new_zim = _zim(tmp_path / "new" / "upstream" / "w.zim", tampered)
    profiler = Profiler(refinery_root=None, sample_rate=1.0)

    def gen(gid: str) -> Generation:
        art = Artifact("w", gid, Path("upstream/w.zim"), 1, "x", "y")  # type: ignore[arg-type]
        return Generation("w", gid, GenerationStatus.SEALED, (art,))

    old = profiler(make_spec("w"), gen("old"), old_zim.parent.parent)
    new = profiler(make_spec("w"), gen("new"), new_zim.parent.parent)
    assert old.doc_count >= 200
    assert old.script_tags == 0
    assert new.script_tags >= 300
    failed = {c.name for c in compare_profiles(old, new)[0] if not c.ok and c.holds}
    assert {"script-density", "new-mimetypes", "executables", "changed-fraction"} <= failed


def test_zim_entries_as_blobs_do_not_count_documents(tmp_path: Path) -> None:
    z = _zim(tmp_path / "a.zim", [("A/x", "text/html", "<p>x</p>"), ("I/y.png", "image/png", "png")])
    b = ProfileBuilder(sample_rate=1.0)
    assert zim_entries(z, b, documents=False) >= 2
    built = b.build()
    assert built.doc_count == 0
    assert built.mimetypes.get("image/png") == 1


def test_integrity_state_and_monitor(tmp_path: Path) -> None:
    db = tmp_path / "orchestrator.sqlite3"
    store = StateStore(db, check_local=False)
    store.put_origin_pin("wp", "download.kiwix.org", "origin", "Let's Encrypt", "c", NOW, NOW)
    channel = {"at": NOW.isoformat(), "version": "2026-10", "status": ["independent", "unavailable"]}
    store.set_meta("channel:wp", json.dumps({**channel, "verdict": "info"}))
    store.open_alert("origin-drift:wp:origin-issuer-changed:download.kiwix.org", "warning", "issuer", {}, NOW)
    store.open_alert("integrity:osm/2026-10-01", "critical", "quarantined", {}, NOW)
    with store.tx() as c:
        for sid, ver, state, hold, err in (
            ("wp", "2026-11", "indexed", "deleted-fraction: 20%", None),
            ("osm", "2026-10-01", "quarantined", None, "integrity: checksum-channel-mismatch"),
        ):
            c.execute(
                "INSERT INTO releases(source_id, version, state, generation_id, ref_json, hold_reason,"
                " last_error, created_at, updated_at) VALUES (?, ?, ?, ?, '{}', ?, ?, 'x', 'x')",
                (sid, ver, state, ver, hold, err),
            )
    store.close()
    state = read_integrity_state(db)
    assert state.pinned_hosts == 1
    assert [h["version"] for h in state.held] == ["2026-11"]
    assert [q["source_id"] for q in state.quarantined] == ["osm"]
    assert channel_summary(state, ["wp", "osm"]) == {"wp": "unavailable", "osm": "never checked"}
    mon = LibraryMonitor(
        sources=[make_spec("wp", adapter="kiwix-opds"), make_spec("osm")],
        vault=None,
        state_db=db,
        scrub_db=None,
        proposals=list,
        downloads_dir=None,
        disks={},
        background=False,
    )
    integrity = mon.status(NOW).integrity
    assert integrity is not None
    assert {c.source_id: c.channel for c in integrity.channels} == {
        "osm": "never checked",
        "wp": "unavailable",
    }
    assert [h.reason for h in integrity.held] == ["deleted-fraction: 20%"]
    assert integrity.quarantined[0].source_id == "osm"
    assert {a.key for a in integrity.drift_alerts} == {
        "origin-drift:wp:origin-issuer-changed:download.kiwix.org",
        "integrity:osm/2026-10-01",
    }


def _request(marker: Path, *headers: tuple[bytes, bytes], path: str = "/v1/search") -> None:
    """One HTTP request through a fresh ActivityMiddleware that marks every request it counts."""

    async def app(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        pass

    mw = ActivityMiddleware(app, path=marker, min_interval=0.0)
    scope = {"type": "http", "path": path, "headers": list(headers)}
    asyncio.run(mw(scope, None, None))  # type: ignore[arg-type]


def test_a_probe_user_agent_does_not_count(tmp_path: Path) -> None:
    """For a client that can set its User-Agent and nothing else (2026-11-10 and 11-11: scripts that
    did not mark themselves froze the background jobs for hours)."""
    marker = tmp_path / "state" / "last-interactive"
    _request(marker, (b"user-agent", b"Mozilla/5.0 (X11; Linux x86_64) Whisipedia-Probe/trawl"))
    _request(marker, (b"accept", b"*/*"), (b"user-agent", b"whisipedia-probe"))
    assert not marker.exists()
    assert not client_path(marker).exists()
    _request(marker, (b"user-agent", b"Mozilla/5.0 (X11; Linux x86_64) Firefox/131.0"))
    assert marker.exists(), "a browser still counts"


def test_the_marker_names_the_client_that_counted(tmp_path: Path) -> None:
    marker = tmp_path / "state" / "last-interactive"
    _request(marker, (b"user-agent", b"curl/8.5.0"))
    assert marker.read_bytes() == b"", "the marker itself stays empty: its mtime is the signal"
    assert client_path(marker) == tmp_path / "state" / "last-interactive.client"
    assert client_path(marker).read_text() == "curl/8.5.0\n"
    # one printable line of 80 characters, whatever was sent; no path and no query
    _request(marker, (b"user-agent", b"agent\r\nX: y \xff\x00" + b"a" * 300), path="/v1/search?q=secret")
    text = client_path(marker).read_text()
    assert text == "agent??X: y ??" + "a" * 66 + "\n"
    assert len(text) == 81
    # a probe changes neither file
    before = (marker.stat().st_mtime_ns, text)
    _request(marker, (b"user-agent", b"python-httpx/0.27"), (b"x-whisipedia-probe", b"verify"))
    assert (marker.stat().st_mtime_ns, client_path(marker).read_text()) == before


def test_an_unknown_client_still_counts(tmp_path: Path) -> None:
    marker = tmp_path / "state" / "last-interactive"
    _request(marker)
    assert recently_active(marker, 60)
    assert client_path(marker).read_text() == "(no user agent)\n"


def test_activity_marker_counts_interactive_requests_only(tmp_path: Path) -> None:
    marker = tmp_path / "state" / "last-interactive"
    seen: list[str] = []

    async def app(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        seen.append(str(scope["path"]))

    mw = ActivityMiddleware(app, path=marker, min_interval=0.0)

    def call(path: str) -> None:
        asyncio.run(mw({"type": "http", "path": path}, None, None))  # type: ignore[arg-type]

    call("/v1/system")
    call("/v1/health")
    call("/v1/changes/stream")  # an open page's live feed: a tab left open is not someone reading
    call("/v1/changes")
    assert not marker.exists()
    # A machine's probe of the start page (the health check, every two minutes) is not the owner.
    asyncio.run(
        mw(
            {"type": "http", "path": "/v1/portal", "headers": [(b"x-whisipedia-probe", b"health")]},
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
        )
    )
    assert not marker.exists()
    call("/v1/search")
    assert marker.exists()
    assert recently_active(marker, 60)
    old = time.time() - 3600
    os.utime(marker, (old, old))
    assert not recently_active(marker, 60)
    assert (seconds_since(marker) or 0) > 3000
    assert seconds_since(None) is None
    assert seen == [
        "/v1/system",
        "/v1/health",
        "/v1/changes/stream",
        "/v1/changes",
        "/v1/portal",
        "/v1/search",
    ]
    off = ActivityMiddleware(app, path=None)
    asyncio.run(off({"type": "http", "path": "/x"}, None, None))  # type: ignore[arg-type]


def test_read_pacer_rests_three_times_as_long_as_it_worked_at_a_quarter_share() -> None:
    clock = {"t": 0.0}
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        clock["t"] += s

    pacer = ReadPacer(0.25, burst=0.5, every=1, clock=lambda: clock["t"], sleep=sleep)
    pacer.tick()  # starts the first burst
    for _ in range(10):
        clock["t"] += 0.1  # a tenth of a second of reading per call
        pacer.tick()
    assert slept == pytest.approx([1.5, 1.5])  # two 0.5 s bursts, each followed by 1.5 s at rest
    assert sum(slept) / clock["t"] == pytest.approx(0.75)


def test_read_pacer_unpaced_never_sleeps() -> None:
    for share in (None, 1.0, 2.0):
        pacer = ReadPacer(share, every=1, sleep=lambda s: pytest.fail("slept"))
        for _ in range(5):
            pacer.tick()
    with pytest.raises(ValueError, match="positive"):
        ReadPacer(0.0)


# --- a profile that continues where it stopped (trawl 2026-11-10: 58 hours lost to one restart) ------


class _CutError(Exception):
    """The profile was stopped (a restart of the machine, in real life)."""


class _CountingArchive:
    """A libzim archive that notes each entry it is asked for and can be cut after some."""

    def __init__(self, archive: Any, read: list[int], stop_after: int | None) -> None:
        self._archive, self._read, self._stop_after = archive, read, stop_after

    @property
    def entry_count(self) -> int:
        return int(self._archive.entry_count)

    def _get_entry_by_id(self, i: int) -> Any:
        if self._stop_after is not None and len(self._read) >= self._stop_after:
            raise _CutError(f"cut before entry {i}")
        self._read.append(i)
        return self._archive._get_entry_by_id(i)


class _Reader:
    """A ``zim_reader`` that runs the real walk over a real ZIM and notes which entries were read.
    ``stop[name] = n``: that file's read is cut when it asks for entry number n + 1 of this run."""

    def __init__(self, **stop: int) -> None:
        self.read: dict[str, list[int]] = {}
        self.stop = {name.replace("_", "."): n for name, n in stop.items()}

    def __call__(
        self,
        path: Path,
        builder: ProfileBuilder,
        *,
        documents: bool,
        prefix: str = "",
        pacer: ReadPacer = UNPACED,
        resume: Callable[[int], int] | None = None,
        saved: Callable[[int], None] | None = None,
    ) -> int:
        from libzim.reader import Archive

        archive = _CountingArchive(
            Archive(path), self.read.setdefault(path.name, []), self.stop.get(path.name)
        )
        return integrity._zim_entries(  # pyright: ignore[reportPrivateUsage]
            archive,
            builder,
            documents=documents,
            prefix=prefix,
            pacer=pacer,
            path=path,
            resume=resume,
            saved=saved,
        )


GID = "g1"
PARTS = 3


def _refinery(root: Path, gid: str = GID) -> Path:
    """A sealed Refinery generation of source ``w``: three part files of 40 documents, with the
    columns a profile reads."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    d = root / "w" / gid
    d.mkdir(parents=True)
    for part in range(PARTS):
        rows = range(part * 40, part * 40 + 40)
        table = pa.table(  # pyright: ignore[reportUnknownMemberType]
            {
                "native_id": [f"A/doc{i}" if i % 3 else f"doc{i}" for i in rows],
                "title": [f"Doc {i}" for i in rows],
                "content_hash": [hashlib.sha256(b"%d" % i).hexdigest() for i in rows],
                "text": ["text " * (i + 1) for i in rows],
            }
        )
        pq.write_table(table, d / f"part-{part:05d}.parquet")  # pyright: ignore[reportUnknownMemberType]
    (d / "_ids.parquet").write_bytes(b"ids")
    (d / "_SUCCESS.json").write_text(json.dumps({"format": 1, "docs": 40 * PARTS, "parts": PARTS}))
    return d


def _generation(*, sha256: str = "s") -> Generation:
    arts = tuple(
        Artifact("w", GID, PurePosixPath(f"upstream/{name}"), 1, sha256, "b") for name in ("a.zim", "b.zim")
    )
    return Generation("w", GID, GenerationStatus.SEALED, arts)


def _two_zims(tmp_path: Path) -> Path:
    """The generation directory of a release of two ZIM files, each of more than 90 entries."""
    gen_dir = tmp_path / "vault" / GID
    a = [(f"A/a{i}", "text/html", f"<p>a {i}</p><script>x()</script>") for i in range(120)]
    b = [(f"A/b{i}", "text/html", f"<p>b {i}</p>") for i in range(90)]
    _zim(gen_dir / "upstream" / "a.zim", [*a, ("I/a.png", "image/png", "png")])
    _zim(gen_dir / "upstream" / "b.zim", [*b, ("X/run.exe", "application/x-msdownload", "MZ fake")])
    return gen_dir


@pytest.fixture
def often(monkeypatch: pytest.MonkeyPatch) -> float:
    """Checkpoints as often as can be: the clock is looked at every 16 ZIM entries, a save is due at
    every look (pass the value as ``checkpoint_seconds``), and a slow save does not put the next off."""
    monkeypatch.setattr(integrity, "CHECKPOINT_STRIDE", 16)
    monkeypatch.setattr(integrity, "CHECKPOINT_SLOWDOWN", 0.0)
    return 1e-6


def _part(ckpt: Path, unit: int) -> dict[str, Any]:
    doc = json.loads(gzip.decompress((ckpt / f"unit-{unit:03d}.part.json.gz").read_bytes()))
    assert isinstance(doc, dict)
    return doc  # pyright: ignore[reportUnknownVariableType]


def _entry_count(zim: Path) -> int:
    from libzim.reader import Archive

    return int(Archive(zim).entry_count)


def _no_refinery(*args: object, **kwargs: object) -> int:
    raise AssertionError("the Refinery was read again")


@pytest.mark.parametrize("refined", [True, False], ids=["refinery-and-zims", "zims-only"])
def test_a_checkpointed_profile_equals_the_uninterrupted_profile(tmp_path: Path, refined: bool) -> None:
    gen_dir = _two_zims(tmp_path)
    root = tmp_path / "refinery" if refined else None
    if root is not None:
        _refinery(root)
    profiler = Profiler(root, sample_rate=1.0)
    plain = profiler(make_spec("w"), _generation(), gen_dir)
    assert plain.doc_count == (120 if refined else plain.doc_count) > 0
    assert plain.script_tags >= 120
    ckpt = tmp_path / "work" / f"{GID}.ckpt"
    saved = profiler(make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt)
    assert saved.to_json() == plain.to_json()
    assert sorted(f.name for f in ckpt.iterdir()) == [
        "meta.json",
        "unit-000.done.json.gz",
        "unit-001.done.json.gz",
        "unit-002.done.json.gz",
    ]
    # ... and again, now from the finished units alone: nothing is read
    reader = _Reader()
    again = Profiler(root, sample_rate=1.0, zim_reader=reader)
    assert again(make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt).to_json() == plain.to_json()
    assert reader.read == {}


def test_a_profile_interrupted_in_a_zim_resumes_at_its_entry(tmp_path: Path, often: float) -> None:
    gen_dir = _two_zims(tmp_path)
    _refinery(tmp_path / "refinery")
    plain = Profiler(tmp_path / "refinery", sample_rate=1.0)(make_spec("w"), _generation(), gen_dir)
    ckpt = tmp_path / "work" / f"{GID}.ckpt"
    first = _Reader(a_zim=100)
    with pytest.raises(_CutError):
        Profiler(tmp_path / "refinery", sample_rate=1.0, zim_reader=first)(
            make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt, checkpoint_seconds=often
        )
    assert first.read == {"a.zim": list(range(100))}
    part = _part(ckpt, 1)
    assert part["cursor"] == 96, "the last look at the clock before the cut: entries 0..95 are saved"
    assert part["total"] > 100
    second = _Reader()
    resumed = Profiler(tmp_path / "refinery", sample_rate=1.0, zim_reader=second)(
        make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt, checkpoint_seconds=often
    )
    assert second.read["a.zim"] == list(range(96, part["total"])), "only what was not saved is read"
    assert resumed.to_json() == plain.to_json(), "entries 96..99 were read twice and counted once"
    assert not (ckpt / "unit-001.part.json.gz").exists(), "a finished unit keeps only its done file"


@pytest.fixture
def journal() -> Iterator[io.StringIO]:
    """What the profile child writes to the journal: the JSON handler on the ``wsp`` logger tree, as
    ``wsp_orchestrator.child`` sets it up (nothing else of a child's logging reaches the journal)."""
    logger = logging.getLogger("wsp")
    handlers, level, propagate = list(logger.handlers), logger.level, logger.propagate
    out = io.StringIO()
    configure_logging("INFO", stream=out)
    yield out
    logger.handlers[:] = handlers
    logger.setLevel(level)
    logger.propagate = propagate


def _messages(journal: io.StringIO) -> list[str]:
    return [str(json.loads(line).get("msg", "")) for line in journal.getvalue().splitlines()]


def test_the_journal_says_where_a_profile_starts_or_resumes(
    tmp_path: Path, often: float, journal: io.StringIO
) -> None:
    # The module logged as ``wsp_cli.integrity``, outside the ``wsp`` tree: a resumed profile wrote
    # nothing the journal kept, and the resume had to be proved from the files (2026-11-13).
    gen_dir = _two_zims(tmp_path)
    _refinery(tmp_path / "refinery")
    ckpt = tmp_path / "work" / f"{GID}.ckpt"

    def run(reader: _Reader) -> None:
        Profiler(tmp_path / "refinery", sample_rate=1.0, zim_reader=reader)(
            make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt, checkpoint_seconds=often
        )

    with pytest.raises(_CutError):
        run(_Reader(a_zim=100))
    first = _messages(journal)
    assert [m for m in first if m.startswith("profile: ")] == [
        f"profile: starting fresh: w, 3 units ({ckpt})"
    ]
    journal.seek(0)
    journal.truncate()
    total = _part(ckpt, 1)["total"]
    run(_Reader())
    second = [m for m in _messages(journal) if m.startswith("profile: ")]
    assert second == [f"profile: resuming w from unit 1 of 0..2 (cursor 96 of {total})"]
    journal.seek(0)
    journal.truncate()
    run(_Reader())  # everything was saved as done
    third = [m for m in _messages(journal) if m.startswith("profile: ")]
    assert third == ["profile: resuming w: all 3 units were done, nothing is read again"]


def test_a_profile_interrupted_between_files_skips_the_finished_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen_dir = _two_zims(tmp_path)
    _refinery(tmp_path / "refinery")
    plain = Profiler(tmp_path / "refinery", sample_rate=1.0)(make_spec("w"), _generation(), gen_dir)
    ckpt = tmp_path / "work" / f"{GID}.ckpt"
    with pytest.raises(_CutError):
        Profiler(tmp_path / "refinery", sample_rate=1.0, zim_reader=_Reader(b_zim=0))(
            make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt, checkpoint_seconds=0.0
        )
    assert not list(ckpt.glob("*.part.json.gz")), "0 seconds: a save only when a unit ends"
    monkeypatch.setattr(integrity, "refinery_documents", _no_refinery)
    second = _Reader()
    resumed = Profiler(tmp_path / "refinery", sample_rate=1.0, zim_reader=second)(
        make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt, checkpoint_seconds=0.0
    )
    assert list(second.read) == ["b.zim"], "the first ZIM was done: it is not opened"
    assert second.read["b.zim"][0] == 0
    assert resumed.to_json() == plain.to_json()


def test_a_profile_interrupted_in_the_refinery_resumes_at_the_next_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, often: float
) -> None:
    gen_dir = _two_zims(tmp_path)
    _refinery(tmp_path / "refinery")
    profiler = Profiler(tmp_path / "refinery", sample_rate=1.0)
    plain = profiler(make_spec("w"), _generation(), gen_dir)
    ckpt = tmp_path / "work" / f"{GID}.ckpt"
    real = integrity._profile_part  # pyright: ignore[reportPrivateUsage]
    opened: list[str] = []
    cut_at = ["part-00002.parquet"]

    def profile_part(part: Path, *args: Any) -> int:
        if part.name in cut_at:
            raise _CutError(part.name)
        opened.append(part.name)
        return real(part, *args)

    monkeypatch.setattr(integrity, "_profile_part", profile_part)
    with pytest.raises(_CutError):
        profiler(make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt, checkpoint_seconds=often)
    assert opened == ["part-00000.parquet", "part-00001.parquet"]
    part = _part(ckpt, 0)
    assert (part["cursor"], part["total"]) == (2, PARTS)
    assert part["profile"]["doc_count"] == 80
    opened.clear()
    cut_at.clear()
    resumed = profiler(make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt, checkpoint_seconds=often)
    assert opened == ["part-00002.parquet"]
    assert resumed.to_json() == plain.to_json()


def _meta(ckpt: Path) -> dict[str, Any]:
    doc = json.loads((ckpt / "meta.json").read_text())
    assert isinstance(doc, dict)
    return doc  # pyright: ignore[reportUnknownVariableType]


def test_a_checkpoint_of_another_fingerprint_is_discarded(tmp_path: Path) -> None:
    gen_dir = _two_zims(tmp_path)
    refined = _refinery(tmp_path / "refinery")
    ckpt = tmp_path / "work" / f"{GID}.ckpt"

    def run(sample_rate: float = 1.0, sha256: str = "s") -> tuple[_Reader, str]:
        reader = _Reader()
        Profiler(tmp_path / "refinery", sample_rate=sample_rate, zim_reader=reader)(
            make_spec("w"), _generation(sha256=sha256), gen_dir, checkpoint_dir=ckpt
        )
        return reader, _meta(ckpt)["fingerprint"]

    _, fingerprint = run()
    assert _meta(ckpt)["format"] == integrity.CHECKPOINT_FORMAT
    seen = {fingerprint}
    marker = ckpt / "unit-099.done.json.gz"  # stands for the old checkpoint's files
    marker.write_bytes(b"old")
    reader, same = run()
    assert (same, reader.read, marker.exists()) == (fingerprint, {}, True), (
        "the same input: kept, nothing read"
    )

    def refined_again(name: str) -> Callable[[], None]:
        """The same bytes written anew (a refine-again of unchanged documents): only the time shows."""

        def touch() -> None:
            st = (refined / name).stat()
            os.utime(refined / name, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))

        return touch

    seal = (refined / "_SUCCESS.json").read_bytes()
    changes: list[tuple[str, Callable[[], None], dict[str, Any]]] = [
        ("another sample rate", lambda: None, {"sample_rate": 0.5}),
        ("another file behind the same name", lambda: None, {"sample_rate": 0.5, "sha256": "t"}),
        ("a part refined again", refined_again("part-00001.parquet"), {"sample_rate": 0.5, "sha256": "t"}),
        ("the id index written again", refined_again("_ids.parquet"), {"sample_rate": 0.5, "sha256": "t"}),
    ]
    for what, change, how in changes:
        marker.write_bytes(b"old")
        change()
        reader, fingerprint = run(**how)
        assert fingerprint not in seen, what
        seen.add(fingerprint)
        assert not marker.exists(), f"{what}: the old checkpoint's files are gone"
        assert reader.read["a.zim"][0] == 0, f"{what}: read from the start"
        assert reader.read["b.zim"][0] == 0, what
    assert (refined / "_SUCCESS.json").read_bytes() == seal, "the seal alone would not have shown it"
    # a checkpoint whose meta.json is gone or garbled is of no use either
    for damage in (lambda: (ckpt / "meta.json").unlink(), lambda: (ckpt / "meta.json").write_bytes(b"{")):
        marker.write_bytes(b"old")
        damage()
        reader, fingerprint = run(sample_rate=0.5, sha256="t")
        assert fingerprint in seen
        assert not marker.exists()
        assert reader.read["a.zim"][0] == 0


def test_a_checkpoint_of_another_fingerprint_is_put_aside_and_taken_back(tmp_path: Path) -> None:
    """The input may only have looked different for one run (a Refinery not mounted yet after a
    restart): days of work are not deleted for that."""
    gen_dir = _two_zims(tmp_path)
    _refinery(tmp_path / "refinery")
    ckpt = tmp_path / "work" / f"{GID}.ckpt"
    aside = tmp_path / "work" / f"{GID}.ckpt.aside"

    def run(refinery: Path | None) -> tuple[dict[str, list[int]], str]:
        reader = _Reader()
        Profiler(refinery, sample_rate=1.0, zim_reader=reader)(
            make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt
        )
        return reader.read, _meta(ckpt)["fingerprint"]

    read, with_refinery = run(tmp_path / "refinery")
    assert sorted(read) == ["a.zim", "b.zim"]
    assert not aside.exists()
    read, without = run(None)  # the Refinery is not seen this time
    assert without != with_refinery
    assert sorted(read) == ["a.zim", "b.zim"], "another input: its own checkpoint, from the start"
    assert _meta(aside)["fingerprint"] == with_refinery
    read, back = run(tmp_path / "refinery")
    assert (read, back) == ({}, with_refinery), "the first checkpoint is taken back: nothing is read"
    assert _meta(aside)["fingerprint"] == without
    assert run(None) == ({}, without)
    assert sorted(d.name for d in (tmp_path / "work").iterdir()) == [f"{GID}.ckpt", f"{GID}.ckpt.aside"]
    # with the one in use gone, the one put aside is taken if it fits
    shutil.rmtree(ckpt)
    assert run(tmp_path / "refinery") == ({}, with_refinery)
    assert not aside.exists()


def test_the_fingerprint_names_the_refinery_files_and_the_zims(tmp_path: Path) -> None:
    refined = _refinery(tmp_path / "refinery")
    base = checkpoint_fingerprint("w", _generation(), refined, 1.0)
    assert base == checkpoint_fingerprint("w", _generation(), refined, 1.0)
    assert len(base) == 64
    assert base != checkpoint_fingerprint("w", _generation(), None, 1.0)
    assert base != checkpoint_fingerprint("v", _generation(), refined, 1.0)
    assert base != checkpoint_fingerprint("w", _generation(sha256="t"), refined, 1.0)
    (refined / "_ids.parquet").unlink()
    assert base != checkpoint_fingerprint("w", _generation(), refined, 1.0), "no id index is a value too"
    (refined / "_SUCCESS.json").unlink()
    with pytest.raises(OSError, match="_SUCCESS"):
        checkpoint_fingerprint("w", _generation(), refined, 1.0)


def test_a_torn_or_foreign_part_file_restarts_only_that_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, often: float
) -> None:
    gen_dir = _two_zims(tmp_path)
    _refinery(tmp_path / "refinery")
    plain = Profiler(tmp_path / "refinery", sample_rate=1.0)(make_spec("w"), _generation(), gen_dir)
    ckpt = tmp_path / "work" / f"{GID}.ckpt"

    def run(reader: _Reader) -> ContentProfile:
        return Profiler(tmp_path / "refinery", sample_rate=1.0, zim_reader=reader)(
            make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt, checkpoint_seconds=often
        )

    with pytest.raises(_CutError):
        run(_Reader(a_zim=100))
    path = ckpt / "unit-001.part.json.gz"
    raw = path.read_bytes()
    good = _part(ckpt, 1)
    assert good["cursor"] == 96
    total = good["total"]
    run(_Reader())  # to the end: every unit is done, and each case below undoes the first ZIM only
    monkeypatch.setattr(integrity, "refinery_documents", _no_refinery)
    bad: dict[str, bytes] = {
        "garbage": b"\x1f\x8b not gzip at all",
        "cut short": raw[:40],
        "not an object": gzip.compress(b"[1, 2]"),
        "no profile": gzip.compress(json.dumps({"cursor": 96, "total": total}).encode()),
        "another total": gzip.compress(json.dumps({**good, "total": total + 1}).encode()),
        "cursor past the end": gzip.compress(json.dumps({**good, "cursor": total + 1}).encode()),
        "negative cursor": gzip.compress(json.dumps({**good, "cursor": -1}).encode()),
        "cursor not a number": gzip.compress(json.dumps({**good, "cursor": "96"}).encode()),
        "cursor a fraction": gzip.compress(json.dumps({**good, "cursor": 96.5}).encode()),
        "cursor a truth value": gzip.compress(json.dumps({**good, "cursor": True}).encode()),
        "profile of another format": gzip.compress(
            json.dumps({**good, "profile": {**good["profile"], "format": "other/9"}}).encode()
        ),
    }
    for what, data in bad.items():
        (ckpt / "unit-001.done.json.gz").unlink()
        path.write_bytes(data)
        reader = _Reader()
        result = run(reader)
        assert list(reader.read) == ["a.zim"], f"{what}: the Refinery and the other ZIM stay done"
        assert reader.read["a.zim"][0] == 0, f"{what}: this unit starts again"
        assert result.to_json() == plain.to_json(), what
    # the check of the test itself: the good file is used
    (ckpt / "unit-001.done.json.gz").unlink()
    path.write_bytes(gzip.compress(json.dumps(good).encode()))
    reader = _Reader()
    assert run(reader).to_json() == plain.to_json()
    assert reader.read["a.zim"][0] == 96


def test_a_finished_unit_that_cannot_be_read_is_done_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never an error: a failed profile holds the release for the owner (guard.promotion_report)."""
    gen_dir = _two_zims(tmp_path)
    _refinery(tmp_path / "refinery")
    plain = Profiler(tmp_path / "refinery", sample_rate=1.0)(make_spec("w"), _generation(), gen_dir)
    ckpt = tmp_path / "work" / f"{GID}.ckpt"

    def run(reader: _Reader) -> ContentProfile:
        return Profiler(tmp_path / "refinery", sample_rate=1.0, zim_reader=reader)(
            make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt
        )

    run(_Reader())
    done = ckpt / "unit-002.done.json.gz"
    whole = done.read_bytes()
    monkeypatch.setattr(integrity, "refinery_documents", _no_refinery)
    for what, data in {
        "garbage": b"garbage",
        "cut short": whole[: len(whole) // 2],
        "a list": gzip.compress(b"[]"),
        "no counts": gzip.compress(json.dumps({"format": "whisipedia-content-profile/1"}).encode()),
        "a sample of the wrong shape": gzip.compress(
            json.dumps({**json.loads(gzip.decompress(whole)), "sample": {"k": []}}).encode()
        ),
    }.items():
        done.write_bytes(data)
        reader = _Reader()
        assert run(reader).to_json() == plain.to_json(), what
        assert list(reader.read) == ["b.zim"], what
        assert done.read_bytes() == whole, f"{what}: written again, the same bytes"


def test_a_checkpoint_that_cannot_be_written_never_fails_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, often: float
) -> None:
    gen_dir = _two_zims(tmp_path)
    _refinery(tmp_path / "refinery")
    profiler = Profiler(tmp_path / "refinery", sample_rate=1.0)
    plain = profiler(make_spec("w"), _generation(), gen_dir)
    real = integrity._write_atomic  # pyright: ignore[reportPrivateUsage]
    refused: list[str] = []
    allow_meta = [True]

    def full_disk(path: Path, data: bytes) -> None:
        if path.name == "meta.json" and allow_meta[0]:
            return real(path, data)
        refused.append(path.name)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(integrity, "_write_atomic", full_disk)
    # 1. every save of a unit fails: the profile is made all the same
    ckpt = tmp_path / "work" / "one.ckpt"
    got = profiler(make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt, checkpoint_seconds=often)
    assert got.to_json() == plain.to_json()
    assert {"unit-000.part.json.gz", "unit-000.done.json.gz", "unit-001.part.json.gz"} <= set(refused)
    assert [f.name for f in ckpt.iterdir()] == ["meta.json"]
    # 2. not even meta.json can be written: no checkpoint this run
    allow_meta[0] = False
    refused.clear()
    ckpt = tmp_path / "work" / "two.ckpt"
    got = profiler(make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt, checkpoint_seconds=often)
    assert got.to_json() == plain.to_json()
    assert refused == ["meta.json"], "after that nothing is tried"
    # 3. the directory cannot exist at all
    monkeypatch.setattr(integrity, "_write_atomic", real)
    (tmp_path / "work" / "file").write_bytes(b"")
    got = profiler(
        make_spec("w"), _generation(), gen_dir, checkpoint_dir=tmp_path / "work" / "file" / "x.ckpt"
    )
    assert got.to_json() == plain.to_json()
    # 4. a meta.json that cannot be READ (not: is garbled) turns the checkpoint off and keeps its files
    ckpt = tmp_path / "work" / "four.ckpt"
    profiler(make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt)
    (ckpt / "meta.json").unlink()
    (ckpt / "meta.json").mkdir()  # reading it raises IsADirectoryError
    before = sorted(f.name for f in ckpt.iterdir())
    got = profiler(make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt)
    assert got.to_json() == plain.to_json()
    assert sorted(f.name for f in ckpt.iterdir()) == before


def test_checkpoint_files_are_replaced_atomically(tmp_path: Path, often: float) -> None:
    gen_dir = _two_zims(tmp_path)
    ckpt = tmp_path / "work" / f"{GID}.ckpt"

    def run(reader: _Reader) -> ContentProfile:
        return Profiler(None, sample_rate=1.0, zim_reader=reader)(
            make_spec("w"), _generation(), gen_dir, checkpoint_dir=ckpt, checkpoint_seconds=often
        )

    with pytest.raises(_CutError):
        run(_Reader(b_zim=50))
    names = sorted(f.name for f in ckpt.iterdir())
    assert names == ["meta.json", "unit-000.done.json.gz", "unit-001.done.json.gz", "unit-002.part.json.gz"]
    assert set(_meta(ckpt)) == {"format", "fingerprint", "created"}
    assert _part(ckpt, 2)["cursor"] == 48
    # a write cut by a kill leaves a .partial of another process: removed at the next open, never read
    leftover = ckpt / "unit-002.part.json.gz.4242.partial"
    leftover.write_bytes(b"half a file")
    reader = _Reader()
    run(reader)
    assert reader.read == {"b.zim": list(range(48, _entry_count(gen_dir / "upstream" / "b.zim")))}
    assert not leftover.exists()
    assert not list(ckpt.glob("*.partial"))


def test_a_slow_save_puts_the_next_one_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sample of a big unit is written whole at each save: it must not become the work."""
    now = [0.0]
    ckpt = ProfileCheckpoint.open(tmp_path / "c.ckpt", "f" * 64, every=300.0, clock=lambda: now[0])
    assert ckpt.root == tmp_path / "c.ckpt"
    assert not ckpt.due()
    now[0] = 300.0
    assert ckpt.due()
    real = integrity._write_atomic  # pyright: ignore[reportPrivateUsage]

    def slow(path: Path, data: bytes) -> None:
        now[0] += 60.0
        real(path, data)

    monkeypatch.setattr(integrity, "_write_atomic", slow)
    builder = ProfileBuilder(sample_rate=1.0)
    builder.add("A/x", namespace="A", size=3, title="X", content=b"<p>")
    ckpt.save_partial(1, 5, 10, builder)
    assert now[0] == 360.0
    now[0] = 360.0 + 20 * 60.0 - 1
    assert not ckpt.due(), "20 x the 60 seconds the save took, not the 300 of the setting"
    now[0] += 1
    assert ckpt.due()
    found = ckpt.partial(1, 10)
    assert found is not None
    assert (found[0], found[1].build()) == (5, builder.build())
    assert ckpt.partial(1, 11) is None
    assert ckpt.finish(1, builder) == builder.build()
    assert ckpt.partial(1, 10) is None, "done: the part is gone"
    assert ckpt.done(1) == builder.build()
    assert not ProfileCheckpoint.open(tmp_path / "d.ckpt", "f" * 64, every=0.0, clock=lambda: 1e9).due()


class _Entry:
    def __init__(self, path: str, mimetype: str, content: bytes, *, redirect: bool = False) -> None:
        self.path, self.title, self.is_redirect = path, path.upper(), redirect
        self.mimetype, self.content = mimetype, content

    def get_item(self) -> _Entry:
        return self


class _FixedArchive:
    entries = (
        _Entry("A/one", "text/html; charset=utf-8", b"<p>one</p><SCRIPT src=x></script><script>y()</script>"),
        _Entry("A/gone", "text/html", b"", redirect=True),
        _Entry("two", "text/html", b"<p>\x00binary</p>"),
        _Entry("I/pic.png", "image/png", b"\x89PNG" + b"\x00" * 5000),
        _Entry("a-namespace-name-that-is-longer-than-forty-characters/x", "text/plain", b"#!/bin/sh"),
        _Entry("X/run", "application/x-msdownload", b"MZ\x90"),
        _Entry("A/empty", "text/html", b""),
    )
    entry_count = len(entries)

    def _get_entry_by_id(self, i: int) -> _Entry:
        return self.entries[i]


PINNED = {
    "whisipedia-profile-checkpoint/1": "629614d0e2468a191695f37b9ab77d65b2af2e8d7757a9fd1791249ac4034274",
}
"""CHECKPOINT_FORMAT -> the digest of the profile of ``_FixedArchive``. A saved half of a profile is
merged with a half made by the code of the day: the two must count alike."""


def test_the_profile_of_a_fixed_input_is_pinned(tmp_path: Path) -> None:
    digests: list[str] = []
    for documents, rate, prefix in ((True, 1.0, ""), (False, 1.0, "w/"), (True, 0.5, "w/")):
        builder = ProfileBuilder(sample_rate=rate)
        integrity._zim_entries(  # pyright: ignore[reportPrivateUsage]
            _FixedArchive(),
            builder,
            documents=documents,
            prefix=prefix,
            pacer=UNPACED,
            path=tmp_path / "no.zim",
        )
        for i, size in enumerate((0, 1, 1023, 1024, 5_000_000)):
            key = f"B/sized{i}"
            builder.add(key, namespace=integrity._namespace(key), size=size, title=key, content_hash="c" * 16)  # pyright: ignore[reportPrivateUsage]
        digests.append(json.dumps(builder.build().to_json(), sort_keys=True))
    digest = hashlib.sha256("\n".join(digests).encode()).hexdigest()
    assert PINNED.get(integrity.CHECKPOINT_FORMAT) == digest, (
        "what a profile counts has changed: bump CHECKPOINT_FORMAT in wsp_cli/integrity.py (the "
        "checkpoints on disk were made by the old rule and must not be continued), then pin the new "
        f"digest here under the new name: {digest}"
    )
