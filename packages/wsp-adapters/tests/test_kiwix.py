from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
from adapters_testkit import FIXTURES, build_zim, make_ctx, make_spec, sha256_of

from wsp_adapters import available_adapters, load_adapter
from wsp_adapters.entries import Redirect
from wsp_adapters.kiwix import (
    CATALOG_URL,
    KiwixOpdsAdapter,
    Selector,
    parse_metalink,
    parse_opds,
    select_entries,
)
from wsp_core.ids import content_hash
from wsp_core.model import Artifact, Checksum, DocRecord, Generation, GenerationStatus
from wsp_core.ports import Ctx, SourceAdapter
from wsp_core.testing.adapter_contract import FakeFetcher, SourceAdapterContract

OPDS = (FIXTURES / "opds_entries.xml").read_bytes()
ZIM_NAME = "testpedia_en_all_maxi_2026-08.zim"
ZIM_URL = f"https://lb.download.kiwix.org/zim/other/{ZIM_NAME}"

ARTICLES = [
    (
        "Alan_Turing",
        "Alan Turing",
        "<html><head><style>p{}</style></head><body><h1>Alan Turing</h1>"
        "<p>Alan Turing was a <b>mathematician</b>.<sup class='reference'>[1]</sup></p>"
        "<script>track()</script><ol class='references'><li>Ref</li></ol></body></html>",
    ),
    ("Enigma", "Enigma machine", "<p>A cipher device.</p><ul><li>Rotor</li><li>Plugboard</li></ul>"),
]
REDIRECTS = [("Turing", "Turing", "Alan_Turing")]
ASSETS = [("style.css", "text/css", "body{}")]


def _entry_xml(name: str, flavour: str, file_url: str, category: str = "other") -> str:
    return f"""  <entry>
    <id>urn:uuid:00000000-0000-0000-0000-{abs(hash(file_url)) % 10**12:012d}</id>
    <title>{name}</title>
    <updated>2026-08-25T00:00:00Z</updated>
    <language>eng</language>
    <name>{name}</name>
    <flavour>{flavour}</flavour>
    <category>{category}</category>
    <dc:issued>2026-08-25T00:00:00Z</dc:issued>
    <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
          href="{file_url}.meta4" length="1024" />
  </entry>
"""


def _feed(*entries: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/">\n'
        f"  <totalResults>{len(entries)}</totalResults>\n" + "".join(entries) + "</feed>\n"
    ).encode()


def _metalink(name: str, size: int, sha256: str, urls: list[str]) -> bytes:
    url_xml = "".join(f'<url location="x" priority="{i + 1}">{u}</url>' for i, u in enumerate(urls))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<metalink xmlns="urn:ietf:params:xml:ns:metalink">
  <file name="{name}">
    <size>{size}</size>
    <hash type="md5">00112233445566778899aabbccddeeff</hash>
    <hash type="sha-1">0011223344556677889900112233445566778899</hash>
    <hash type="sha-256">{sha256}</hash>
    <pieces length="4194304" type="sha-1"><hash>aa</hash><hash>bb</hash></pieces>
    {url_xml}
  </file>
</metalink>
""".encode()


@pytest.fixture(scope="module")
def zim_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("zim") / ZIM_NAME
    return build_zim(path, name="testpedia_en_all", articles=ARTICLES, redirects=REDIRECTS, assets=ASSETS)


@pytest.fixture
def fetcher(zim_file: Path) -> FakeFetcher:
    digest = sha256_of(zim_file)
    return FakeFetcher(
        {
            CATALOG_URL: _feed(_entry_xml("testpedia_en_all", "maxi", ZIM_URL)),
            f"{ZIM_URL}.sha256": f"{digest}  {ZIM_NAME}\n".encode(),
            f"{ZIM_URL}.meta4": _metalink(
                ZIM_NAME,
                zim_file.stat().st_size,
                digest,
                ["https://mirror.example.org/zim/other/" + ZIM_NAME],
            ),
            ZIM_URL: zim_file.read_bytes(),
        }
    )


SINGLE = make_spec("wikipedia-en-zim", {"adapter": "kiwix-opds", "selector": {"name": "testpedia_en_all"}})


class TestKiwixContract(SourceAdapterContract):
    @pytest.fixture
    def adapter(self) -> SourceAdapter:
        return KiwixOpdsAdapter()

    @pytest.fixture
    def ctx(self, fetcher: FakeFetcher, tmp_path: Path) -> Ctx:
        return make_ctx(SINGLE, fetcher, tmp_path / "work")

    @pytest.fixture
    def generation_dir(self, zim_file: Path, tmp_path: Path) -> Path:
        gen = tmp_path / "gen"
        (gen / "upstream").mkdir(parents=True)
        (gen / "upstream" / ZIM_NAME).write_bytes(zim_file.read_bytes())
        return gen


def test_entry_point_is_registered() -> None:
    assert "kiwix-opds" in available_adapters()
    assert isinstance(load_adapter("kiwix-opds"), KiwixOpdsAdapter)


def test_parse_recorded_catalog() -> None:
    entries, total = parse_opds(OPDS)
    assert total == len(entries) == 9
    maxi = next(e for e in entries if e.name == "wikipedia_en_all" and e.flavour == "maxi")
    assert maxi.file_name == "wikipedia_en_all_maxi_2026-08.zim"
    assert maxi.zim_date == "2026-08"
    assert maxi.category == "wikipedia"
    assert maxi.length == 127418088448
    assert maxi.file_url.endswith("/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim")
    assert next(e for e in entries if e.name.startswith("ja.")).languages == ("eng", "jpn")


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ({"name": "wikipedia_en_all", "flavour": "maxi"}, ["wikipedia_en_all_maxi_2026-08.zim"]),
        ({"name": "wikipedia_en_all", "flavour": "mini"}, ["wikipedia_en_all_mini_2026-09.zim"]),
        ({"name": "mdwiki_en_all"}, ["mdwiki_en_all_maxi_2025-11.zim"]),  # maxi preferred
        ({"names": ["mdwiki_en_all", "ifixit_en_all"]},
         ["ifixit_en_all_2025-12.zim", "mdwiki_en_all_maxi_2025-11.zim"]),
        ({"category": "stack_exchange", "lang": "rus"}, ["ru.stackoverflow.com_mul_all_2026-08.zim",
                                                         "rus.stackexchange.com_mul_all_2026-08.zim"]),
        ({"category": "stack_exchange", "lang": "deu"}, []),
    ],
)  # fmt: skip
def test_selector(selector: dict[str, object], expected: list[str]) -> None:
    spec = make_spec("s", {"adapter": "kiwix-opds", "selector": selector})
    chosen = select_entries(parse_opds(OPDS)[0], Selector.from_spec(spec))
    assert [e.file_name for e in chosen] == expected


def test_selector_requires_name_or_category() -> None:
    with pytest.raises(ValueError, match="selector"):
        Selector.from_spec(make_spec("s", {"adapter": "kiwix-opds", "selector": {"lang": "eng"}}))


def test_discover_against_recorded_catalog(tmp_path: Path) -> None:
    spec = make_spec(
        "wikipedia-en-zim",
        {"adapter": "kiwix-opds", "selector": {"name": "wikipedia_en_all", "flavour": "maxi"}},
    )
    file_url = "https://lb.download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim"
    fetcher = FakeFetcher(
        {CATALOG_URL: OPDS, file_url + ".sha256": b"ab" * 32 + b"  wikipedia_en_all_maxi_2026-08.zim\n"}
    )
    (release,) = KiwixOpdsAdapter().discover(make_ctx(spec, fetcher, tmp_path))
    assert release.version == "2026-08"
    (remote,) = release.files
    assert remote.url == file_url
    assert remote.checksums == (Checksum("sha256", "ab" * 32),)
    assert (
        remote.mirrors[0]
        == "https://mirror.download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim"
    )
    assert remote.metalink == file_url + ".meta4"
    assert not any(m.endswith(".meta4") for m in remote.mirrors)
    assert release.meta["approx_size:" + remote.name] == "127418088448"
    assert any("name=wikipedia_en_all" in r for r in fetcher.requests)


def test_plan_resolves_metalink(fetcher: FakeFetcher, zim_file: Path, tmp_path: Path) -> None:
    adapter = KiwixOpdsAdapter()
    ctx = make_ctx(SINGLE, fetcher, tmp_path)
    release = next(iter(adapter.discover(ctx)))
    assert release.files[0].size is None
    (remote,) = adapter.plan(ctx, None, release).files
    assert remote.size == zim_file.stat().st_size
    assert {c.algorithm for c in remote.checksums} == {"sha256", "md5", "sha1"}
    assert remote.mirrors[0].startswith("https://mirror.example.org/")
    assert remote.metalink == ZIM_URL + ".meta4"


def test_plan_rejects_conflicting_metalink(fetcher: FakeFetcher, tmp_path: Path) -> None:
    fetcher.routes[f"{ZIM_URL}.meta4"] = _metalink(ZIM_NAME, 1, "00" * 32, [])
    adapter = KiwixOpdsAdapter()
    ctx = make_ctx(SINGLE, fetcher, tmp_path)
    with pytest.raises(ValueError, match="disagrees"):
        adapter.plan(ctx, None, next(iter(adapter.discover(ctx))))


def test_plan_skips_files_already_held(fetcher: FakeFetcher, zim_file: Path, tmp_path: Path) -> None:
    adapter = KiwixOpdsAdapter()
    ctx = make_ctx(SINGLE, fetcher, tmp_path)
    release = next(iter(adapter.discover(ctx)))
    held = Artifact(
        source_id=SINGLE.id,
        version="2026-07",
        relpath=PurePosixPath("upstream") / ZIM_NAME,
        size=zim_file.stat().st_size,
        sha256=sha256_of(zim_file),
        blake3="",
    )
    have = Generation(SINGLE.id, "2026-09-15", GenerationStatus.SEALED, artifacts=(held,))
    plan = adapter.plan(ctx, have, release)
    assert plan.files == ()
    assert plan.previous_generation == "2026-09-15"


def test_parse_metalink() -> None:
    link = parse_metalink(_metalink("x.zim", 42, "cd" * 32, ["https://b/", "https://a/"]))
    assert link.size == 42
    assert link.urls == ("https://b/", "https://a/")
    assert link.piece_length == 4194304
    assert link.piece_hashes == ("aa", "bb")
    assert Checksum("sha1", "0011223344556677889900112233445566778899") in link.checksums


def test_documents_extract_text_and_skip_redirects_and_assets(zim_file: Path, tmp_path: Path) -> None:
    ctx = make_ctx(SINGLE, FakeFetcher(), tmp_path)
    entries = list(KiwixOpdsAdapter().entries(ctx, zim_file.parent))
    docs = {e.native_id: e for e in entries if isinstance(e, DocRecord)}
    assert set(docs) == {"Alan_Turing", "Enigma"}
    assert Redirect("Turing", "Alan_Turing") in entries
    turing = docs["Alan_Turing"]
    assert turing.text == "Alan Turing\nAlan Turing was a mathematician."
    assert turing.title == "Alan Turing"
    assert turing.html_ref == "zim:Alan_Turing"
    assert turing.lang == "en"
    assert turing.license == "CC-BY-SA-4.0"
    assert turing.revision == "testpedia_en_all_maxi_2026-08"
    assert turing.content_hash == content_hash(turing.text)
    assert docs["Enigma"].text == "A cipher device.\nRotor\nPlugboard"


def test_multi_book_sources_prefix_native_ids(zim_file: Path, tmp_path: Path) -> None:
    spec = make_spec(
        "bundle", {"adapter": "kiwix-opds", "selector": {"names": ["testpedia_en_all", "other"]}}
    )
    docs = list(KiwixOpdsAdapter().documents(make_ctx(spec, FakeFetcher(), tmp_path), zim_file.parent))
    assert {d.native_id for d in docs} == {"testpedia_en_all/Alan_Turing", "testpedia_en_all/Enigma"}
    assert docs[0].html_ref == "zim:testpedia_en_all/Alan_Turing"


def test_parallel_matches_sequential(zim_file: Path, tmp_path: Path) -> None:
    def run(workers: int) -> list[tuple[str, str]]:
        spec = make_spec("p", {"adapter": "kiwix-opds", "selector": {"name": "x"}, "workers": workers})
        ctx = make_ctx(spec, FakeFetcher(), tmp_path)
        return [(d.native_id, d.content_hash) for d in KiwixOpdsAdapter().documents(ctx, zim_file.parent)]

    assert run(2) == run(1)
