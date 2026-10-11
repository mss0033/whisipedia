"""Kiwix release identity, content sets across generations and pinned id prefixes.

Audit 2026-10-11: C1 (a generation holds only the books it fetched), H3 (the version missed
re-releases and updates to every book but the newest), H4 (ids changed with the selector's size).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from adapters_testkit import build_zim, make_ctx, make_spec

from wsp_adapters._common import IncompleteGenerationError
from wsp_adapters.kiwix import (
    KiwixOpdsAdapter,
    OpdsEntry,
    Selector,
    book_key,
    bundle_content,
    names_digest,
    prefix_books,
    release_version,
    select_entries,
)
from wsp_core.model import DocRecord
from wsp_core.testing.adapter_contract import FakeFetcher


def test_single_book_version_keeps_the_rerelease_letter() -> None:
    assert release_version(["wikipedia_en_all_maxi_2026-07.zim"], bundle=False) == "2026-07"
    assert release_version(["wikipedia_en_all_maxi_2026-07a.zim"], bundle=False) == "2026-07a"


def test_bundle_version_changes_when_any_book_changes() -> None:
    before = ["a_en_all_2026-05.zim", "b_en_all_2026-08.zim"]
    updated = ["a_en_all_2026-06.zim", "b_en_all_2026-08.zim"]  # not the newest book
    rereleased = ["a_en_all_2026-05a.zim", "b_en_all_2026-08.zim"]
    versions = {release_version(v, bundle=True) for v in (before, updated, rereleased)}
    assert len(versions) == 3
    assert all(v.startswith("2026-08+") for v in versions)
    assert release_version(before, bundle=True) == f"2026-08+{names_digest(before)}"
    assert release_version(list(reversed(before)), bundle=True) == release_version(before, bundle=True)


def test_rerelease_letter_wins_selection() -> None:
    def entry(date: str) -> OpdsEntry:
        name = f"x_en_all_{date}.zim"
        return OpdsEntry("x_en_all", "", "c", ("eng",), "x", None, f"https://k/zim/c/{name}.meta4", 1)

    spec = make_spec("s", {"adapter": "kiwix-opds", "selector": {"name": "x_en_all"}})
    (chosen,) = select_entries([entry("2026-07"), entry("2026-07a")], Selector.from_spec(spec))
    assert chosen.file_name == "x_en_all_2026-07a.zim"


_SEALED = iter(range(10, 60))


def _gen(lib: Path, gen_id: str, *names: str) -> Path:
    """A sealed generation (``MANIFEST.json`` carries only what the chain reads: ``sealed_at``)."""
    up = lib / gen_id / "upstream"
    up.mkdir(parents=True)
    for n in names:
        (up / n).write_bytes(b"")
    (lib / gen_id / "MANIFEST.json").write_text(
        json.dumps({"sealed_at": f"2026-10-11T00:{next(_SEALED)}:00"})
    )
    return lib / gen_id


def _bundle_id(names: list[str]) -> str:
    return release_version(names, bundle=True).replace("+", "-")


def test_bundle_content_carries_unchanged_books_and_verifies_the_digest(tmp_path: Path) -> None:
    lib = tmp_path / "bundle"
    first = ["a_en_all_2026-05.zim", "b_en_all_2026-08.zim", "c_en_all_2026-01.zim"]
    g1 = _gen(lib, _bundle_id(first), *first)
    second = ["a_en_all_2026-09.zim", "b_en_all_2026-08.zim", "c_en_all_2026-01.zim"]
    g2 = _gen(lib, _bundle_id(second), "a_en_all_2026-09.zim")  # only the changed book was fetched
    content = bundle_content(g2)
    assert sorted(p.name for p in content.values()) == sorted(second)
    assert content["b_en_all"].parent.parent == g1
    adapter = KiwixOpdsAdapter()
    ctx = make_ctx(
        make_spec("bundle", {"adapter": "kiwix-opds", "selector": {"names": ["a", "b"]}}),
        FakeFetcher(),
        tmp_path,
    )
    assert adapter.depends_on(ctx, g2) == frozenset({g1.name})
    assert adapter.depends_on(ctx, g1) == frozenset()

    # c is dropped from the bundle: the digest says so, and c is not carried any further.
    third = ["a_en_all_2026-09.zim", "b_en_all_2026-10.zim"]
    g3 = _gen(lib, _bundle_id(third), "b_en_all_2026-10.zim")
    assert sorted(p.name for p in bundle_content(g3).values()) == sorted(third)
    # Same newest date as g3: release order comes from sealed_at, not from the digests.
    fourth = [*third, "d_en_all_2026-10.zim"]
    g4 = _gen(lib, _bundle_id(fourth), "d_en_all_2026-10.zim")
    assert sorted(p.name for p in bundle_content(g4).values()) == sorted(fourth)  # c stays gone
    assert sorted(p.name for p in bundle_content(g3).values()) == sorted(third)  # g4 is later


def test_a_missing_carried_book_raises(tmp_path: Path) -> None:
    lib = tmp_path / "bundle"
    names = ["a_en_all_2026-05.zim", "b_en_all_2026-08.zim"]
    g = _gen(lib, _bundle_id(names), "a_en_all_2026-05.zim")  # b was never stored anywhere
    with pytest.raises(IncompleteGenerationError, match="bundle digest"):
        bundle_content(g)


def test_single_book_content_follows_the_newest_file(tmp_path: Path) -> None:
    lib = tmp_path / "single"
    _gen(lib, "2026-07", "w_en_all_maxi_2026-07.zim")
    g2 = _gen(lib, "2026-08", "w_en_all_nopic_2026-08.zim")  # flavour change: replaces, not adds
    assert [p.name for p in bundle_content(g2, single_book=True).values()] == ["w_en_all_nopic_2026-08.zim"]
    empty = _gen(lib, "2026-08a")  # nothing new fetched: the previous file is the content
    assert [p.name for p in bundle_content(empty, single_book=True).values()] == [
        "w_en_all_nopic_2026-08.zim"
    ]


def test_book_key() -> None:
    assert book_key("wikipedia_en_all_maxi_2026-07a.zim") == "wikipedia_en_all_maxi"
    assert book_key("x.zim") == "x"


def test_prefix_books_is_pinned_by_the_registry() -> None:
    one = {"adapter": "kiwix-opds", "selector": {"name": "w"}}
    two = {"adapter": "kiwix-opds", "selector": {"names": ["w", "v"]}}
    assert prefix_books(make_spec("s", one)) is False  # default: the old rule
    assert prefix_books(make_spec("s", two)) is True
    # Pinned: adding a book to a pinned single-book source does not rename its ids.
    assert prefix_books(make_spec("s", {**two, "prefix_books": False})) is False
    assert prefix_books(make_spec("s", {**one, "prefix_books": True})) is True
    with pytest.raises(ValueError, match="prefix_books"):
        prefix_books(make_spec("s", {**one, "prefix_books": "yes"}))


def test_unprefixed_ids_refuse_a_second_book(tmp_path: Path) -> None:
    lib = tmp_path / "src"
    gen = lib / "2026-08" / "upstream"
    build_zim(gen / "w_en_all_2026-08.zim", name="w_en_all", articles=[("A", "A", "<p>a</p>")])
    build_zim(gen / "v_en_all_2026-08.zim", name="v_en_all", articles=[("A", "A", "<p>b</p>")])
    spec = make_spec(
        "src",
        {
            "adapter": "kiwix-opds",
            "selector": {"names": ["w_en_all", "v_en_all"]},
            "prefix_books": False,
            "workers": 1,
        },
    )
    with pytest.raises(ValueError, match="collide"):
        list(KiwixOpdsAdapter().entries(make_ctx(spec, FakeFetcher(), tmp_path / "w"), gen.parent))
    spec = make_spec(
        "src", {"adapter": "kiwix-opds", "selector": {"names": ["w_en_all", "v_en_all"]}, "workers": 1}
    )
    docs = [e for e in KiwixOpdsAdapter().entries(make_ctx(spec, FakeFetcher(), tmp_path / "w"), gen.parent)]
    assert sorted(d.native_id for d in docs if isinstance(d, DocRecord)) == ["v_en_all/A", "w_en_all/A"]
