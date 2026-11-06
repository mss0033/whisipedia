from __future__ import annotations

import random
from collections.abc import Iterator
from pathlib import Path

import marisa_trie  # pyright: ignore[reportMissingTypeStubs]
import pytest

from wsp_index.suggest import SuggestEntry, Suggester, TitleTrie, suggest_entries_bruteforce
from wsp_index.text import normalize_key, sanitize_query


def _entries(n: int, seed: int = 7) -> list[SuggestEntry]:
    rng = random.Random(seed)
    words = ["al", "alpha", "alps", "beta", "bet", "the", "theory", "zürich", "a", "ab", "abc"]
    out: list[SuggestEntry] = []
    for i in range(n):
        title = " ".join(rng.choice(words) for _ in range(rng.randint(1, 3))) + f" {i}"
        aliases = (f"alias {title}",) if i % 7 == 0 else ()
        lang = ("en", "de", "", "en-GB")[i % 4]
        out.append(SuggestEntry(f"wsp:s/{i}", title, aliases, prior=rng.random() + i * 1e-6, lang=lang))
    return out


def test_trie_matches_bruteforce_including_heavy_prefixes(tmp_path: Path) -> None:
    entries = _entries(600)
    trie = TitleTrie.build(entries, tmp_path, heavy_threshold=8, top_k=6)
    sug = Suggester(trie)
    for prefix in ["a", "al", "alp", "the", "the t", "b", "zur", "ZÜR", "alias", "x", "abc a", "1"]:
        for limit in (1, 5, 20):
            for lang in (None, "en", "de-AT"):
                want = suggest_entries_bruteforce(entries, prefix, limit, lang)
                assert sug.suggest(prefix, limit=limit, lang=lang) == want, (prefix, limit, lang)


def test_heavy_prefixes_survive_the_largest_code_point(tmp_path: Path) -> None:
    """A normalized key containing U+10FFFF has no successor character to bound its branch with."""
    odd = "\U0010ffff"
    entries = [SuggestEntry(f"wsp:s/{i}", f"x{odd}{odd} {i}", prior=float(i)) for i in range(12)]
    entries += [SuggestEntry(f"wsp:t/{i}", f"xy {i}", prior=0.5) for i in range(3)]
    trie = TitleTrie.build(entries, tmp_path, heavy_threshold=4, top_k=3)
    sug = Suggester(trie)
    for prefix in ["x", f"x{odd}", f"x{odd}{odd}", f"x{odd}{odd} 1", "xy"]:
        assert sug.suggest(prefix, limit=5) == suggest_entries_bruteforce(entries, prefix, 5), prefix


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


def test_a_build_replaces_both_tries_at_once(tmp_path: Path) -> None:
    # Audit 2026-10-12 #18: the titles trie and the heavy-prefix trie were replaced one after the other,
    # and a reader reloading between the two paired a new titles trie with the old heavy one.
    from wsp_index.suggest import HEAVY_FILE, POINTER_FILE, TRIE_FILE

    (tmp_path / TRIE_FILE).write_bytes(b"older flat layout")
    first = TitleTrie.build([SuggestEntry("wsp:s/1", "Alpha")], tmp_path)
    stamp = TitleTrie.stamp(tmp_path)
    assert (tmp_path / POINTER_FILE).read_text().strip() == "v000001"
    assert not (tmp_path / TRIE_FILE).exists()  # the flat files are gone
    TitleTrie.build([SuggestEntry("wsp:s/2", "Beta")], tmp_path)
    TitleTrie.build([SuggestEntry("wsp:s/3", "Gamma")], tmp_path)
    assert TitleTrie.stamp(tmp_path) != stamp
    assert sorted(p.name for p in tmp_path.iterdir()) == [POINTER_FILE, "v000002", "v000003"]
    assert all((tmp_path / "v000003" / f).is_file() for f in (TRIE_FILE, HEAVY_FILE))
    assert Suggester(TitleTrie.open(tmp_path)).suggest("g") == ["Gamma"]
    assert Suggester(first).suggest("a") == ["Alpha"]  # a reader of a deleted version keeps its mapping


def test_a_name_finds_the_documents_titled_or_aliased_so(tmp_path: Path) -> None:
    """2026-11-05 deep dive: "heart attack" never reached the article Myocardial infarction; the trie
    holds the redirect, and an exact name lookup finds its target (a prefix is not a name)."""
    base = TitleTrie.build(
        [
            SuggestEntry("wsp:w/en/Myocardial_infarction", "Myocardial infarction", ("Heart attack",), 3.0),
            SuggestEntry("wsp:m/heart-attack", "Heart Attack", prior=1.0),
            SuggestEntry("wsp:w/en/Heart_attack_grill", "Heart Attack Grill", prior=2.0),
            SuggestEntry("wsp:w/de/Herzinfarkt", "Herzinfarkt", ("Heart attack", "Herzanfall"), 0.5),
        ],
        tmp_path,
    )
    sug = Suggester(base)
    assert sug.named("HEART  attack") == [
        ("wsp:w/en/Myocardial_infarction", "Myocardial infarction"),
        ("wsp:m/heart-attack", "Heart Attack"),
        ("wsp:w/de/Herzinfarkt", "Herzinfarkt"),
    ]
    assert sug.named("heart attack", limit=1) == [("wsp:w/en/Myocardial_infarction", "Myocardial infarction")]
    assert sug.named("heart") == []  # a prefix names nothing
    assert sug.named("") == []
    # the overlay: a document upserted since the build, and one removed
    sug.upsert([SuggestEntry("wsp:w/en/Cardiac_arrest", "Cardiac arrest", ("heart attack",), 9.0)])
    sug.remove(["wsp:w/de/Herzinfarkt"])
    assert [d for d, _ in sug.named("heart attack")] == [
        "wsp:w/en/Cardiac_arrest",
        "wsp:w/en/Myocardial_infarction",
        "wsp:m/heart-attack",
    ]


def _library() -> list[SuggestEntry]:
    """English articles and other languages' articles that carry English labels as aliases."""
    return [
        SuggestEntry("wsp:w/zh/磷", "磷", ("Phosphorus", "Phosphor"), prior=0.9, lang="zh"),
        SuggestEntry("wsp:w/zh/摄影", "摄影", ("Photography",), prior=0.9, lang="zh"),
        SuggestEntry("wsp:w/ar/تورنغ", "آلان تورنغ", ("Alan Turing",), prior=0.9, lang="ar"),
        SuggestEntry("wsp:w/en/Phosphorus", "Phosphorus", prior=0.3, lang="en"),
        SuggestEntry("wsp:w/en/Phoenix", "Phoenix", prior=0.2, lang="en"),
        SuggestEntry("wsp:w/de/Photosynthese", "Photosynthese", prior=0.4, lang="de"),
        SuggestEntry("wsp:w/en/Alan_Turing", "Alan Turing", prior=0.5, lang="en"),
        SuggestEntry("wsp:w/en/US", "United States", ("USA", "U.S.A. of America"), prior=0.95, lang="en"),
        SuggestEntry("wsp:w/en/USA_Today", "USA Today", prior=0.1, lang="en"),
        SuggestEntry("wsp:w/en/Usability", "Usability", prior=0.2, lang="en"),
        SuggestEntry("wsp:rfc/9110", "HTTP Semantics", ("phonebook semantics",), prior=0.0),  # no language
    ]


@pytest.mark.parametrize("heavy_threshold", [2, 512])
def test_titles_typed_come_before_labels_and_the_readers_language_first(
    tmp_path: Path, heavy_threshold: int
) -> None:
    """2026-11-05 deep dive: "pho" listed 磷, 摄影 and 音位 (their English labels matched, and a short
    title scored high); "alan tu" put the Arabic article first; picking one searched for "磷"."""
    entries = _library()
    sug = Suggester(TitleTrie.build(entries, tmp_path, heavy_threshold=heavy_threshold, top_k=3))
    got = sug.suggest_docs("pho", limit=8, lang="en")
    # English titles first, then a German title, then what only a label matched, shown as the label.
    assert [(s.text, s.doc_id) for s in got] == [
        ("Phosphorus", "wsp:w/en/Phosphorus"),  # the English article; 磷's "Phosphorus" is the same text
        ("Phoenix", "wsp:w/en/Phoenix"),
        ("phonebook semantics", "wsp:rfc/9110"),  # no language is no one's foreign language
        ("Photosynthese", "wsp:w/de/Photosynthese"),
        ("Phosphor", "wsp:w/zh/磷"),
        ("Photography", "wsp:w/zh/摄影"),
    ]
    assert (got[-1].text, got[-1].title) == ("Photography", "摄影")
    assert sug.suggest("alan tu", lang="en") == ["Alan Turing"]
    assert sug.suggest_docs("alan tu", lang="en")[0].doc_id == "wsp:w/en/Alan_Turing"
    assert sug.suggest_docs("alan tu", lang="ar")[0].doc_id == "wsp:w/ar/تورنغ"
    # An alias typed in full ranks with the titles (a heavy prefix's top list holds titles only).
    assert sug.suggest("usa", lang="en") == ["USA", "Usability", "USA Today"]
    assert sug.suggest("usa", lang="en") == suggest_entries_bruteforce(entries, "usa", 10, "en")
    assert sug.suggest("u.s.a. of", lang="en") == ["U.S.A. of America"]
    # ... also when more titles than the pool holds start with it.
    usage = [SuggestEntry(f"wsp:w/en/Usage_{i}", f"Usage {i}", prior=0.5, lang="en") for i in range(8)]
    more = Suggester(TitleTrie.build([*entries, *usage], tmp_path / "more", heavy_threshold=heavy_threshold))
    assert more.suggest("usa", limit=1, lang="en") == ["USA"]
    for prefix in ("pho", "ph", "alan", "u", "usa", "p"):
        for lang in (None, "en", "zh"):
            want = suggest_entries_bruteforce(entries, prefix, 4, lang)
            assert sug.suggest(prefix, limit=4, lang=lang) == want, (prefix, lang)


def test_the_overlay_ranks_like_the_trie(tmp_path: Path) -> None:
    entries = _library()
    sug = Suggester(TitleTrie.build([], tmp_path))
    sug.upsert(entries)
    for prefix in ("pho", "alan tu", "usa"):
        assert sug.suggest(prefix, lang="en") == suggest_entries_bruteforce(entries, prefix, 10, "en"), prefix


def test_a_trie_of_the_older_layout_is_still_read(tmp_path: Path) -> None:
    """Builds before 2026-11-05 stored ``norm, title, doc_id`` and no FORMAT file: titles only."""
    from wsp_index.suggest import FORMAT_FILE, HEAVY_FILE, POINTER_FILE, SEP, TRIE_FILE

    TitleTrie.build([], tmp_path)
    old = tmp_path / "v000001"
    (old / FORMAT_FILE).unlink()
    keys = [
        (f"phosphor{SEP}磷{SEP}wsp:w/zh/磷", (1.4,)),
        (f"phosphorus{SEP}Phosphorus{SEP}wsp:w/en/Phosphorus", (0.4,)),
    ]
    marisa_trie.RecordTrie("<f", keys).save(str(old / TRIE_FILE))  # pyright: ignore[reportUnknownMemberType]
    marisa_trie.BytesTrie([]).save(str(old / HEAVY_FILE))  # pyright: ignore[reportUnknownMemberType]
    assert (tmp_path / POINTER_FILE).read_text().strip() == "v000001"
    sug = Suggester(TitleTrie.open(tmp_path))
    assert [(s.text, s.doc_id) for s in sug.suggest_docs("phos", lang="en")] == [
        ("磷", "wsp:w/zh/磷"),  # no language stored: never demoted
        ("Phosphorus", "wsp:w/en/Phosphorus"),
    ]


def test_a_heavy_prefix_is_ranked_from_its_precomputed_list(tmp_path: Path) -> None:
    """The ranking pool stops at the precomputed top list: one more candidate scanned the prefix's
    first 4,096 keys on every keystroke."""
    entries = [SuggestEntry(f"wsp:s/{i}", f"Alpha {i}", prior=i / 100, lang="en") for i in range(100)]
    trie = TitleTrie.build(entries, tmp_path, heavy_threshold=8, top_k=32)
    scanned: list[str] = []
    inner = trie._trie  # pyright: ignore[reportPrivateUsage]

    class Spy:
        def __getattr__(self, name: str) -> object:
            return getattr(inner, name)

        def __getitem__(self, key: str) -> list[tuple[float]]:
            return inner[key]

        def iterkeys(self, prefix: str = "") -> Iterator[str]:
            scanned.append(prefix)
            return inner.iterkeys(prefix)

    trie._trie = Spy()  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    sug = Suggester(trie)
    assert sug.suggest("alpha", limit=10, lang="en") == suggest_entries_bruteforce(entries, "alpha", 10, "en")
    assert scanned == ["alpha\x1f"]  # the exact lookup only, no scan past the top list
