from __future__ import annotations

import bz2
import hashlib
import json
from pathlib import Path

import pytest
from adapters_testkit import (
    FIXTURES,
    REPO_ROOT,
    Page,
    build_multistream,
    dumps_listing,
    dumpstatus_for,
    make_ctx,
    make_spec,
)

from wsp_adapters import available_adapters, load_adapter
from wsp_adapters.entries import Redirect
from wsp_adapters.wikimedia import (
    DUMPS_BASE,
    WikimediaDumpsAdapter,
    entity_record,
    group_ranges,
    index_path_for,
    parse_dumpstatus,
    stream_offsets,
    wiki_site,
    wikis_for,
)
from wsp_core.ids import content_hash
from wsp_core.model import Checksum, DocRecord
from wsp_core.ports import Ctx, SourceAdapter
from wsp_core.registry import load_registry
from wsp_core.testing.adapter_contract import FakeFetcher, SourceAdapterContract

DUMPSTATUS = (FIXTURES / "dumpstatus_enwiki_20261001.json").read_bytes()
DATE = "20261001"

PAGES = [
    Page(
        1,
        "Alan Turing",
        "'''Alan Turing''' was a [[mathematician]].<ref>x</ref>\n== Life ==\nBorn in [[London]].",
    ),
    Page(2, "Talk:Alan Turing", "talk page", ns=1),
    Page(3, "Turing", redirect="Alan Turing"),
    Page(4, "Enigma machine", "{{Infobox|a=b}}The '''Enigma''' is a [[cipher|cipher device]]."),
    Page(5, "London", "Capital & largest city."),
    Page(6, "Rotor", "A [[Enigma machine#Rotors|rotor]]."),
    Page(7, "Plugboard", "Swaps letters."),
]


@pytest.fixture(scope="module")
def dump(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    return build_multistream(tmp_path_factory.mktemp("dump") / "upstream", "enwiktionary", DATE, PAGES)


WIKT = make_spec("wiktionary-en", {"adapter": "wikimedia-dumps"})


def _dump_routes(dump: tuple[Path, Path]) -> dict[str, bytes]:
    xml, index = dump
    base = f"{DUMPS_BASE}enwiktionary/"
    routes = {
        base: dumps_listing(["20260901", DATE]),
        f"{base}{DATE}/dumpstatus.json": dumpstatus_for([xml, index], "enwiktionary", DATE),
        f"{base}20260901/dumpstatus.json": dumpstatus_for([xml, index], "enwiktionary", "20260901"),
    }
    for f in (xml, index):
        routes[f"{DUMPS_BASE}enwiktionary/{DATE}/{f.name}"] = f.read_bytes()
    return routes


class TestWikimediaContract(SourceAdapterContract):
    @pytest.fixture
    def adapter(self) -> SourceAdapter:
        return WikimediaDumpsAdapter()

    @pytest.fixture
    def ctx(self, dump: tuple[Path, Path], tmp_path: Path) -> Ctx:
        return make_ctx(WIKT, FakeFetcher(_dump_routes(dump)), tmp_path / "work")

    @pytest.fixture
    def generation_dir(self, dump: tuple[Path, Path]) -> Path:
        return dump[0].parent.parent


def test_entry_point_is_registered() -> None:
    assert "wikimedia-dumps" in available_adapters()
    assert isinstance(load_adapter("wikimedia-dumps"), WikimediaDumpsAdapter)


def test_wiki_names_from_repository_registry() -> None:
    specs = {
        s.id: s
        for s in load_registry(
            REPO_ROOT / "registry/sources.yaml", REPO_ROOT / "registry/schema/source.schema.json"
        )
    }
    assert wikis_for(specs["wikipedia-top10"]) == tuple(
        f"{lang}wiki" for lang in ("en", "ceb", "de", "fr", "sv", "nl", "es", "ru", "it", "pl")
    )
    assert wikis_for(specs["wiktionary-en"]) == ("enwiktionary",)
    assert wikis_for(specs["wikisource-en"]) == ("enwikisource",)
    assert wikis_for(specs["wikidata"]) == ()
    assert wikis_for(make_spec("x", {"wikis": ["frwiki"]})) == ("frwiki",)
    with pytest.raises(ValueError, match=r"access\.wikis"):
        wikis_for(make_spec("gutenberg", {}))


def test_wiki_site() -> None:
    assert wiki_site("enwiki").domain == "en.wikipedia.org"
    assert wiki_site("zh_yuewiki").lang == "zh-yue"
    site = wiki_site("enwiktionary")
    assert (site.domain, site.capitalize) == ("en.wiktionary.org", False)


def test_parse_recorded_dumpstatus() -> None:
    mirrors = ["https://mirror.example.org/dumps/"]
    combined = parse_dumpstatus(
        DUMPSTATUS, wiki="enwiki", date=DATE, base="https://dumps.wikimedia.org", mirrors=mirrors
    )
    assert combined is not None
    assert [f.name for f in combined.files] == [
        "enwiki-20261001-pages-articles-multistream-index.txt.bz2",
        "enwiki-20261001-pages-articles-multistream.xml.bz2",
    ]
    xml = combined.files[1]
    assert xml.size == 26917225724
    assert (
        xml.url
        == "https://dumps.wikimedia.org/enwiki/20261001/enwiki-20261001-pages-articles-multistream.xml.bz2"
    )
    assert xml.checksums == (
        Checksum("sha1", "992b5ca87b3404894f013e59a43efbb8b1bc012b"),
        Checksum("md5", "85ebcbc1f7a8b2503138c93b627150d5"),
    )
    assert xml.mirrors == ("https://mirror.example.org/dumps/enwiki/20261001/" + xml.name,)
    parts = parse_dumpstatus(
        DUMPSTATUS, wiki="enwiki", date=DATE, base="https://x", mirrors=(), layout="parts"
    )
    assert parts is not None
    assert len(parts.files) == 4


def test_unfinished_run_is_not_a_release() -> None:
    status = json.loads(DUMPSTATUS)
    status["jobs"]["articlesmultistreamdump"]["status"] = "in-progress"
    assert (
        parse_dumpstatus(json.dumps(status).encode(), wiki="enwiki", date=DATE, base="", mirrors=()) is None
    )


def test_discover_requires_every_wiki_done(tmp_path: Path, dump: tuple[Path, Path]) -> None:
    files = list(dump)
    routes = {
        f"{DUMPS_BASE}enwiki/": dumps_listing(["20260901", DATE]),
        f"{DUMPS_BASE}dewiki/": dumps_listing(["20260820", "20260901", DATE]),
        f"{DUMPS_BASE}enwiki/{DATE}/dumpstatus.json": dumpstatus_for(files, "enwiki", DATE),
        f"{DUMPS_BASE}dewiki/{DATE}/dumpstatus.json": dumpstatus_for(
            files, "dewiki", DATE, status="in-progress"
        ),
        f"{DUMPS_BASE}enwiki/20260901/dumpstatus.json": dumpstatus_for(files, "enwiki", "20260901"),
        f"{DUMPS_BASE}dewiki/20260901/dumpstatus.json": dumpstatus_for(files, "dewiki", "20260901"),
    }
    spec = make_spec("wikipedia-top2", {"adapter": "wikimedia-dumps"}, languages=["en", "de"])
    (release,) = WikimediaDumpsAdapter().discover(make_ctx(spec, FakeFetcher(routes), tmp_path))
    assert release.version == "2026-09-01"
    assert release.meta == {"run": "20260901", "wikis": "enwiki,dewiki"}
    assert len(release.files) == 4
    assert (
        release.files[0]
        .mirrors[0]
        .startswith("https://ftp.acc.umu.se/mirror/wikimedia.org/dumps/enwiki/20260901/")
    )


def test_discover_falls_back_to_sha1sums(tmp_path: Path, dump: tuple[Path, Path]) -> None:
    routes = _dump_routes(dump)
    status = json.loads(routes[f"{DUMPS_BASE}enwiktionary/{DATE}/dumpstatus.json"])
    for info in status["jobs"]["articlesmultistreamdump"]["files"].values():
        del info["sha1"], info["md5"]
    routes[f"{DUMPS_BASE}enwiktionary/{DATE}/dumpstatus.json"] = json.dumps(status).encode()
    sums = "".join(f"{hashlib.sha1(f.read_bytes()).hexdigest()}  {f.name}\n" for f in dump)
    routes[f"{DUMPS_BASE}enwiktionary/{DATE}/enwiktionary-{DATE}-sha1sums.txt"] = sums.encode()
    release = next(iter(WikimediaDumpsAdapter().discover(make_ctx(WIKT, FakeFetcher(routes), tmp_path))))
    assert all(f.checksums and f.checksums[0].algorithm == "sha1" for f in release.files)


def test_discover_wikidata_entities(tmp_path: Path) -> None:
    base = f"{DUMPS_BASE}wikidatawiki/entities/"
    name = "wikidata-20261005-all.json.bz2"
    routes = {
        base: dumps_listing(["20260930", "20261005", "20261007"]),
        f"{base}20261005/wikidata-20261005-sha1sums.txt": f"{'a' * 40}  {name}\n".encode(),
        f"{base}20261005/wikidata-20261005-md5sums.txt": f"{'b' * 32}  {name}\n".encode(),
        f"{base}20261005/": f'<a href="{name}">{name}</a>   06-Oct-2026 10:32   103337856107\n'.encode(),
    }  # 20261007 is still running: no checksum files yet
    spec = make_spec("wikidata", {"adapter": "wikimedia-dumps", "endpoints": [base]}, license_="CC0-1.0")
    (release,) = WikimediaDumpsAdapter().discover(make_ctx(spec, FakeFetcher(routes), tmp_path))
    assert release.version == "2026-10-05"
    (remote,) = release.files
    assert remote.size == 103337856107
    assert remote.checksums == (Checksum("sha1", "a" * 40), Checksum("md5", "b" * 32))
    assert remote.url == f"{base}20261005/{name}"


def test_index_and_ranges(dump: tuple[Path, Path]) -> None:
    xml, index = dump
    assert index_path_for(xml) == index
    offsets = stream_offsets(index)
    assert len(offsets) == 4  # 7 pages, 2 per stream
    ranges = group_ranges(offsets, xml.stat().st_size, target=1)
    assert ranges[0][0] == offsets[0] > 0  # header stream skipped
    assert ranges[-1][1] == xml.stat().st_size
    assert all(a < b for a, b in ranges)
    assert group_ranges(offsets, xml.stat().st_size) == [(offsets[0], xml.stat().st_size)]


def test_documents_from_multistream(dump: tuple[Path, Path], tmp_path: Path) -> None:
    ctx = make_ctx(WIKT, FakeFetcher(), tmp_path)
    entries = list(WikimediaDumpsAdapter().entries(ctx, dump[0].parent.parent))
    docs = {e.native_id: e for e in entries if isinstance(e, DocRecord)}
    assert list(docs) == ["Alan_Turing", "Enigma_machine", "London", "Rotor", "Plugboard"]
    assert Redirect("Turing", "Alan_Turing") in entries
    turing = docs["Alan_Turing"]
    assert turing.text == "Alan Turing was a mathematician.\nLife\nBorn in London."
    assert turing.doc_id == "wsp:wiktionary-en/Alan_Turing"
    assert turing.url_native == "https://en.wiktionary.org/wiki/Alan_Turing"
    assert turing.lang == "en"
    assert turing.revision == "1000"
    assert turing.timestamp is not None
    assert turing.timestamp.year == 2026
    assert turing.links == (
        "wsp:wiktionary-en/mathematician",
        "wsp:wiktionary-en/London",
    )  # Wiktionary: no capitalization
    assert turing.content_hash == content_hash(turing.text)
    assert docs["Enigma_machine"].text == "The Enigma is a cipher device."
    assert docs["London"].text == "Capital & largest city."


def test_parallel_and_sequential_paths_agree(dump: tuple[Path, Path], tmp_path: Path) -> None:
    def run(workers: int, with_index: bool) -> list[tuple[str, str]]:
        gen = tmp_path / f"g{workers}{with_index}" / "upstream"
        gen.mkdir(parents=True)
        for f in dump:
            if with_index or "index" not in f.name:
                (gen / f.name).write_bytes(f.read_bytes())
        spec = make_spec("wiktionary-en", {"adapter": "wikimedia-dumps", "workers": workers})
        ctx = make_ctx(spec, FakeFetcher(), tmp_path)
        return [(d.native_id, d.content_hash) for d in WikimediaDumpsAdapter().documents(ctx, gen.parent)]

    reference = run(1, True)
    assert run(2, True) == reference
    assert run(1, False) == reference


def test_multi_wiki_sources_prefix_language(tmp_path: Path) -> None:
    gen = tmp_path / "gen" / "upstream"
    build_multistream(gen, "dewiki", DATE, [Page(1, "Berlin", "Hauptstadt.")])
    build_multistream(gen, "enwiki", DATE, [Page(1, "Berlin", "Capital.")])
    spec = make_spec("wikipedia-top2", {"adapter": "wikimedia-dumps"}, languages=["en", "de"])
    docs = list(WikimediaDumpsAdapter().documents(make_ctx(spec, FakeFetcher(), tmp_path), gen.parent))
    assert [(d.native_id, d.lang, d.url_native) for d in docs] == [
        ("de/Berlin", "de", "https://de.wikipedia.org/wiki/Berlin"),
        ("en/Berlin", "en", "https://en.wikipedia.org/wiki/Berlin"),
    ]


ENTITY = {
    "type": "item",
    "id": "Q42",
    "labels": {"en": {"language": "en", "value": "Douglas Adams"}},
    "descriptions": {"en": {"language": "en", "value": "English author and humourist"}},
    "aliases": {"en": [{"language": "en", "value": "Douglas Noel Adams"}]},
    "lastrevid": 123,
    "modified": "2026-10-01T00:00:00Z",
}


def test_entity_record() -> None:
    doc = entity_record(ENTITY, "wikidata", "CC0-1.0", "en")
    assert doc is not None
    assert doc.doc_id == "wsp:wikidata/Q42"
    assert doc.text == "Douglas Adams\nEnglish author and humourist\nAlso known as: Douglas Noel Adams"
    assert doc.entities == ("Q42",)
    assert doc.url_native == "https://www.wikidata.org/wiki/Q42"
    prop = entity_record(
        {"type": "property", "id": "P31", "labels": {"fr": {"value": "nature"}}}, "w", "CC0-1.0", "en"
    )
    assert prop is not None
    assert prop.title == "nature"
    assert prop.url_native == "https://www.wikidata.org/wiki/Property:P31"


def test_documents_from_entity_dump(tmp_path: Path) -> None:
    upstream = tmp_path / "gen" / "upstream"
    upstream.mkdir(parents=True)
    other = {"type": "item", "id": "Q1", "labels": {"en": {"value": "Universe"}}}
    body = "[\n" + json.dumps(ENTITY) + ",\n" + json.dumps(other) + "\n]\n"
    (upstream / "wikidata-20261005-all.json.bz2").write_bytes(bz2.compress(body.encode()))
    spec = make_spec("wikidata", {"adapter": "wikimedia-dumps"}, license_="CC0-1.0")
    docs = list(WikimediaDumpsAdapter().documents(make_ctx(spec, FakeFetcher(), tmp_path), upstream.parent))
    assert [d.native_id for d in docs] == ["Q42", "Q1"]
    assert docs[1].text == "Universe"
