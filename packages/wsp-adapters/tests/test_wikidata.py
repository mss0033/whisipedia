from __future__ import annotations

import bz2
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from adapters_testkit import dumps_listing, make_ctx, make_spec

from wsp_adapters import available_adapters, wikimedia
from wsp_adapters._common import IncompleteGenerationError
from wsp_adapters.wikidata import LOOKUP_NAME, EntityLookup, WikidataAdapter, entity_doc
from wsp_adapters.wikimedia import DUMPS_BASE, EntityFormatError, entity_lines, parse_entity, strip_claims
from wsp_core.ports import Ctx, SourceAdapter
from wsp_core.testing.adapter_contract import FakeFetcher, SourceAdapterContract

BASE = f"{DUMPS_BASE}wikidatawiki/entities/"
NAME = "wikidata-20261005-all.json.bz2"
SPEC = make_spec(
    "wikidata",
    {"adapter": "wikidata", "endpoints": [BASE], "languages": ["en", "de"], "workers": 1},
    license_="CC0-1.0",
)


def _labels(**values: str) -> dict[str, Any]:
    return {lang: {"language": lang, "value": v} for lang, v in values.items()}


ENTITIES: list[dict[str, Any]] = [
    {
        "type": "item",
        "id": "Q42",
        "labels": _labels(en="Douglas Adams", de="Douglas Adams", fr="Douglas Adams"),
        "descriptions": _labels(en="English writer and humorist", de="britischer Schriftsteller"),
        "aliases": {"en": [{"language": "en", "value": "Douglas Noël Adams"}]},
        "sitelinks": {
            "enwiki": {"site": "enwiki", "title": "Douglas Adams"},
            "dewiki": {"site": "dewiki", "title": "Douglas Adams"},
            "frwiki": {"site": "frwiki", "title": "Douglas Adams"},
        },
        "lastrevid": 123,
        "modified": "2026-10-01T12:00:00Z",
    },
    {"type": "item", "id": "Q64", "labels": _labels(de="Berlin"), "sitelinks": {}},
    {"type": "property", "id": "P31", "labels": _labels(en="instance of")},
    {"type": "item", "id": "Q99999", "labels": {}},
]
DUMP = bz2.compress(("[\n" + ",\n".join(json.dumps(e) for e in ENTITIES) + "\n]\n").encode())


def routes() -> dict[str, bytes]:
    run = f"{BASE}20261005/"
    return {
        BASE: dumps_listing(["20261005"]),
        f"{run}wikidata-20261005-sha1sums.txt": f"{hashlib.sha1(DUMP).hexdigest()}  {NAME}\n".encode(),
        f"{run}wikidata-20261005-md5sums.txt": f"{hashlib.md5(DUMP).hexdigest()}  {NAME}\n".encode(),
        f"{BASE}20261005/": f'<a href="{NAME}">{NAME}</a>   06-Oct-2026 10:32   {len(DUMP)}\n'.encode(),
        f"{BASE}20261005/{NAME}": DUMP,
    }


@pytest.fixture
def generation_dir(tmp_path: Path) -> Path:
    up = tmp_path / "gen" / "upstream"
    up.mkdir(parents=True)
    (up / NAME).write_bytes(DUMP)
    return tmp_path / "gen"


class TestWikidataContract(SourceAdapterContract):
    @pytest.fixture
    def adapter(self) -> SourceAdapter:
        return WikidataAdapter()

    @pytest.fixture
    def ctx(self, tmp_path: Path) -> Ctx:
        return make_ctx(SPEC, FakeFetcher(routes()), tmp_path / "work")

    @pytest.fixture
    def generation_dir(self, generation_dir: Path) -> Path:
        return generation_dir


def test_documents_and_entity_lookup(generation_dir: Path, tmp_path: Path) -> None:
    assert "wikidata" in available_adapters()
    ctx = make_ctx(SPEC, FakeFetcher(), tmp_path / "work")
    docs = {d.native_id: d for d in WikidataAdapter().documents(ctx, generation_dir)}
    assert list(docs) == ["Q42", "Q64", "P31", "Q99999"]
    adams = docs["Q42"]
    assert adams.title == "Douglas Adams"
    assert adams.text.splitlines() == [
        "Douglas Adams",
        "[en] Douglas Adams - English writer and humorist (also: Douglas Noël Adams)",
        "[de] Douglas Adams - britischer Schriftsteller",
        "Sitelinks: 3 wiki pages",
    ]
    assert adams.aliases == ("Douglas Noël Adams",)
    assert adams.links == (
        "https://en.wikipedia.org/wiki/Douglas_Adams",
        "https://de.wikipedia.org/wiki/Douglas_Adams",
    )
    assert adams.entities == ("Q42",)
    assert docs["Q64"].title == "Berlin"
    assert docs["Q64"].lang == "de"
    assert docs["P31"].url_native == "https://www.wikidata.org/wiki/Property:P31"
    assert docs["Q99999"].title == "Q99999"

    lookup = EntityLookup(tmp_path / "work" / LOOKUP_NAME)
    row = lookup.get("Q42")
    assert row is not None
    assert row.description == "English writer and humorist"
    assert dict(row.sitelinks) == {"enwiki": "Douglas Adams", "dewiki": "Douglas Adams"}
    assert lookup.label("Q64") == "Berlin"
    assert lookup.by_label("Douglas Adams") == ["Q42"]
    assert lookup.get("P31") is None  # items only
    lookup.close()


def test_lookup_is_published_only_after_a_full_pass(generation_dir: Path, tmp_path: Path) -> None:
    ctx = make_ctx(SPEC, FakeFetcher(), tmp_path / "work")
    stream = WikidataAdapter().documents(ctx, generation_dir)
    next(iter(stream))
    stream.close()  # type: ignore[attr-defined]
    assert not (tmp_path / "work" / LOOKUP_NAME).exists()
    assert not (tmp_path / "work" / (LOOKUP_NAME + ".partial")).exists()


def test_entity_without_id_is_skipped() -> None:
    assert entity_doc({"labels": {}}, "wikidata", "CC0-1.0", ("en",)) is None


def _line(**members: object) -> bytes:
    return json.dumps(members, ensure_ascii=False, separators=(",", ":")).encode()


FULL = {
    "type": "item",
    "id": "Q1",
    "labels": _labels(en="Universe"),
    "descriptions": _labels(en="totality of space"),
    "aliases": {},
    "claims": {
        "P31": [{"mainsnak": {"datavalue": {"value": 'a "claims": trap,"sitelinks":x'}}, "id": "Q1$1"}]
    },
    "sitelinks": {"enwiki": {"site": "enwiki", "title": "Universe"}},
    "lastrevid": 7,
    "modified": "2026-10-01T00:00:00Z",
}


def test_claims_are_cut_structurally_and_documents_are_unchanged() -> None:
    line = _line(**FULL)
    cut = strip_claims(line)
    assert b'"claims"' not in cut
    assert len(cut) < len(line)
    full = entity_doc(json.loads(line), "w", "CC0-1.0", ("en",))
    assert entity_doc(parse_entity(cut), "w", "CC0-1.0", ("en",)) == full


@pytest.mark.parametrize(
    "order",
    [
        ("type", "id", "claims", "labels", "descriptions", "aliases", "sitelinks", "lastrevid", "modified"),
        ("type", "id", "labels", "descriptions", "aliases", "claims", "lastrevid", "sitelinks", "modified"),
        ("type", "id", "labels", "descriptions", "aliases", "claims", "lastrevid", "modified"),
    ],
)
def test_other_layouts_are_left_whole(order: tuple[str, ...]) -> None:
    line = _line(**{k: FULL[k] for k in order})
    assert strip_claims(line) == line  # never cut a key that is read


def test_a_cut_that_does_not_parse_fails_loudly() -> None:
    # A ',"sitelinks":' key nested in claims (not in the schema) makes an unbalanced cut.
    line = _line(**{**FULL, "claims": {"P1": {"a": 0, "sitelinks": 1}}})
    with pytest.raises(EntityFormatError):
        parse_entity(strip_claims(line))


def test_entity_batches_are_bounded_by_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wikimedia, "ENTITY_BATCH_BYTES", 600)
    entities = [{**FULL, "id": f"Q{i}"} for i in range(1, 9)]
    dump = tmp_path / "wikidata-x-all.json.bz2"
    dump.write_bytes(bz2.compress(b"[\n" + b",\n".join(_line(**e) for e in entities) + b"\n]\n"))
    batches = list(entity_lines(dump, 1))
    assert len(batches) > 1
    assert all(sum(map(len, b)) < 600 + max(map(len, b)) for b in batches)
    assert [parse_entity(x)["id"] for b in batches for x in b] == [f"Q{i}" for i in range(1, 9)]


def test_a_generation_without_the_json_dump_raises_and_publishes_no_lookup(tmp_path: Path) -> None:
    """Audit 2026-10-11 C1: a truthy-only generation used to refine to 0 documents and commit an
    empty lookup (and the diff then deleted every Wikidata document)."""
    up = tmp_path / "2026-10-07" / "upstream"
    up.mkdir(parents=True)
    (up / "wikidata-20261007-truthy-BETA.nt.bz2").write_bytes(bz2.compress(b"<a> <b> <c> .\n"))
    work = tmp_path / "work"
    with pytest.raises(IncompleteGenerationError, match=r"all\.json\.bz2"):
        list(WikidataAdapter().documents(make_ctx(SPEC, FakeFetcher(), work), up.parent))
    assert not (work / LOOKUP_NAME).exists()
