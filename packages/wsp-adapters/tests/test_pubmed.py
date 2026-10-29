from __future__ import annotations

import gzip
import hashlib
from pathlib import Path
from typing import Any

import pytest
from adapters_testkit import make_ctx, make_spec

from wsp_adapters import available_adapters, load_adapter
from wsp_adapters._common import IncompleteGenerationError
from wsp_adapters.pubmed import (
    PubmedAdapter,
    content_set,
    file_key,
    parse_md5,
)
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


# --- audit 2026-10-11: C1 (content set across generations) and H1 (DOI) -------------------------------


def _gen(root: Path, name: str, files: dict[str, bytes]) -> Path:
    up = root / name / "upstream"
    up.mkdir(parents=True)
    for fname, data in files.items():
        (up / fname).write_bytes(data)
    return root / name


def test_update_generation_reads_the_baseline_from_its_earlier_generation(tmp_path: Path) -> None:
    lib = tmp_path / "pubmed"
    _gen(lib, "pubmed26", {"pubmed26n0001.xml.gz": BASELINE})
    upd = _gen(lib, "pubmed26-u0002", {"pubmed26n0002.xml.gz": UPDATE})
    _gen(lib, "pubmed26-u0003", {"pubmed26n0003.xml.gz": UPDATE})  # newer: never read by u0002
    _gen(lib, "pubmed25", {"pubmed25n0001.xml.gz": UPDATE})  # another year: never read
    adapter, ctx = PubmedAdapter(), make_ctx(SPEC, FakeFetcher(), tmp_path / "w")
    docs = {d.native_id: d for d in adapter.documents(ctx, upd)}
    assert sorted(docs) == ["1", "2", "4"]  # the baseline's 1 is there; 3 is deleted by the update
    assert adapter.depends_on(ctx, upd) == frozenset({"pubmed26"})
    assert adapter.depends_on(ctx, lib / "pubmed26") == frozenset()


def test_missing_baseline_raises_instead_of_refining_updates_alone(tmp_path: Path) -> None:
    upd = _gen(tmp_path / "pubmed", "pubmed26-u0002", {"pubmed26n0002.xml.gz": UPDATE})
    with pytest.raises(IncompleteGenerationError, match="no baseline file"):
        list(PubmedAdapter().documents(make_ctx(SPEC, FakeFetcher(), tmp_path / "w"), upd))


def test_a_gap_in_the_update_files_raises(tmp_path: Path) -> None:
    lib = tmp_path / "pubmed"
    _gen(lib, "pubmed26", {"pubmed26n0001.xml.gz": BASELINE})
    upd = _gen(lib, "pubmed26-u0003", {"pubmed26n0003.xml.gz": UPDATE})
    with pytest.raises(IncompleteGenerationError, match="0002"):
        content_set(upd)
    short = _gen(lib, "pubmed26-u0009", {"pubmed26n0002.xml.gz": UPDATE})
    with pytest.raises(IncompleteGenerationError, match="end at"):
        content_set(short)


def test_workers_honour_wsp_max_workers(
    generation_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wsp_adapters.pubmed as pubmed_module

    seen: list[int] = []
    real = pubmed_module.ordered_parallel_map  # pyright: ignore[reportPrivateImportUsage]

    def spy(fn: Any, items: Any, workers: int, **kw: Any) -> Any:
        seen.append(workers)
        return real(fn, items, 1, **kw)

    monkeypatch.setattr(pubmed_module, "ordered_parallel_map", spy)
    monkeypatch.setenv("WSP_MAX_WORKERS", "2")
    spec = make_spec("pubmed", {"adapter": "pubmed", "workers": 8}, license_="LicenseRef-NLM-terms")
    list(PubmedAdapter().documents(make_ctx(spec, FakeFetcher(), tmp_path), generation_dir))
    assert seen == [2]


def test_a_broken_article_is_a_reject_not_an_aborted_refine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wsp_adapters.pubmed as pubmed_module

    real = pubmed_module.article_record

    def flaky(elem: Any, source_id: str, license_: str) -> Any:
        if (elem.findtext("MedlineCitation/PMID") or "").strip() == "9":
            raise ValueError("boom")
        return real(elem, source_id, license_)

    monkeypatch.setattr(pubmed_module, "article_record", flaky)  # workers=1 runs inline
    gen = _gen(
        tmp_path / "pubmed", "x", {"pubmed26n0001.xml.gz": xml_file(article(1, "Fine"), article(9, "Bad"))}
    )
    docs = [
        d.native_id for d in PubmedAdapter().documents(make_ctx(SPEC, FakeFetcher(), tmp_path / "w"), gen)
    ]
    assert docs == ["1"]
    rejects = (tmp_path / "w" / "_rejects-x.jsonl").read_text()
    assert "PMID 9" in rejects
    assert "boom" in rejects


def test_doi_comes_from_the_article_not_its_references(tmp_path: Path) -> None:
    body = article(7, "Citing paper", doi="10.1000/own").replace(
        "</ArticleIdList></PubmedData>",
        "</ArticleIdList><ReferenceList><Reference><Citation>Other.</Citation><ArticleIdList>"
        '<ArticleId IdType="doi">10.9999/cited</ArticleId><ArticleId IdType="pmc">PMC1</ArticleId>'
        "</ArticleIdList></Reference></ReferenceList></PubmedData>",
    )
    no_doi = article(8, "No DOI of its own").replace(
        "</ArticleIdList></PubmedData>",
        '</ArticleIdList><ReferenceList><Reference><ArticleIdList><ArticleId IdType="doi">10.9999/x'
        "</ArticleId></ArticleIdList></Reference></ReferenceList></PubmedData>",
    )
    gen = _gen(tmp_path / "pubmed", "x", {"pubmed26n0001.xml.gz": xml_file(body, no_doi)})
    docs = {d.native_id: d for d in PubmedAdapter().documents(make_ctx(SPEC, FakeFetcher(), tmp_path), gen)}
    assert docs["7"].links[0] == "https://doi.org/10.1000/own"
    assert "https://pmc.ncbi.nlm.nih.gov/articles/PMC7/" in docs["7"].links
    assert not any("10.9999" in link or "PMC1/" in link for d in docs.values() for link in d.links)


def test_a_pmid_repeated_within_one_file_keeps_its_last_copy(tmp_path: Path) -> None:
    """2026-10-26 review #74: a same-day correction follows the record it corrects; the first, older
    copy was kept."""
    data = xml_file(article(7, "Old title."), article(7, "Corrected title."))
    gen = _gen(tmp_path / "pubmed", "pubmed26", {"pubmed26n0001.xml.gz": data})
    docs = list(PubmedAdapter().documents(make_ctx(SPEC, FakeFetcher(), tmp_path / "w"), gen))
    assert [(d.native_id, d.title.split(" in vitro")[0]) for d in docs] == [("7", "Corrected title.")]
