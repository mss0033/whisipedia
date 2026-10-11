from datetime import UTC, datetime
from typing import Any

from wsp_api.renditions import borrowed, zim_document
from wsp_core.ids import make_doc_id
from wsp_core.model import DocRecord, Generation, GenerationStatus, SourceSpec, SourceStatus, Tier

GEN = Generation("zim-src", "2026-08", GenerationStatus.SEALED, sealed_at=datetime(2026, 8, 1, tzinfo=UTC))
PAGE = "<html><head><title>Alan Turing</title></head><body><p>Mathematician &amp; pioneer.</p></body></html>"


def spec(sid: str, adapter: str, **access: Any) -> SourceSpec:
    return SourceSpec(
        id=sid,
        name=sid,
        tier=Tier.T1,
        status=SourceStatus.ACTIVE,
        license="CC-BY-SA-4.0",
        legal_basis="open-license",
        domain=("encyclopedia",),
        adapter=adapter,
        raw={"access": {"adapter": adapter, **access}},
    )


class Vault:
    def __init__(self, current: dict[str, Generation]) -> None:
        self._current = current

    def current(self, source_id: str) -> Generation | None:
        return self._current.get(source_id)


class Assets:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, str, str]] = []

    def html(self, source_id: str, generation_id: str, html_ref: str) -> str | None:
        self.calls.append((source_id, generation_id, html_ref))
        return self.pages.get(html_ref)


def test_unrefined_zim_entry_is_served_live() -> None:
    assets = Assets({"zim:Alan_Turing": PAGE})
    doc = zim_document(spec("zim-src", "kiwix-opds"), "Alan_Turing", Vault({"zim-src": GEN}), assets)  # type: ignore[arg-type]
    assert doc is not None
    assert doc.doc_id == make_doc_id("zim-src", "Alan_Turing")
    assert doc.title == "Alan Turing"
    assert "Mathematician & pioneer." in doc.text
    assert doc.html_ref == "zim:Alan_Turing"


def test_live_serving_only_for_zim_sources_with_the_entry() -> None:
    vault, assets = Vault({"zim-src": GEN}), Assets({})
    assert zim_document(spec("zim-src", "kiwix-opds"), "Missing", vault, assets) is None  # type: ignore[arg-type]
    assert zim_document(spec("dump", "wikimedia-dumps"), "Alan_Turing", vault, assets) is None  # type: ignore[arg-type]
    assert zim_document(None, "Alan_Turing", vault, assets) is None  # type: ignore[arg-type]


def test_borrowed_rendition_matches_language_and_title() -> None:
    dump = spec("dump", "wikimedia-dumps", rendition=[{"lang": "en", "source": "zim-src"}])
    doc = DocRecord(
        make_doc_id("dump", "en/Alan Turing"), "dump", "en/Alan Turing", "Alan Turing", "en", "x", "t"
    )
    alt = borrowed(dump, doc, Vault({"zim-src": GEN}))  # type: ignore[arg-type]
    assert alt is not None
    assert (alt.source_id, alt.generation.generation_id, alt.html_ref) == (
        "zim-src",
        "2026-08",
        "zim:Alan_Turing",
    )
    german = DocRecord(make_doc_id("dump", "de/X"), "dump", "de/X", "X", "de", "x", "t")
    assert borrowed(dump, german, Vault({"zim-src": GEN})) is None  # type: ignore[arg-type]
    assert borrowed(dump, doc, Vault({})) is None  # type: ignore[arg-type]


def test_live_text_leaves_out_scripts_and_styles() -> None:
    page = (
        "<html><head><title>T</title><style>a { color: red }</style>"
        "<script>RLCONF = {};</script></head><body><p>Readable.</p>"
        "<SCRIPT type='x'>if (a < b) {}</SCRIPT><noscript><p>Enable JS</p></noscript></body></html>"
    )
    doc = zim_document(spec("zim-src", "kiwix-opds"), "T", Vault({"zim-src": GEN}), Assets({"zim:T": page}))  # type: ignore[arg-type]
    assert doc is not None
    assert doc.text == "Readable."
