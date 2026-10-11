"""``<name>@<flavour>`` in a Kiwix selector pins one book's flavour (multilingual plan, D15)."""

from __future__ import annotations

from adapters_testkit import make_spec

from wsp_adapters.kiwix import OpdsEntry, Selector, select_entries


def entry(name: str, flavour: str) -> OpdsEntry:
    return OpdsEntry(
        name, flavour, "wikipedia", ("deu",), name, None, f"https://x/{name}_{flavour}.zim.meta4", 1
    )


def test_pinned_flavours_mix_in_one_bundle() -> None:
    spec = make_spec(
        "kiwix-multilingual-de",
        {"adapter": "kiwix-opds", "selector": {"names": ["wikipedia_de_all@nopic", "wiktionary_de_all"]}},
    )
    sel = Selector.from_spec(spec)
    assert sel.names == ("wikipedia_de_all", "wiktionary_de_all")
    assert sel.queries() == [{"name": "wikipedia_de_all"}, {"name": "wiktionary_de_all"}]
    entries = [
        entry("wikipedia_de_all", "maxi"),
        entry("wikipedia_de_all", "nopic"),
        entry("wiktionary_de_all", "maxi"),
        entry("wiktionary_de_all", "nopic"),
    ]
    chosen = {e.name: e.flavour for e in select_entries(entries, sel)}
    assert chosen == {"wikipedia_de_all": "nopic", "wiktionary_de_all": "maxi"}
