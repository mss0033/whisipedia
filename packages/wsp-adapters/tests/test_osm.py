from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from adapters_testkit import make_ctx, make_spec

from wsp_adapters import available_adapters
from wsp_adapters.osm import PLANET_URL, OsmPlanetAdapter, place_record, version_of
from wsp_core.ports import Ctx, SourceAdapter
from wsp_core.testing.adapter_contract import FakeFetcher, SourceAdapterContract

SPEC = make_spec("osm-planet", {"adapter": "osm-planet", "languages": ["en"]}, license_="ODbL-1.0")

OSM_XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <node id="1" version="1" lat="52.5170365" lon="13.3888599">
    <tag k="name" v="Berlin"/><tag k="name:en" v="Berlin"/><tag k="place" v="city"/>
    <tag k="population" v="3769495"/><tag k="wikidata" v="Q64"/><tag k="wikipedia" v="de:Berlin"/>
  </node>
  <node id="2" version="1" lat="48.8582" lon="2.2945">
    <tag k="name" v="Tour Eiffel"/><tag k="name:en" v="Eiffel Tower"/><tag k="tourism" v="attraction"/>
    <tag k="wikidata" v="Q243"/>
  </node>
  <node id="3" version="1" lat="1" lon="1">
    <tag k="name" v="Corner shop"/><tag k="shop" v="convenience"/>
  </node>
  <node id="4" version="1" lat="1" lon="1"><tag k="highway" v="crossing"/></node>
  <node id="5" version="1" lat="1.1" lon="1.1"/>
  <way id="10" version="1"><nd ref="4"/><nd ref="5"/>
    <tag k="name" v="Central Park"/><tag k="leisure" v="park"/><tag k="wikidata" v="Q160409"/>
  </way>
  <relation id="20" version="1"><member type="way" ref="10" role="outer"/>
    <tag k="name" v="Hamlet Town"/><tag k="place" v="hamlet"/><tag k="type" v="multipolygon"/>
  </relation>
</osm>
"""
PBF_NAME = "planet-261005.osm.pbf"
PBF_BYTES = b"not really a pbf"


def routes() -> dict[str, bytes]:
    listing = "".join(
        f'<a href="{n}">{n}</a>\n' for n in ("planet-260928.osm.pbf", PBF_NAME, "planet-261012.osm.pbf")
    )
    return {
        PLANET_URL: listing.encode(),
        f"{PLANET_URL}{PBF_NAME}.md5": f"{hashlib.md5(PBF_BYTES).hexdigest()}  {PBF_NAME}\n".encode(),
        f"{PLANET_URL}planet-260928.osm.pbf.md5": f"{'0' * 32}  planet-260928.osm.pbf\n".encode(),
        f"{PLANET_URL}{PBF_NAME}": PBF_BYTES,
    }  # 261012 is still being written: no .md5 yet


@pytest.fixture
def generation_dir(tmp_path: Path) -> Path:
    up = tmp_path / "gen" / "upstream"
    up.mkdir(parents=True)
    (up / "extract.osm").write_text(OSM_XML)
    return tmp_path / "gen"


class TestOsmContract(SourceAdapterContract):
    @pytest.fixture
    def adapter(self) -> SourceAdapter:
        return OsmPlanetAdapter()

    @pytest.fixture
    def ctx(self, tmp_path: Path) -> Ctx:
        return make_ctx(SPEC, FakeFetcher(routes()), tmp_path / "work")

    @pytest.fixture
    def generation_dir(self, generation_dir: Path) -> Path:
        return generation_dir


def test_discover_newest_complete_planet(tmp_path: Path) -> None:
    assert "osm-planet" in available_adapters()
    (release,) = OsmPlanetAdapter().discover(make_ctx(SPEC, FakeFetcher(routes()), tmp_path))
    assert release.version == "2026-10-05"
    assert release.files[0].checksums[0].hexdigest == hashlib.md5(PBF_BYTES).hexdigest()
    assert version_of("planet-261005.osm.pbf") == "2026-10-05"


def test_named_places(generation_dir: Path, tmp_path: Path) -> None:
    docs = {
        d.native_id: d
        for d in OsmPlanetAdapter().documents(make_ctx(SPEC, FakeFetcher(), tmp_path), generation_dir)
    }
    assert sorted(docs) == ["node/1", "node/2", "relation/20", "way/10"]  # unnamed / unlinked shop skipped
    berlin = docs["node/1"]
    assert berlin.entities == ("Q64",)
    assert "Population: 3769495" in berlin.text
    assert "Coordinates: 52.517037, 13.388860" in berlin.text
    assert berlin.links == ("https://de.wikipedia.org/wiki/Berlin",)
    assert berlin.url_native == "https://www.openstreetmap.org/node/1"
    assert docs["node/2"].aliases == ("Eiffel Tower",)
    assert "tourism=attraction" in docs["node/2"].text
    assert docs["way/10"].entities == ("Q160409",)
    assert "Coordinates" not in docs["way/10"].text
    assert docs["relation/20"].entities == ()


def test_place_record_requires_a_name() -> None:
    assert place_record("node", 1, {"place": "city"}, None, "osm-planet", "ODbL-1.0", ("en",)) is None
