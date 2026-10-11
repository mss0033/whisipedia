"""The ZIM store's caches (audit 2026-10-11 #6) and ZIM pages through the API (#8, #10, #15)."""

import threading
import time
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

from wsp_api import ApiSettings, create_app
from wsp_api.addresses import document_href
from wsp_api.app import library_of
from wsp_api.testing import local_client
from wsp_api.testing.fakes import SAMPLE_GEN_2, CountingVault, InMemoryVault, sample_services
from wsp_api.testing.zims import write_zim as _zim
from wsp_api.zim import BookIndex, ZimAssetStore
from wsp_api.zimread import ArchiveCache, EntryData, InProcessZimReader, Want, ZimUnavailable
from wsp_core.model import Artifact, Generation, GenerationStatus
from wsp_core.ports import Vault

GEN = SAMPLE_GEN_2


def _bundle(tmp_path: Path) -> InMemoryVault:
    """Source ``qa``: two books whose file names do not match their ``Name`` metadata."""
    up = tmp_path / "qa" / GEN / "upstream"
    _zim(
        up / "askubuntu.com_en_all_2026-07.zim",
        "askubuntu.com_en_all",
        [
            ("questions/1", "text/html", '<p><img src="/-/static/logo.png"></p>'),
            ("-/static/logo.png", "image/png", "x"),
        ],
        [],
    )
    _zim(up / "oddname_2026-07.zim", "math.stackexchange.com_en_all", [("q/2", "text/html", "<p>m</p>")], [])
    vault = InMemoryVault(root=tmp_path)
    vault.add(Generation("qa", GEN, GenerationStatus.SEALED), promote=True)
    return vault


def _single(tmp_path: Path, source: str = "wiki") -> InMemoryVault:
    rel = "upstream/wiki_2026-09.zim"
    _zim(tmp_path / source / GEN / rel, "wiki", [("A/Page", "text/html", "<title>Page</title><p>hi</p>")], [])
    vault = InMemoryVault(root=tmp_path)
    art = Artifact(source, GEN, PurePosixPath(rel), 1, "s" * 64, "b" * 64)
    vault.add(Generation(source, GEN, GenerationStatus.SEALED, artifacts=(art,)), promote=True)
    return vault


def test_unknown_generations_are_rejected_before_any_lookup(tmp_path: Path) -> None:
    vault = CountingVault(_single(tmp_path))
    store = ZimAssetStore(vault)
    assert store.html("wiki", GEN, "zim:A/Page") is not None
    real = store.layout("wiki", GEN)
    vault.calls.clear()
    for i in range(50):  # bogus ids neither walk the vault nor evict the real archive's layout
        assert store.asset("wiki", f"bogus{i}", "I/x.png") is None
        assert store.asset("wiki", "../../etc", "passwd") is None
    assert {c for c, _ in vault.calls} == {"generations"}  # only the in-memory list in the server
    assert store.layout("wiki", GEN) is real
    assert len(store._layouts) == 1  # pyright: ignore[reportPrivateUsage] - no negative entries


def test_archives_open_once_under_concurrency_and_failures_are_not_cached() -> None:
    opened: list[str] = []
    fail = {"once": True}
    gate = threading.Event()

    def slow_open(file: str) -> Any:
        opened.append(file)
        gate.wait(5)
        if file == "bad" and fail["once"]:
            fail["once"] = False
            raise RuntimeError("SMB hiccup")
        return object()

    cache = ArchiveCache(4, slow_open)
    results: list[object] = []
    errors: list[BaseException] = []

    def get(file: str) -> None:
        try:
            results.append(cache.get(file))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=get, args=("big.zim",)) for _ in range(16)]
    threads += [threading.Thread(target=get, args=("bad",)) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    gate.set()
    for t in threads:
        t.join(5)
    assert opened.count("big.zim") == 1  # single-flight
    assert len({id(r) for r in results}) == 1
    assert len(errors) == 4  # waiters share the failure ...
    assert opened.count("bad") == 1
    assert cache.get("bad") is not None  # ... which is not remembered
    assert opened.count("bad") == 2


def test_archive_cache_is_bounded() -> None:
    cache = ArchiveCache(2, lambda f: cast(Any, object()))
    for f in ("a", "b", "c"):
        cache.get(f)
    assert len(cache) == 2


class _CountingReader(InProcessZimReader):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[tuple[str, str, Want]] = []
        self.books: list[str] = []
        self.lock = threading.Lock()

    def read(self, file: str, path: str, want: Want) -> EntryData | None:
        with self.lock:
            self.reads.append((Path(file).name, path, want))
        return super().read(file, path, want)

    def book_name(self, file: str) -> str:
        with self.lock:
            self.books.append(Path(file).name)
        return super().book_name(file)


def test_book_index_is_built_once_and_persisted(tmp_path: Path) -> None:
    vault = _bundle(tmp_path)
    index_file = tmp_path / "cache" / "zim-books.json"
    reader = _CountingReader()
    store = ZimAssetStore(vault, reader, book_index=BookIndex(index_file))
    assert store.html("qa", GEN, "zim:math.stackexchange.com_en_all/q/2") == "<p>m</p>"
    assert store.html("qa", GEN, "zim:nosuchbook/q/2") is None
    assert store.html("qa", GEN, "zim:nosuchbook2/q/2") is None
    assert sorted(reader.books) == ["askubuntu.com_en_all_2026-07.zim", "oddname_2026-07.zim"]  # once
    assert index_file.is_file()

    class NoBooks(InProcessZimReader):
        def book_name(self, file: str) -> str:
            raise AssertionError("the persisted index is used")

    again = ZimAssetStore(vault, NoBooks(), book_index=BookIndex(index_file))
    assert again.books("qa", GEN) == ["askubuntu.com_en_all", "math.stackexchange.com_en_all"]
    assert again.html("qa", GEN, "zim:math.stackexchange.com_en_all/q/2") == "<p>m</p>"


def test_server_builds_the_book_index_in_the_background(tmp_path: Path) -> None:
    vault = _bundle(tmp_path)
    store = ZimAssetStore(vault, InProcessZimReader(), sync_books=False)
    # The file name implies this book's name: served at once, while the index builds.
    assert store.html("qa", GEN, "zim:askubuntu.com_en_all/questions/1") is not None
    deadline = time.monotonic() + 10
    while store.book_index.get("qa", GEN) is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert store.html("qa", GEN, "zim:math.stackexchange.com_en_all/q/2") == "<p>m</p>"

    fresh = ZimAssetStore(vault, InProcessZimReader(), sync_books=False)
    with pytest.raises(ZimUnavailable):  # not implied by a file name: "try again", not "not found"
        fresh.html("qa", GEN, "zim:math.stackexchange.com_en_all/q/2")


def test_bundle_root_relative_urls_stay_in_their_book(tmp_path: Path) -> None:
    services = replace(sample_services(), vault=cast(Vault, _bundle(tmp_path)))
    services = replace(services, sources=(*services.sources, _spec("qa")))
    with local_client(create_app(services)) as c:
        doc = c.get(document_href("wsp:qa/askubuntu.com_en_all/questions/1")).json()
        src = f"/v1/assets/qa/{GEN}/askubuntu.com_en_all/-/static/logo.png"
        assert f'src="{src}"' in doc["html"]
        assert c.get(src).content == b"x"  # the book's own file


def _spec(source_id: str) -> Any:
    from wsp_api.testing.fakes import SAMPLE_SOURCES

    zim = next(s for s in SAMPLE_SOURCES if s.adapter == "kiwix-opds")
    return replace(zim, id=source_id, name=source_id)


def test_live_zim_page_is_read_once_and_served_from_the_page_cache(tmp_path: Path) -> None:
    reader = _CountingReader()
    services = replace(sample_services(), vault=cast(Vault, _single(tmp_path, "wiki")))
    services = replace(services, sources=(*services.sources, _spec("wiki")))
    services = replace(services, assets=ZimAssetStore(services.vault, reader))
    app = create_app(services)
    with local_client(app) as c:
        href = document_href("wsp:wiki/A/Page")
        first = c.get(href)
        assert first.status_code == 200
        assert first.json()["title"] == "Page"
        assert first.json()["text"] is None  # HTML page: text only on request
        assert c.get(href, params={"fields": "text"}).json()["text"] == "hi"
        etag = first.headers["etag"]
        assert c.get(href, headers={"If-None-Match": etag}).status_code == 304
        threads = [threading.Thread(target=lambda: c.get(href)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    html_reads = [r for r in reader.reads if r[2] is Want.HTML]
    assert html_reads == [("wiki_2026-09.zim", "A/Page", Want.HTML)]  # one read, however many views
    assert library_of(app).pages.hits >= 3


def test_go_and_search_navigation_find_zim_only_pages(tmp_path: Path) -> None:
    reader = _CountingReader()
    services = replace(sample_services(), vault=cast(Vault, _single(tmp_path, "wiki")))
    services = replace(services, sources=(*services.sources, _spec("wiki")))
    services = replace(services, assets=ZimAssetStore(services.vault, reader))
    with local_client(create_app(services)) as c:
        r = c.get("/go/wiki/A/Page")  # not refined, only in the ZIM (audit #10)
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "document"
        assert c.get("/go/wiki/A/Nope").status_code == 404
        assert c.get("/v1/search", params={"q": "wsp:wiki/A/Page"}).json()["navigate_to"] == "wsp:wiki/A/Page"
    assert all(want is Want.NONE for _, _, want in reader.reads)  # existence never reads a page


def test_server_mode_answers_from_the_snapshot_and_zim_workers(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    vault = CountingVault(_single(tmp_path, "wiki"))
    services = replace(sample_services(), vault=cast(Vault, vault))
    services = replace(services, sources=(*services.sources, _spec("wiki")))
    settings = ApiSettings(background=True, snapshot_seconds=3600, ops_seconds=3600, zim_workers=1)
    app = create_app(services, settings)
    with TestClient(app, base_url="http://127.0.0.1:8737") as c:  # runs the lifespan: refreshers start
        assert c.get("/v1/sources").status_code == 200
        lib = library_of(app)
        deadline = time.monotonic() + 20
        while lib.state.home_doc_id(_spec("wiki"), vault.inner.current("wiki")) is None:
            assert time.monotonic() < deadline, "landing pages are found in the background"
            time.sleep(0.05)
        vault.calls.clear()
        doc = c.get(document_href("wsp:wiki/A/Page"))
        assert doc.status_code == 200, doc.text
        assert "hi" in doc.json()["html"]
        sources = c.get("/v1/sources", params={"limit": 200}).json()["items"]
        assert next(s for s in sources if s["id"] == "wiki")["home_doc_id"] == "wsp:wiki/A/Page"
        assert c.get("/v1/system").status_code == 200
        assert vault.calls == []  # requests never read the vault in the server (audit #4)
        assert lib.zim is not None
        pool = cast(Any, lib.zim.reader)
        assert pool.stats()["workers"][0]["pid"] is not None  # read in a worker process (audit #2)
