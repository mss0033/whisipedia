"""``american-stories``: Chronicling America newspapers laid out into articles, one document per article."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest
from adapters_testkit import make_ctx, make_spec

from wsp_adapters import available_adapters
from wsp_adapters.americanstories import (
    HF,
    REPO,
    AmericanStoriesAdapter,
    page_documents,
    paper_name,
    release_files,
)
from wsp_core.ports import Ctx, SourceAdapter
from wsp_core.testing.adapter_contract import FakeFetcher, SourceAdapterContract

BODY = (
    "The undersigned, citizens of the county, met at the court house on Saturday last to consider "
    "the state of the roads and resolved that a petition be sent."
)


def page(date: str, number: int, articles: list[dict[str, Any]]) -> dict[str, Any]:
    stamp = date.replace("-", "")
    return {
        "lccn": {
            "title": "The Massachusetts spy. [volume] (Boston [Mass.]) 1770-1772",
            "state": "Massachusetts",
            "lccn": "sn83021193",
        },
        "page_number": number,
        "scan_url": f"https://chroniclingamerica.loc.gov/data/batches/mb_artemis_ver01/data/sn83021193/00517172169/{stamp}02/0157.jp2\n",
        "edition": {"date": date},
        "bboxes": [],
        "full articles": articles,
    }


def article(aid: str, headline: str, body: str, byline: str = "") -> dict[str, Any]:
    return {"full_article_id": aid, "headline": headline, "byline": byline, "article": body}


FIRST = page(
    "1771-12-26",
    1,
    [
        article("1", ".[NUMB. 43.", BODY),
        article("2", "PUBLIC MEETING.", BODY.replace("roads", "bridges"), "By a Correspondent"),
        article("3", "BOSTON", "Ship news."),  # a fragment: too short to read
    ],
)
NAME = "faro_1771/1771-12-26_p1_sn83021193_00517172169_1771122602_0157.json"
PAGES: dict[str, Any] = {NAME: FIRST, "faro_1771/bad.json": "{not json"}


def archive(pages: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in pages.items():
            data = content.encode() if isinstance(content, str) else json.dumps(content).encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


ARCHIVE = archive(PAGES)
LATER = archive(
    {"faro_1850/1850-03-01_p2_sn1_1_1850030101_7.json": page("1850-03-01", 2, [article("1", "Gold", BODY)])}
)
REV = "a" * 40
INFO = json.dumps({"sha": REV, "lastModified": "2025-03-26T15:49:08.000Z"}).encode()
TREE = json.dumps(
    [
        {"type": "file", "path": "README.md", "size": 10},
        {
            "type": "file",
            "path": "faro_1850.tar.gz",
            "size": len(LATER),
            "lfs": {"oid": hashlib.sha256(LATER).hexdigest(), "size": len(LATER)},
        },
        {
            "type": "file",
            "path": "faro_1771.tar.gz",
            "size": len(ARCHIVE),
            "lfs": {"oid": hashlib.sha256(ARCHIVE).hexdigest(), "size": len(ARCHIVE)},
        },
    ]
).encode()
SPEC = make_spec("american-stories", {"adapter": "american-stories"}, license_="CC-BY-4.0")


def test_registered() -> None:
    assert "american-stories" in available_adapters()


def test_release_files_are_the_yearly_archives_at_one_revision() -> None:
    files = release_files(json.loads(TREE), REPO, REV)
    assert [f.name for f in files] == ["faro_1771.tar.gz", "faro_1850.tar.gz"]
    assert files[0].url == f"{HF}/datasets/{REPO}/resolve/{REV}/faro_1771.tar.gz"
    assert files[0].checksums[0].algorithm == "sha256"
    assert files[0].size == len(ARCHIVE)


def test_paper_names_read_as_a_reader_would_cite_them() -> None:
    assert paper_name("The Massachusetts spy. [volume] (Boston [Mass.]) 1770-1772") == (
        "The Massachusetts spy (Boston, Mass.)"
    )
    assert (
        paper_name("Evening star. [volume] (Washington, D.C.) 1854-1972") == "Evening star (Washington, D.C.)"
    )
    assert paper_name("The Seattle star. (Seattle, Wash.) 1899-1947") == "The Seattle star (Seattle, Wash.)"


def test_each_article_is_a_document_titled_by_headline_paper_and_day() -> None:
    docs = list(page_documents(FIRST, "american-stories", "CC-BY-4.0", NAME))
    assert [d.native_id for d in docs] == [
        "1771-12-26_p1_sn83021193_00517172169_1771122602_0157/1",
        "1771-12-26_p1_sn83021193_00517172169_1771122602_0157/2",
    ]
    first, second = docs
    # A headline with no word (an issue number read as text) gives way to the paper and day.
    assert first.title == "The Massachusetts spy (Boston, Mass.), 1771-12-26, page 1"
    assert first.text == BODY
    assert second.title == "PUBLIC MEETING. — The Massachusetts spy (Boston, Mass.), 1771-12-26, page 1"
    assert second.text.startswith("PUBLIC MEETING.\n\nBy a Correspondent\n\nThe undersigned")
    assert second.url_native == "https://www.loc.gov/resource/sn83021193/1771-12-26/ed-2/?sp=1"
    assert second.timestamp is not None
    assert second.timestamp.year == 1771
    assert second.meta["newspaper"] == "The Massachusetts spy (Boston, Mass.)"
    assert second.meta["place"] == "Massachusetts"


def test_index_years_read_one_era(tmp_path: Path) -> None:
    up = tmp_path / "american-stories" / "g" / "upstream"
    up.mkdir(parents=True)
    (up / "faro_1771.tar.gz").write_bytes(ARCHIVE)
    (up / "faro_1850.tar.gz").write_bytes(LATER)
    every = list(AmericanStoriesAdapter().documents(make_ctx(SPEC, FakeFetcher(), tmp_path / "w"), up.parent))
    assert [d.timestamp.year for d in every if d.timestamp] == [1771, 1771, 1850]
    era = make_spec("american-stories", {"adapter": "american-stories", "index_years": [1800, 1899]})
    later = list(AmericanStoriesAdapter().documents(make_ctx(era, FakeFetcher(), tmp_path / "w"), up.parent))
    assert [d.native_id for d in later] == ["1850-03-01_p2_sn1_1_1850030101_7/1"]


class TestAmericanStoriesContract(SourceAdapterContract):
    @pytest.fixture
    def adapter(self) -> SourceAdapter:
        return AmericanStoriesAdapter()

    @pytest.fixture
    def ctx(self, tmp_path: Path) -> Ctx:
        routes = {
            f"{HF}/api/datasets/{REPO}": INFO,
            f"{HF}/api/datasets/{REPO}/tree/{REV}": TREE,
            f"{HF}/datasets/{REPO}/resolve/{REV}/faro_1771.tar.gz": ARCHIVE,
            f"{HF}/datasets/{REPO}/resolve/{REV}/faro_1850.tar.gz": LATER,
        }
        return make_ctx(SPEC, FakeFetcher(routes), tmp_path / "work")

    @pytest.fixture
    def generation_dir(self, tmp_path: Path) -> Path:
        up = tmp_path / "american-stories" / "2025-03-26" / "upstream"
        up.mkdir(parents=True)
        (up / "faro_1771.tar.gz").write_bytes(ARCHIVE)
        return up.parent


def test_the_file_name_keys_the_page_when_its_fields_are_unread_or_wrong() -> None:
    """A page's ``page_number`` is sometimes "na", sometimes another page's (1809-05-02, The
    Delaware Gazette: page 1 said 3, and its articles took page 3's ids)."""
    wrong = {**FIRST, "page_number": "3"}
    docs = list(
        page_documents(
            wrong, "american-stories", "PD", "faro_1771/1771-12-26_p1_sn83021193_1_1771122601_9.json"
        )
    )
    assert docs[0].native_id == "1771-12-26_p1_sn83021193_1_1771122601_9/1"
    assert docs[0].url_native == "https://www.loc.gov/resource/sn83021193/1771-12-26/ed-1/?sp=1"
    unread = list(page_documents({**FIRST, "page_number": "na"}, "american-stories", "PD", "odd.json"))
    assert unread[0].native_id == "odd/1"
    assert unread[0].url_native is None


def test_column_lines_join_into_paragraphs_and_entities_decode() -> None:
    body = (
        "With Miller's House, a two-story Dwell-\ninc House, commodious Stone\n"
        "Store &amp; Shop.\n\n\nAlso sixty acres " + "of land. " * 12
    )
    page = {
        **FIRST,
        "lccn": {
            **FIRST["lccn"],
            "title": "Alexandria gazette, commercial &amp; political. (Alexandria, Va.) 1808-1811",
        },
        "full articles": [article("1", "FOR SALE", body)],
    }
    (doc,) = page_documents(page, "american-stories", "PD", NAME)
    assert doc.text.startswith(
        "FOR SALE\n\nWith Miller's House, a two-story Dwellinc House, commodious Stone "
        "Store & Shop.\n\nAlso sixty"
    )
    assert doc.meta["newspaper"] == "Alexandria gazette, commercial & political (Alexandria, Va.)"
