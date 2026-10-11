"""In-memory fakes for the seams in :mod:`wsp_api.seams` (assets, gap log, catalog categories).

Kept apart from :mod:`wsp_api.testing.fakes` (the core-port fakes) so the two can evolve separately.
:func:`browse_services` adds an HTML document with an image and a few categories to the sample library.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from wsp_api.catalog import FeedCatalog
from wsp_api.services import Services
from wsp_api.testing.fakes import SAMPLE_GEN_2, InMemoryDocumentStore, InMemoryLexicalIndex, sample_services
from wsp_core.ids import DocId, content_hash, make_doc_id
from wsp_core.model import Asset, Category, ChangeKind, ChangeRecord, DocRecord, Gap
from wsp_core.ports import ChangeFeed

# 1x1 transparent PNG.
PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


class InMemoryAssetStore:
    """``{(source, generation, path): Asset}``; ``html`` looks up ``html_ref`` minus ``zim:``."""

    def __init__(self, files: Mapping[tuple[str, str, str], Asset] | None = None) -> None:
        self.files: dict[tuple[str, str, str], Asset] = dict(files or {})

    def html(self, source_id: str, generation_id: str, html_ref: str) -> str | None:
        found = self.asset(source_id, generation_id, html_ref.removeprefix("zim:"))
        if found is None or not found.mimetype.startswith("text/html"):
            return None
        return found.content.decode("utf-8")

    def asset(self, source_id: str, generation_id: str, path: str) -> Asset | None:
        return self.files.get((source_id, generation_id, path))


class InMemoryGapLog:
    def __init__(self) -> None:
        self.gaps: list[Gap] = []

    def record(self, gap: Gap) -> None:
        self.gaps.append(gap)


class CategorizedCatalog(FeedCatalog):
    """:class:`FeedCatalog` plus fixed per-source categories ``{source: {slug: (name, [doc ids])}}``."""

    def __init__(self, feed: ChangeFeed, categories: Mapping[str, Mapping[str, tuple[str, Sequence[DocId]]]]):
        super().__init__(feed)
        self._cats = categories

    @property
    def category_map(self) -> Mapping[str, Mapping[str, tuple[str, Sequence[DocId]]]]:
        return self._cats

    def categories(self, source_id: str) -> Sequence[Category]:
        return [
            Category(slug=slug, name=name, count=len(ids))
            for slug, (name, ids) in sorted(self._cats.get(source_id, {}).items())
        ]

    def category_docs(self, source_id: str, slug: str, *, limit: int) -> Sequence[DocId]:
        return list(self._cats.get(source_id, {}).get(slug, ("", ()))[1])[:limit]


ENIGMA = make_doc_id("wikipedia-en-zim", "A/Enigma_machine")
ENIGMA_HTML = """<!doctype html><html><head><title>Enigma machine</title></head><body>
<p>The <b>Enigma machine</b> was broken with help from <a href="Alan_Turing">Alan Turing</a>
and <a href="../A/Bombe#history">the Bombe</a>. See <a href="https://example.org/enigma">elsewhere</a>.</p>
<img src="../I/enigma.png" srcset="../I/enigma-2x.png 2x" alt="An Enigma machine">
</body></html>"""


def browse_services() -> Services:
    """The sample library plus an HTML (ZIM-style) document with an image, categories and a gap log."""
    base = sample_services()
    text = "The Enigma machine was broken with help from Alan Turing and the Bombe."
    enigma = DocRecord(
        doc_id=ENIGMA,
        source_id="wikipedia-en-zim",
        native_id="A/Enigma_machine",
        title="Enigma machine",
        lang="en",
        license="CC-BY-SA-4.0",
        text=text,
        html_ref="zim:A/Enigma_machine",
        links=(
            "wsp:wikipedia-en-zim/A/Alan_Turing",
            "wsp:wikipedia-en-zim/A/Bombe",
            "https://example.org/enigma",
        ),
        content_hash=content_hash(text),
    )
    documents, index = base.documents, base.index
    assert isinstance(documents, InMemoryDocumentStore)
    assert isinstance(index, InMemoryLexicalIndex)
    documents.put(SAMPLE_GEN_2, enigma)
    index.upsert([enigma])
    index.commit()
    base.changes.publish([ChangeRecord(ENIGMA, ChangeKind.ADDED, SAMPLE_GEN_2, None, enigma.content_hash)])
    assets = InMemoryAssetStore(
        {
            ("wikipedia-en-zim", SAMPLE_GEN_2, "A/Enigma_machine"): Asset(ENIGMA_HTML.encode(), "text/html"),
            ("wikipedia-en-zim", SAMPLE_GEN_2, "I/enigma.png"): Asset(PIXEL_PNG, "image/png"),
            ("wikipedia-en-zim", SAMPLE_GEN_2, "I/evil.js"): Asset(b"alert(1)", "application/javascript"),
        }
    )
    catalog = CategorizedCatalog(
        base.changes,
        {
            "wikipedia-en-zim": {
                "computing": ("Computing", [make_doc_id("wikipedia-en-zim", "A/Alan_Turing"), ENIGMA]),
                "medicine": ("Medicine", [make_doc_id("wikipedia-en-zim", "A/Vaccine")]),
            }
        },
    )
    return replace(base, assets=assets, gaps=InMemoryGapLog(), catalog=catalog)
