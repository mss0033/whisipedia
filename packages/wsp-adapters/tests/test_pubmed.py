from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

import pytest
from adapters_testkit import make_ctx, make_spec

from wsp_adapters import available_adapters, load_adapter
from wsp_adapters.pubmed import PubmedAdapter, file_key, parse_md5
from wsp_core.model import Checksum
from wsp_core.ports import Ctx, SourceAdapter
from wsp_core.testing.adapter_contract import FakeFetcher, SourceAdapterContract

BASE = "https://ftp.ncbi.nlm.nih.gov/pubmed/baseline/"
UPD = "https://ftp.ncbi.nlm.nih.gov/pubmed/updatefiles/"
SPEC = make_spec("pubmed", {"adapter": "pubmed", "workers": 1}, license_="LicenseRef-NLM-terms")


def article(pmid: int, title: str, *, abstract: str = "", mesh: tuple[str, ...] = (), doi: str = "") -> str:
    parts = "".join(
        f'<AbstractText Label="{label}">{text}</AbstractText>'
        for label, _, text in (p.partition("|") for p in abstract.split(";") if p)
    )
    headings = "".join(
        f'<MeshHeading><DescriptorName UI="D{i:06d}">{m}</DescriptorName></MeshHeading>'
        for i, m in enumerate(mesh)
    )
    ids = f'<ArticleId IdType="doi">{doi}</ArticleId>' if doi else ""
    return f"""<PubmedArticle><MedlineCitation Status="MEDLINE" Owner="NLM">
<PMID Version="1">{pmid}</PMID>
<DateRevised><Year>2026</Year><Month>01</Month><Day>2{pmid % 10}</Day></DateRevised>
<Article><Journal><JournalIssue><PubDate><Year>2025</Year></PubDate></JournalIssue>
<Title>Journal of Tests</Title>
</Journal><ArticleTitle>{title} <i>in vitro</i>.</ArticleTitle><Abstract>{parts}</Abstract>
<AuthorList><Author><LastName>Curie</LastName><ForeName>Marie</ForeName></Author>
<Author><CollectiveName>Test Consortium</CollectiveName></Author></AuthorList>
<Language>eng</Language></Article><MeshHeadingList>{headings}</MeshHeadingList>
<KeywordList><Keyword>fixtures</Keyword></KeywordList></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="pubmed">{pmid}</ArticleId>{ids}
<ArticleId IdType="pmc">PMC{pmid}</ArticleId></ArticleIdList></PubmedData></PubmedArticle>
"""


def xml_file(*articles: str, deleted: tuple[int, ...] = ()) -> bytes:
    deletions = ""
    if deleted:
        deletions = (
            "<DeleteCitation>"
            + "".join(f'<PMID Version="1">{p}</PMID>' for p in deleted)
            + "</DeleteCitation>"
        )
    body = (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE PubmedArticleSet>\n<PubmedArticleSet>\n'
        + "".join(articles)
        + deletions
        + "</PubmedArticleSet>\n"
    )
    return gzip.compress(body.encode(), mtime=0)


BASELINE = xml_file(
    article(
        1, "Aspirin and headache", abstract="BACKGROUND|Pain is common.;RESULTS|It helps.", mesh=("Aspirin",)
    ),
    article(2, "Old title", doi="10.1000/xyz"),
    article(3, "Retracted study"),
)
UPDATE = xml_file(article(2, "Revised title", doi="10.1000/xyz"), article(4, "New study"), deleted=(3,))


def listing(*names: str) -> bytes:
    return (
        "<html><body><pre>" + "".join(f'<a href="{n}">{n}</a>\n' for n in names) + "</pre></body></html>"
    ).encode()


def routes() -> dict[str, bytes]:
    out = {
        BASE: listing("../", "pubmed26n0001.xml.gz", "pubmed26n0001.xml.gz.md5", "README.txt"),
        UPD: listing("pubmed26n0002.xml.gz", "pubmed26n0002.xml.gz.md5", "pubmed25n1300.xml.gz"),
        f"{BASE}pubmed26n0001.xml.gz": BASELINE,
        f"{UPD}pubmed26n0002.xml.gz": UPDATE,
    }
    for url in (f"{BASE}pubmed26n0001.xml.gz", f"{UPD}pubmed26n0002.xml.gz"):
        out[url + ".md5"] = f"MD5({url.rsplit('/', 1)[-1]})= {hashlib.md5(out[url]).hexdigest()}\n".encode()
    return out


@pytest.fixture
def generation_dir(tmp_path: Path) -> Path:
    up = tmp_path / "gen" / "upstream"
    up.mkdir(parents=True)
    (up / "pubmed26n0001.xml.gz").write_bytes(BASELINE)
    (up / "pubmed26n0002.xml.gz").write_bytes(UPDATE)
    return tmp_path / "gen"


class TestPubmedContract(SourceAdapterContract):
    @pytest.fixture
    def adapter(self) -> SourceAdapter:
        return PubmedAdapter(interval=0)

    @pytest.fixture
    def ctx(self, tmp_path: Path) -> Ctx:
        return make_ctx(SPEC, FakeFetcher(routes()), tmp_path / "work")

    @pytest.fixture
    def generation_dir(self, generation_dir: Path) -> Path:
        return generation_dir


def test_registered() -> None:
    assert "pubmed" in available_adapters()
    assert load_adapter("pubmed").id == "pubmed"


def test_discover_baseline_plus_updates(tmp_path: Path) -> None:
    (release,) = PubmedAdapter(interval=0).discover(make_ctx(SPEC, FakeFetcher(routes()), tmp_path))
    assert release.version == "pubmed26-u0002"
    assert [f.name for f in release.files] == ["pubmed26n0001.xml.gz", "pubmed26n0002.xml.gz"]
    assert release.files[0].checksums == (Checksum("md5", hashlib.md5(BASELINE).hexdigest()),)
    assert release.meta == {"baseline_files": "1", "update_files": "1"}


def test_newest_version_wins_and_deletions_hide_older(generation_dir: Path, tmp_path: Path) -> None:
    ctx = make_ctx(SPEC, FakeFetcher(), tmp_path)
    docs = {d.native_id: d for d in PubmedAdapter().documents(ctx, generation_dir)}
    assert sorted(docs) == ["1", "2", "4"]
    assert docs["2"].title == "Revised title in vitro."
    one = docs["1"]
    assert "BACKGROUND: Pain is common." in one.text
    assert "MeSH terms: Aspirin" in one.text
    assert "Marie Curie, Test Consortium" in one.text
    assert "Journal of Tests. 2025" in one.text
    assert one.lang == "en"
    assert one.revision == "2026-01-21"
    assert one.url_native == "https://pubmed.ncbi.nlm.nih.gov/1/"
    assert "https://id.nlm.nih.gov/mesh/D000000" in one.links
    assert docs["2"].links[0] == "https://doi.org/10.1000/xyz"


def test_parallel_matches_sequential(generation_dir: Path, tmp_path: Path) -> None:
    seq = [
        (d.doc_id, d.content_hash)
        for d in PubmedAdapter().documents(make_ctx(SPEC, FakeFetcher(), tmp_path), generation_dir)
    ]
    spec = make_spec("pubmed", {"adapter": "pubmed", "workers": 2}, license_="LicenseRef-NLM-terms")
    par = [
        (d.doc_id, d.content_hash)
        for d in PubmedAdapter().documents(make_ctx(spec, FakeFetcher(), tmp_path), generation_dir)
    ]
    assert par == seq


def test_helpers() -> None:
    assert file_key("pubmed26n0042.xml.gz") == ("26", 42)
    assert file_key("pubmed26n0042.xml.gz.md5") is None
    assert (
        parse_md5("MD5(pubmed26n0001.xml.gz)= ABCDEF0123456789abcdef0123456789\n")
        == "abcdef0123456789abcdef0123456789"
    )
    assert parse_md5("garbage") is None
