"""Ingest of a Kiwix metalink item uses Kiwix's .sha256 the owner saved (Kiwix's host refuses robots)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wsp_cli.ingest import FilePlan, FileState, Ingestor, Target, VersionPlan
from wsp_core.model import Checksum, SourceSpec, SourceStatus, Tier


def test_a_saved_kiwix_checksum_verifies_the_zim_without_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = tmp_path / "checksums"
    saved.mkdir()
    digest = "ab" * 32
    name = "wikipedia_es_all_maxi_2026-10.zim"
    (saved / f"{name}.sha256").write_text(f"{digest}  {name}\n")
    monkeypatch.setenv("WSP_KIWIX_CHECKSUMS", str(saved))
    spec = SourceSpec(
        id="kiwix-x",
        name="x",
        tier=Tier.T1,
        status=SourceStatus.ACTIVE,
        license="CC-BY-SA-4.0",
        legal_basis="open-license",
        domain=("encyclopedia",),
        adapter="kiwix-opds",
        raw={"id": "kiwix-x", "access": {"adapter": "kiwix-opds"}},
    )
    url = f"https://download.kiwix.org/zim/wikipedia/{name}"
    target = Target("kiwix-x", "2026-10", name, (f"{url}.meta4",), "kiwix", None, None, {})
    plan = VersionPlan(
        "kiwix-x", "2026-10", ("2026-10",), [FilePlan(target, tmp_path / name, FileState.COMPLETE)]
    )
    vault = type("V", (), {"root": tmp_path})()
    Ingestor(vault, [spec], fetcher=None).resolve_checksums(plan)  # type: ignore[arg-type]
    assert plan.files[0].checksums == (Checksum("sha256", digest),)
    assert plan.files[0].note == ""
