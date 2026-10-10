from pathlib import Path

import pytest

from wsp_core.model import Tier
from wsp_core.registry import RegistryError, load_registry

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = ROOT / "registry/schema/source.schema.json"


def test_repository_registry_is_valid() -> None:
    specs = load_registry(ROOT / "registry/sources.yaml", SCHEMA)
    by_id = {s.id: s for s in specs}
    assert by_id["wikidata"].tier is Tier.T1
    assert by_id["wikipedia-en-zim"].adapter == "kiwix-opds"


def test_active_source_without_legal_basis_is_rejected(tmp_path: Path) -> None:
    reg = tmp_path / "r.yaml"
    reg.write_text(
        "schema_version: 1\nsources:\n  - {id: x, name: X, domain: [a], tier: T1,"
        " status: active, license: none, legal_basis: none}\n"
    )
    with pytest.raises(RegistryError):
        load_registry(reg, SCHEMA)


def test_overlay_replaces_and_adds_entries(tmp_path: Path) -> None:
    overlay = tmp_path / "registry.local.yaml"
    overlay.write_text(
        "schema_version: 1\nsources:\n"
        "  - {id: wikidata, name: Wikidata (local), domain: [knowledge-graph], tier: T1,"
        " status: paused, license: CC0-1.0, legal_basis: open-license}\n"
        "  - {id: kiwix-x, name: X, domain: [education], tier: T2,"
        " status: active, license: CC-BY-SA-4.0, legal_basis: open-license}\n"
    )
    specs = {s.id: s for s in load_registry(ROOT / "registry/sources.yaml", SCHEMA, overlay=overlay)}
    assert specs["wikidata"].name == "Wikidata (local)"
    assert "kiwix-x" in specs
    assert load_registry(ROOT / "registry/sources.yaml", SCHEMA, overlay=tmp_path / "missing.yaml")
