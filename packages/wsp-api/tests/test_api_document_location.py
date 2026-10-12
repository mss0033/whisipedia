"""View Source Data names the file that holds the page (audit 2026-10-12 F4, F5, F17) and the links of a
text document resolve to pages the library holds (F6, F12)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from wsp_api.testing.fakes import InMemoryDocumentStore, InMemoryVault, sample_services
from wsp_api.views import Library
from wsp_api.zimread import ZimUnavailable
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import Artifact, DocRecord, Generation, GenerationStatus

SEALED = GenerationStatus.SEALED


def art(source: str, gen: str, rel: str, digest: str) -> Artifact:
    return Artifact(source, gen, PurePosixPath(rel), len(rel), digest * 64, digest * 64)


def gen(source: str, gid: str, *arts: Artifact) -> Generation:
    return Generation(source, gid, SEALED, arts, datetime(2026, 10, 11, tzinfo=UTC))


def rec(source: str, native: str, ref: str | None, links: tuple[str, ...] = ()) -> DocRecord:
    return DocRecord(
        doc_id=make_doc_id(source, native),
        source_id=source,
        native_id=native,
        title=native,
        lang="en",
        license="LicenseRef-US-Gov-PD",
        text="text",
        content_hash=content_hash("text"),
        html_ref=ref,
        links=links,
    )


class Zims:
    """The bundle's book -> archive map, as ZimAssetStore.file_of answers it."""

    def __init__(self, files: dict[str, str] | None, busy: bool = False) -> None:
        self.files, self.busy = files or {}, busy

    def file_of(self, source_id: str, generation_id: str, native_id: str) -> str | None:
        return self.files.get(native_id.partition("/")[0])

    def exists(self, source_id: str, generation_id: str, native_id: str) -> bool:
        if self.busy:
            raise ZimUnavailable("the book list of this collection is still being built")
        return native_id in self.files


def library() -> tuple[Library, InMemoryVault, InMemoryDocumentStore]:
    services = sample_services()
    vault, docs = services.vault, services.documents
    assert isinstance(vault, InMemoryVault)
    assert isinstance(docs, InMemoryDocumentStore)
    return Library(services), vault, docs


def test_a_multi_archive_page_names_its_own_archive() -> None:
    lib, _, _ = library()
    g = gen(
        "se",
        "2026-07",
        art("se", "2026-07", "upstream/3dprinting.zim", "a"),
        art("se", "2026-07", "upstream/diy.zim", "b"),
    )
    lib.zim = Zims({"diy": "/vault/se/2026-07/upstream/diy.zim"})  # type: ignore[assignment]
    loc = lib.vault_location(g, "zim:diy/questions/1", "se")
    assert loc.path.endswith("upstream/diy.zim")
    assert loc.sha256 == "b" * 64
    assert loc.entry == "diy/questions/1"
    unknown = lib.vault_location(g, "zim:other/x", "se")  # never another archive's hashes
    assert unknown.sha256 is None
    assert unknown.path.endswith("2026-07")


def test_a_stored_original_names_its_file_and_generation() -> None:
    lib, vault, docs = library()
    src = "usgs-historical-topo"
    ct = gen(
        src,
        "2026-10-11.ct",
        art(src, "2026-10-11.ct", "upstream/plainfield.pdf", "c"),
        art(src, "2026-10-11.ct", "upstream/other.pdf", "d"),
    )
    nj = gen(src, "2026-10-11.nj", art(src, "2026-10-11.nj", "upstream/newark.pdf", "e"))
    vault.add(ct, promote=True)
    vault.add(nj, promote=True)  # the partition promoted last is CURRENT
    docs.put("2026-10-11.ri.set00001", rec(src, "plainfield", "file:2026-10-11.ct/upstream/plainfield.pdf"))
    view = lib.document(make_doc_id(src, "plainfield"), None)
    assert view.generation_id == "2026-10-11.ct"
    assert view.vault is not None
    assert view.vault.path.endswith("2026-10-11.ct/upstream/plainfield.pdf")
    assert view.vault.sha256 == "c" * 64


def test_text_links_resolve_to_held_zim_pages_and_busy_is_not_absent() -> None:
    lib, _, _ = library()
    zim_source = next(s for s in lib.s.sources if s.adapter == "kiwix-opds")
    held, missing = f"wsp:{zim_source.id}/Held", f"wsp:{zim_source.id}/Missing"
    zims = Zims({"Held": "/x.zim"})
    lib.zim = zims  # type: ignore[assignment]
    lib.vault.current = lambda source_id: gen(source_id, "2026-08")  # type: ignore[method-assign]
    links = lib.links(rec("gutenberg", "84", None, (held, missing, "https://example.org/")))
    assert [link.target for link in links] == [held, None, None]
    zims.busy = True
    with pytest.raises(ZimUnavailable):
        lib.exists(make_doc_id(zim_source.id, "Held"))  # a 503 the client retries, not "not in the library"
    assert [link.target for link in lib.links(rec("gutenberg", "84", None, (held,)))] == [None]
