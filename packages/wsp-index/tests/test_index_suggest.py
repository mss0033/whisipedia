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


# --- how well known, not how short; the next word (2026-11-10 trawl, search-index-13) ---------------


def _known() -> list[SuggestEntry]:
    return [
        SuggestEntry("wsp:w/en/Kathak", "Kathak", prior=0.990, lang="en"),
        SuggestEntry("wsp:w/en/Kathmandu", "Kathmandu", prior=0.999, lang="en"),
        SuggestEntry("wsp:w/en/Kath", "Kath", prior=0.5, lang="en"),
        SuggestEntry("wsp:w/en/Kathmandu_Valley", "Kathmandu Valley", prior=0.9905, lang="en"),
        SuggestEntry("wsp:w/en/Nepal", "Nepal", ("Kathmandu kingdom",), prior=0.9995, lang="en"),
    ]


@pytest.mark.parametrize("heavy_threshold", [2, 512])
def test_a_well_known_title_comes_before_a_shorter_lesser_one(tmp_path: Path, heavy_threshold: int) -> None:
    """The stored score is prior + 1/(1+len): Kathak 0.990 + 1/7 = 1.133 beat Kathmandu 0.999 + 1/10 =
    1.099, and any 6-letter title beat a 9-letter one unless its prior was 0.04 higher."""
    entries = _known()
    sug = Suggester(TitleTrie.build(entries, tmp_path, heavy_threshold=heavy_threshold, top_k=8))
    assert sug.suggest("kathm", lang="en") == ["Kathmandu", "Kathmandu Valley", "Kathmandu kingdom"]
    # "Kath" typed in full is a title and leads; then by how well known. Kathak (0.990) stays ahead
    # of Kathmandu Valley (0.9905): 0.0005 apart is a tie the length still breaks.
    assert sug.suggest("kath", lang="en") == [
        "Kath",
        "Kathmandu",
        "Kathak",
        "Kathmandu Valley",
        "Kathmandu kingdom",
    ]
    assert sug.suggest("kat", lang="en")[:2] == ["Kathmandu", "Kathak"]  # the finding: not Kathak first
    for prefix in ("k", "kath", "kathm", "kathmandu "):
        assert sug.suggest(prefix, lang="en") == suggest_entries_bruteforce(entries, prefix, 10, "en"), prefix
    overlay = Suggester(TitleTrie.build([], tmp_path / "empty"))
    overlay.upsert(entries)
    assert overlay.suggest("kath", lang="en") == sug.suggest("kath", lang="en")


def test_length_still_breaks_ties(tmp_path: Path) -> None:
    entries = [
        SuggestEntry("wsp:w/en/Paris_Commune", "Paris Commune", prior=0.9, lang="en"),
        SuggestEntry("wsp:w/en/Paris", "Paris", prior=0.9, lang="en"),
        SuggestEntry("wsp:w/en/Parish", "Parish", prior=0.9, lang="en"),
    ]
    sug = Suggester(TitleTrie.build(entries, tmp_path))
    assert sug.suggest("par", lang="en") == ["Paris", "Parish", "Paris Commune"]
    # ... and only near-ties: 0.005 of the prior outweighs these lengths (the whole term is under 0.01)
    entries[0] = SuggestEntry("wsp:w/en/Paris_Commune", "Paris Commune", prior=0.905, lang="en")
    sug = Suggester(TitleTrie.build(entries, tmp_path / "b"))
    assert sug.suggest("par", lang="en")[0] == "Paris Commune"


def test_the_rank_score_is_the_prior_and_a_small_length_term() -> None:
    from wsp_index.suggest import ALIAS_PENALTY, LENGTH_WEIGHT, Candidate, _rank_score, key_score  # pyright: ignore[reportPrivateUsage]

    title = Candidate(key_score(0.7, "Kathmandu"), "kathmandu", "Kathmandu", "", "en", "wsp:a/1")
    assert _rank_score(title) == pytest.approx(0.7 + LENGTH_WEIGHT / 10)
    alias = Candidate(
        key_score(0.7, "Nepal", "Kathmandu kingdom"),
        "kathmandu kingdom",
        "Nepal",
        "Kathmandu kingdom",
        "en",
        "wsp:a/2",
    )
    assert alias.score < 0  # stored ALIAS_PENALTY lower
    assert ALIAS_PENALTY == 2.0
    assert _rank_score(alias) == pytest.approx(0.7 + LENGTH_WEIGHT / 18)  # by the alias shown, no penalty


@pytest.mark.parametrize("heavy_threshold", [2, 512])
def test_a_trailing_space_asks_for_the_next_word(tmp_path: Path, heavy_threshold: int) -> None:
    """ "alan " was normalized to "alan" and offered Alans and Alanya."""
    entries = [
        SuggestEntry("wsp:w/en/Alans", "Alans", prior=0.99, lang="en"),
        SuggestEntry("wsp:w/en/Alanya", "Alanya", prior=0.98, lang="en"),
        SuggestEntry("wsp:w/en/Alan", "Alan", prior=0.5, lang="en"),
        SuggestEntry("wsp:w/en/Alan_Turing", "Alan Turing", prior=0.97, lang="en"),
        SuggestEntry("wsp:w/en/Alan_Kay", "Alan Kay", prior=0.9, lang="en"),
        SuggestEntry("wsp:w/en/Given_name", "Given name", ("Alan",), prior=0.2, lang="en"),
        SuggestEntry("wsp:w/en/Fog", "Fog", ("Alan fog",), prior=0.95, lang="en"),
    ]
    sug = Suggester(TitleTrie.build(entries, tmp_path, heavy_threshold=heavy_threshold, top_k=3))
    # the page titled just so, then the word's next words by how well known; never "Alans". The page
    # only named "Alan" shows the same text and is dropped; a label only begun comes last.
    assert sug.suggest("alan ", lang="en") == ["Alan", "Alan Turing", "Alan Kay", "Alan fog"]
    assert sug.suggest("Alan\t", lang="en") == sug.suggest("alan ", lang="en")  # any trailing whitespace
    assert sug.suggest(" alan", lang="en")[1] == "Alans"  # only a trailing one: this is "alan"
    assert sug.suggest("alan", lang="en")[:3] == ["Alan", "Alans", "Alanya"]  # without it: every completion
    assert sug.suggest("alan t ", lang="en") == []  # "alan t" is no word of a title
    assert sug.suggest("alan turing ", lang="en") == ["Alan Turing"]  # typed in full
    assert sug.suggest(" ", lang="en") == []
    for prefix in ("alan ", "alan", "al ", "alan turing ", "alan  "):
        assert sug.suggest(prefix, lang="en") == suggest_entries_bruteforce(entries, prefix, 10, "en"), prefix
    overlay = Suggester(TitleTrie.build([], tmp_path / "empty"))
    overlay.upsert(entries)
    assert overlay.suggest("alan ", lang="en") == sug.suggest("alan ", lang="en")


def test_what_was_typed_in_full_leads_however_many_better_known_titles_begin_with_it(tmp_path: Path) -> None:
    """Under the stored score the shortest completion led. Ranked by the prior alone, "Mercury" fell
    off the list behind nine better-known "Mercury ..." titles, and the pipeline asks this list
    whether a phrase is a title (suggest(phrase, limit=5))."""
    entries = [SuggestEntry("wsp:w/en/Mercury", "Mercury", prior=0.97, lang="en")]
    entries += [
        SuggestEntry(f"wsp:w/en/Mercury_{i}", f"Mercury {i}", prior=0.98, lang="en") for i in range(9)
    ]
    entries += [SuggestEntry("wsp:w/en/Hg", "Quicksilver", ("Mercury",), prior=0.1, lang="en")]
    for heavy in (2, 512):
        sug = Suggester(TitleTrie.build(entries, tmp_path / str(heavy), heavy_threshold=heavy, top_k=4))
        got = sug.suggest_docs("mercury", limit=5, lang="en")
        assert (got[0].text, got[0].doc_id) == ("Mercury", "wsp:w/en/Mercury")
        assert [s.text for s in got] == suggest_entries_bruteforce(entries, "mercury", 5, "en")
        assert sug.suggest("mercury", limit=5)[0] == "Mercury"  # as the pipeline asks, without a language


def test_a_finished_word_is_followed_by_more_than_a_space(tmp_path: Path) -> None:
    """A space typed after a word finishes the word; the title may go on with a comma or a hyphen."""
    entries = [
        SuggestEntry("wsp:w/en/Washington,_D.C.", "Washington, D.C.", prior=0.99, lang="en"),
        SuggestEntry("wsp:w/en/Washingtonia", "Washingtonia", prior=0.9, lang="en"),
        SuggestEntry("wsp:w/en/Jean-Paul_Sartre", "Jean-Paul Sartre", prior=0.9, lang="en"),
        SuggestEntry("wsp:w/en/Jeans", "Jeans", prior=0.95, lang="en"),
        SuggestEntry(
            "wsp:w/en/Alan's_Psychedelic_Breakfast", "Alan's Psychedelic Breakfast", prior=0.5, lang="en"
        ),
        SuggestEntry("wsp:w/en/USA", "U.S.A. of America", prior=0.5, lang="en"),
        SuggestEntry("wsp:w/ja/東京都", "東京都", prior=0.9, lang="ja"),
        SuggestEntry("wsp:w/ja/東京", "東京", prior=0.8, lang="ja"),
        SuggestEntry("wsp:w/en/Iran\u2013Iraq_War", "Iran\u2013Iraq War", prior=0.9, lang="en"),
        SuggestEntry("wsp:w/en/Iranian_rial", "Iranian rial", prior=0.95, lang="en"),
        SuggestEntry("wsp:w/en/Yahoo!_Mail", "Yahoo! Mail", prior=0.9, lang="en"),
    ]
    for heavy in (2, 512):
        sug = Suggester(TitleTrie.build(entries, tmp_path / str(heavy), heavy_threshold=heavy, top_k=3))
        # a dash or an exclamation mark ends a word too ("iran " lost the war; verification)
        assert sug.suggest("iran ") == ["Iran\u2013Iraq War"]
        assert sug.suggest("yahoo ") == ["Yahoo! Mail"]
        assert sug.suggest("washington ") == ["Washington, D.C."]
        assert sug.suggest("jean ") == ["Jean-Paul Sartre"]
        assert sug.suggest("alan ") == ["Alan's Psychedelic Breakfast"]
        assert sug.suggest("u.s.a ") == ["U.S.A. of America"]
        # a script written without spaces: the space an input method leaves finishes no word
        assert sug.suggest("東京 ") == ["東京", "東京都"]
        assert sug.suggest("東京\u3000") == ["東京", "東京都"]
        for prefix in ("washington ", "jean ", "alan ", "u.s.a ", "東京 ", "iran ", "yahoo "):
            assert sug.suggest(prefix) == suggest_entries_bruteforce(entries, prefix, 10), prefix


def test_a_language_that_is_none_prefers_none(tmp_path: Path) -> None:
    """``mul`` made every tagged page foreign and put the untagged ones first, whatever their prior."""
    entries = [
        SuggestEntry("wsp:a/Photon", "Photon", prior=0.1, lang=""),
        SuggestEntry("wsp:a/Phobos", "Phobos", prior=0.05, lang="und"),
        SuggestEntry("wsp:w/de/Photosynthese", "Photosynthese", prior=0.98, lang="de"),
        SuggestEntry("wsp:w/en/Phoenix", "Phoenix", prior=0.5, lang="en"),
    ]
    sug = Suggester(TitleTrie.build(entries, tmp_path))
    mixed = ["Photosynthese", "Phoenix", "Photon", "Phobos"]
    assert sug.suggest("pho") == mixed
    for none in ("mul", "MUL", "und", "zxx", " "):
        assert sug.suggest("pho", lang=none) == mixed, none
    assert sug.suggest("pho", lang="en") == ["Phoenix", "Photon", "Phobos", "Photosynthese"]
