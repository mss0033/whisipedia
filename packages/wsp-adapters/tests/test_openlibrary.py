from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest
from adapters_testkit import make_ctx, make_spec

from wsp_adapters import available_adapters
from wsp_adapters.openlibrary import DATA_URL, OpenLibraryAdapter
from wsp_core.model import RemoteHead, VerdictStatus
from wsp_core.ports import Ctx, SourceAdapter
from wsp_core.testing.adapter_contract import FakeFetcher, SourceAdapterContract

SPEC = make_spec("open-library", {"adapter": "open-library", "workers": 1}, license_="CC0-1.0")


def line(kind: str, key: str, record: dict[str, Any], revision: int = 1) -> str:
    return (
        f"/type/{kind}\t{key}\t{revision}\t2026-09-30T10:00:00.000000\t{json.dumps({**record, 'key': key})}\n"
    )


AUTHORS = "".join(
    [
        line(
            "author",
            "/authors/OL1A",
            {
                "name": "Mary Shelley",
                "birth_date": "1797",
                "death_date": "1851",
                "alternate_names": ["Mary Wollstonecraft Shelley"],
                "remote_ids": {"wikidata": "Q47152"},
            },
        ),
        line("redirect", "/authors/OL9A", {"location": "/authors/OL1A"}),
    ]
)
WORKS = line(
    "work",
    "/works/OL1W",
    {
        "title": "Frankenstein",
        "subtitle": "or, The Modern Prometheus",
        "authors": [{"type": {"key": "/type/author_role"}, "author": {"key": "/authors/OL1A"}}],
        "subjects": ["Science fiction", "Monsters"],
        "description": {"type": "/type/text", "value": "A scientist creates life."},
        "first_publish_date": "1818",
    },
)
EDITIONS = line(
    "edition",
    "/books/OL1M",
    {
        "title": "Frankenstein",
        "authors": [{"key": "/authors/OL1A"}],
        "publishers": ["Lackington"],
        "identifiers": {"project_gutenberg": ["84"], "librivox": ["3271"]},
        "ocaid": "frankenstein00shel",
        "publish_date": "1818",
        "isbn_13": ["9780000000002"],
        "languages": [{"key": "/languages/eng"}],
        "works": [{"key": "/works/OL1W"}],
    },
)
FILES = {
    "ol_dump_authors_latest.txt.gz": gzip.compress(AUTHORS.encode(), mtime=0),
    "ol_dump_works_latest.txt.gz": gzip.compress(WORKS.encode(), mtime=0),
    "ol_dump_editions_latest.txt.gz": gzip.compress(EDITIONS.encode(), mtime=0),
}


@pytest.fixture
def generation_dir(tmp_path: Path) -> Path:
    up = tmp_path / "gen" / "upstream"
    up.mkdir(parents=True)
    for name, data in FILES.items():
        (up / name).write_bytes(data)
    return tmp_path / "gen"


class HeadFetcher(FakeFetcher):
    def head(self, url: str) -> RemoteHead:
        kind = url.rsplit("_dump_", 1)[1].split("_")[0]
        return RemoteHead(
            f"https://archive.org/download/ol_dump_2026-09-30/ol_dump_{kind}_2026-09-30.txt.gz", size=42
        )


class TestOpenLibraryContract(SourceAdapterContract):
    @pytest.fixture
    def adapter(self) -> SourceAdapter:
        return OpenLibraryAdapter()

    @pytest.fixture
    def ctx(self, tmp_path: Path) -> Ctx:
        return make_ctx(SPEC, FakeFetcher({DATA_URL + n: d for n, d in FILES.items()}), tmp_path / "work")

    @pytest.fixture
    def generation_dir(self, generation_dir: Path) -> Path:
        return generation_dir

    def test_verify_detects_corruption(self, adapter: SourceAdapter, ctx: Ctx) -> None:
        """Open Library publishes no checksums: a flipped byte is undetectable upstream-side (the vault's
        own SHA-256/BLAKE3 take over after ingest), but a missing file is still reported."""
        release = next(iter(adapter.discover(ctx)))
        remote = release.files[0]
        path = next(iter(adapter.fetch(ctx, adapter.plan(ctx, None, release)))).path
        assert adapter.verify(ctx, remote, path).status is VerdictStatus.UNVERIFIABLE
        path.unlink()
        assert adapter.verify(ctx, remote, path).status is VerdictStatus.MISSING


def test_discover_uses_the_dated_redirect(tmp_path: Path) -> None:
    assert "open-library" in available_adapters()
    (release,) = OpenLibraryAdapter().discover(make_ctx(SPEC, HeadFetcher(), tmp_path))
    assert release.version == "2026-09-30"
    assert [f.name for f in release.files] == [
        "ol_dump_authors_latest.txt.gz",
        "ol_dump_works_latest.txt.gz",
        "ol_dump_editions_latest.txt.gz",
    ]
    assert release.files[0].size == 42


def test_catalog_records(generation_dir: Path, tmp_path: Path) -> None:
    ctx = make_ctx(SPEC, FakeFetcher(), tmp_path / "work")
    docs = {d.native_id: d for d in OpenLibraryAdapter().documents(ctx, generation_dir)}
    assert list(docs) == ["authors/OL1A", "works/OL1W", "books/OL1M"]  # the redirect is skipped
    author, work, edition = docs.values()
    assert author.title == "Mary Shelley"
    assert author.entities == ("Q47152",)
    assert author.aliases == ("Mary Wollstonecraft Shelley",)
    assert "Open Library catalog record: author." in author.text
    assert work.text.splitlines()[:3] == [
        "Frankenstein: or, The Modern Prometheus",
        "Open Library catalog record: work.",
        "By: Mary Shelley",
    ]
    assert "Subjects: Science fiction; Monsters" in work.text
    assert "A scientist creates life." in work.text
    assert edition.lang == "en"
    assert "By: Mary Shelley" in edition.text
    assert "ISBN: 9780000000002" in edition.text
    assert (
        "Also at: Project Gutenberg #84; LibriVox 3271; Internet Archive frankenstein00shel" in edition.text
    )
    assert edition.links == (
        "wsp:gutenberg/84",
        "https://archive.org/details/frankenstein00shel",
        "https://openlibrary.org/works/OL1W",
    )
    assert "Also at" not in work.text
    assert edition.url_native == "https://openlibrary.org/books/OL1M"
    assert not (tmp_path / "work" / "openlibrary-authors.sqlite").exists()
