"""Source-integrity plumbing of the composition root: profiles, health reporting, interactive-use
signal (ADR-0009)."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from libzim.writer import Creator, Hint, Item, StringProvider

from wsp_cli.integrity import Profiler, ReadPacer, channel_summary, read_integrity_state, zim_entries
from wsp_cli.monitor import LibraryMonitor
from wsp_core.activity import ActivityMiddleware, recently_active, seconds_since
from wsp_core.model import Artifact, Generation, GenerationStatus
from wsp_orchestrator.integrity import ProfileBuilder, compare_profiles
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
