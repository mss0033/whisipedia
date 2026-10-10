import csv
import io
import re
from datetime import UTC, datetime
from pathlib import Path

from conftest import SealFn
from vaultkit import GEN, SOURCE

from wsp_core.model import SourceSpec, SourceStatus, Tier, UpdateStrategy
from wsp_vault.docs import CSV_COLUMNS, human_size, write_library_docs
from wsp_vault.vault import FileVault

WHEN = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

SPECS = [
    SourceSpec(
        SOURCE,
        "Test <Source> & Co",
        Tier.T1,
        SourceStatus.ACTIVE,
        "CC-BY-SA-4.0",
        "open-license",
        ("reference",),
        cadence="P1M",
        strategy=UpdateStrategy.SNAPSHOT_RETENTION,
        raw={"publisher": "Example Publisher"},
    ),
    SourceSpec("planned", "Planned", Tier.T2, SourceStatus.CANDIDATE, "CC0-1.0", "open-license", ("books",)),
    SourceSpec("nope", "Excluded", Tier.EXCLUDED, SourceStatus.EXCLUDED, "none", "none", ("misc",)),
]


def test_catalog_and_readmes(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    seal()
    written = write_library_docs(vault, SPECS, generated=WHEN)
    root = tmp_path / "vault"
    assert {p.relative_to(root).as_posix() for p in written} == {
        "README.txt",
        "CATALOG.html",
        "CATALOG.csv",
        f"library/reference/{SOURCE}/README.txt",
    }

    rows = list(csv.DictReader(io.StringIO((root / "CATALOG.csv").read_text())))
    assert tuple(rows[0]) == CSV_COLUMNS
    assert [r["source_id"] for r in rows] == ["planned", SOURCE]
    mine = rows[1]
    assert (mine["current_generation"], mine["state"], mine["generations"]) == (GEN, "ok", "1")
    assert int(mine["current_bytes"]) > 0
    assert rows[0]["state"] == "not yet archived"

    page = (root / "CATALOG.html").read_text()
    assert page.startswith('<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">')
    assert "Test &lt;Source&gt; &amp; Co" in page
    assert not re.search(r"<script|<style|<link|https?://|src=", page, re.IGNORECASE)

    readme = (root / "library/reference" / SOURCE / "README.txt").read_text()
    assert "Example Publisher" in readme
    assert "Kiwix" in readme  # .zim
    assert ".json" in readme
    assert f"Current generation: {GEN} (ok)" in readme
    top = (root / "README.txt").read_text()
    assert "Sources archived: 1 of 2" in top


def test_degraded_state_is_shown(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    seal()
    vault.mark_degraded(SOURCE, GEN, "test", ["upstream/beta.txt"])
    write_library_docs(vault, SPECS, generated=WHEN)
    assert ",DEGRADED" in (tmp_path / "vault" / "CATALOG.csv").read_text()


def test_unknown_vault_source_still_catalogued(vault: FileVault, seal: SealFn, tmp_path: Path) -> None:
    seal()
    write_library_docs(vault, [], generated=WHEN)
    assert SOURCE in (tmp_path / "vault" / "CATALOG.csv").read_text()


def test_human_size() -> None:
    assert human_size(999) == "999 B"
    assert human_size(1_500_000) == "1.5 MB"
    assert human_size(127_400_000_000_000_000) == "127400.0 TB"
