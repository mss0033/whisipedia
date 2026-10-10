from __future__ import annotations

import random
from pathlib import Path

from wsp_index.suggest import SuggestEntry, Suggester, TitleTrie, suggest_entries_bruteforce
from wsp_index.text import normalize_key, sanitize_query


def _entries(n: int, seed: int = 7) -> list[SuggestEntry]:
    rng = random.Random(seed)
    words = ["al", "alpha", "alps", "beta", "bet", "the", "theory", "zürich", "a", "ab", "abc"]
    out: list[SuggestEntry] = []
    for i in range(n):
        title = " ".join(rng.choice(words) for _ in range(rng.randint(1, 3))) + f" {i}"
        aliases = (f"alias {title}",) if i % 7 == 0 else ()
        out.append(SuggestEntry(f"wsp:s/{i}", title, aliases, prior=rng.random() * 3 + i * 1e-6))
    return out


def test_trie_matches_bruteforce_including_heavy_prefixes(tmp_path: Path) -> None:
    entries = _entries(600)
    trie = TitleTrie.build(entries, tmp_path, heavy_threshold=8, top_k=6)
    sug = Suggester(trie)
    for prefix in ["a", "al", "alp", "the", "the t", "b", "zur", "ZÜR", "alias", "x", "abc a", "1"]:
        for limit in (1, 5, 20):
            assert sug.suggest(prefix, limit=limit) == suggest_entries_bruteforce(entries, prefix, limit), (
                prefix,
                limit,
            )


def test_reopen_from_disk(tmp_path: Path) -> None:
    entries = _entries(50)
    TitleTrie.build(entries, tmp_path)
    assert TitleTrie.exists(tmp_path)
    reopened = Suggester(TitleTrie.open(tmp_path))
    assert reopened.suggest("al", limit=5) == suggest_entries_bruteforce(entries, "al", 5)


def test_overlay_upsert_remove_and_dedupe(tmp_path: Path) -> None:
    base = TitleTrie.build(
        [SuggestEntry("wsp:a/1", "Paris", prior=1.0), SuggestEntry("wsp:b/1", "Paris", prior=0.5)], tmp_path
    )
    sug = Suggester(base)
    assert sug.suggest("par") == ["Paris"]  # same title in two sources shown once
    sug.upsert(
        [SuggestEntry("wsp:a/2", "Parma", prior=2.0), SuggestEntry("wsp:a/1", "Paris (city)", prior=1.0)]
    )
    assert sug.suggest("par") == ["Parma", "Paris (city)", "Paris"]
    sug.remove(["wsp:b/1", "wsp:a/2"])
    assert sug.suggest("par") == ["Paris (city)"]
    assert sug.pending == 4
    sug.replace_base(None)
    assert sug.suggest("par") == []


def test_empty_trie_and_prefix(tmp_path: Path) -> None:
    sug = Suggester(TitleTrie.build([], tmp_path))
    assert sug.suggest("a") == []
    assert sug.suggest("") == []
    assert Suggester().suggest("x") == []


def test_normalize_and_sanitize() -> None:
    assert normalize_key("  Zürich\tCAFÉ ") == "zurich cafe"
    assert sanitize_query('foo:bar "x') == "foo bar x"
    assert sanitize_query("a AND b") == "a and b"
    assert sanitize_query('"exact phrase"') == '"exact phrase"'
