"""Run logs (wsp_core.diag)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from wsp_core import diag


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_run_log_records_the_run_its_phases_and_what_the_loggers_say(tmp_path: Path) -> None:
    run = diag.start_run(tmp_path, "index build", ["index", "build"])
    with diag.phase("refine", source="wikipedia-en-zim", generation="2026-08") as info:
        info["docs"] = 7
        logging.getLogger("wsp.index.progress").info("wikipedia-en-zim: 1 sealed generation")
        logging.getLogger("wsp_vault.store").info("slow read")  # a getLogger(__name__) module
    with pytest.raises(ValueError, match="no vocabulary"), diag.phase("spell"):
        raise ValueError("no vocabulary")
    diag.end_run(run, 0)
    assert run.path.parent == tmp_path / "logs" / "index-build"
    rows = _lines(run.path)
    events = [(r.get("event"), r.get("phase")) for r in rows]
    assert events == [
        ("run-start", None),
        ("phase-start", "refine"),
        (None, None),  # the progress line
        (None, None),  # the vault's line
        ("phase-end", "refine"),
        ("phase-start", "spell"),
        ("phase-error", "spell"),
        ("run-end", None),
    ]
    assert rows[3]["logger"] == "wsp_vault.store"
    end = rows[4]
    assert (end["source"], end["generation"], end["docs"]) == ("wikipedia-en-zim", "2026-08", 7)
    assert isinstance(end["seconds"], float)
    assert isinstance(end["peak_rss_mb"], int)
    assert rows[6]["error"] == "ValueError: no vocabulary"
    assert rows[7]["exit"] == 0
    assert rows[0]["argv"] == ["index", "build"]
    # Closed: nothing more is written after the run ends.
    logging.getLogger("wsp.index.progress").info("after")
    assert len(_lines(run.path)) == 8
    assert logging.getLogger("wsp_vault").level == logging.NOTSET


def test_only_the_newest_run_logs_of_a_command_are_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diag, "KEEP", 3)
    folder = tmp_path / "logs" / "ingest"
    folder.mkdir(parents=True)
    for i in range(5):
        (folder / f"20260101T00000{i}Z-1.jsonl").write_text("{}\n")
    run = diag.start_run(tmp_path, "ingest", [])
    diag.end_run(run, 0)
    assert len(list(folder.glob("*.jsonl"))) == 3
    assert run.path in folder.glob("*.jsonl")


def test_wsp_diag_turns_run_logs_on() -> None:
    assert diag.env_level({}) is None
    assert diag.env_level({"WSP_DIAG": "0"}) is None
    assert diag.env_level({"WSP_DIAG": "1"}) == "INFO"
    assert diag.env_level({"WSP_DIAG": "debug"}) == "DEBUG"
