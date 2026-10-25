"""'Did you mean': symmetric-delete candidates, the noisy-channel choice, the vocabulary file."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
import pytest

from wsp_index import RefineryReader, RefineryWriter
from wsp_index.refinery import generation_dir
from wsp_index.spelling import (
    Correction,
    ReloadingSpeller,
    SpellConfig,
    Speller,
    build_vocabulary,
    deletes,
    encode_vocabularies,
    osa_distance,
    query_words,
    word_hash,
    write_vocabulary,
)
from wsp_index_helpers import doc

COUNTS = {
    "vaccine": 500,
    "vaccines": 300,
    "immune": 800,
    "system": 5000,
    "turing": 200,
    "during": 9000,
    "lisbon": 150,
    "isbn": 9000,
    "portugal": 400,
    "capital": 2000,
    "the": 100000,
    "relativity": 120,
    "einstein": 300,
    "form": 3000,
    "from": 80000,
    "scattering": 60,
}


@pytest.fixture
def speller() -> Speller:
    return Speller.from_counts(COUNTS)


def test_edit_distance_and_deletes() -> None:
    assert osa_distance("teh", "the", 2) == 1  # transposition is one edit
    assert osa_distance("vaccinne", "vaccine", 2) == 1
    assert osa_distance("abc", "xyz", 2) == 3  # capped at limit + 1
    assert osa_distance("a", "abcd", 1) == 2
    assert deletes("abc", 1, 7) == {"abc", "ab", "ac", "bc"}
    assert "abcdefg" in deletes("abcdefghij", 2, 7)  # only the prefix gets variants


@pytest.mark.parametrize(
    ("query", "fixed"),
    [
        ("vaccinne immune sytem", "vaccine immune system"),
        ("einstien relativty", "einstein relativity"),
        ("teh capital of portugl", "the capital of portugal"),
        ("Lisbn", "Lisbon"),  # first-letter edits cost extra: not "Isbn"
        ("TURNIG", "TURING"),  # capitalization is kept
        ("rayleigh scatering", "rayleigh scattering"),  # unknown words without a candidate stay
    ],
)
def test_corrections(speller: Speller, query: str, fixed: str) -> None:
    assert speller.correct(query) == fixed


def test_known_words_and_noise_are_left_alone(speller: Speller) -> None:
    assert speller.correct("the immune system") is None
    assert speller.correct("form") is None  # a real word is not swapped for a more frequent neighbour
    assert speller.correct("rfc 9110") is None  # digits and short words
    assert speller.correct("qqqqqqq zzzz") is None
    assert speller.correct("vaccinne sytem", only=["sytem"]) == "vaccinne system"
    assert speller.unknown("vaccinne immune of 42") == ["vaccinne"]
    assert speller.count("immune") == 800
    assert speller.count("nope") == 0
    best = speller.best("turnig")
    assert best is not None
    assert (best.candidate, best.distance) == ("turing", 1)
    assert {c for c, _, _ in speller.candidates("turnig")} >= {"turing"}


def test_vocabulary_from_the_refinery(tmp_path: Path) -> None:
    docs = [
        doc("enc", f"P{i}", "Vaccine", "Vaccines train the immune system. The immune system remembers.")
        for i in range(20)
    ] + [doc("enc", "X", "Misc", "A singular typo: immuen.")]
    with RefineryWriter(tmp_path / "ref", "enc", "g1") as w:
        w.write(docs)
    reader = RefineryReader(generation_dir(tmp_path / "ref", "enc", "g1"))
    path = tmp_path / "lexical" / "spelling.bin"
    stats = build_vocabulary([("enc", reader)], path, SpellConfig(min_count=3, min_count_docs=0))
    assert stats.documents == 21
    assert "words from 21 documents" in stats.line()
    s = Speller.open(path)
    assert s is not None
    assert s.warm() > 0  # the hash arrays are read ahead on open; 0 only for an in-memory speller
    assert Speller.from_counts({"a": 1}).warm() == 0
    assert s.count("vaccine") == 20 * 5  # titles count five times
    assert s.count("immune") == 40
    assert s.count("immuen") == 0  # below min_count: corpus typos stay out
    assert s.correct("imune sytem") == "immune system"
    capped = build_vocabulary([("enc", reader)], tmp_path / "c.bin", SpellConfig(min_count=1, max_words=2))
    assert capped.words == 2


def test_file_errors_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "spelling.bin"
    assert Speller.open(path) is None
    write_vocabulary(path, {"apple": 500})
    data = path.read_bytes()
    path.write_bytes(data[:-1])
    assert Speller.open(path) is None
    path.write_bytes(b"nonsense" * 10)
    assert Speller.open(path) is None
    empty = Speller.from_counts({})
    assert empty.correct("anything") is None
    assert len(empty) == 0

    write_vocabulary(path, {"apple": 500})
    live = ReloadingSpeller(path, every=0.0)
    assert live.correct("appel") == "apple"
    write_vocabulary(path, {"orange": 500})
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    time.sleep(0.01)
    assert live.correct("ornage") == "orange"
    assert live.unknown("apple") == ["apple"]
    gone = ReloadingSpeller(tmp_path / "missing.bin")
    assert gone.correct("x") is None
    assert gone.unknown("abc") == []


def test_one_tokenizer_for_build_and_query(tmp_path: Path) -> None:
    """Straße, final sigma, Devanagari marks and apostrophes are the same words on both sides."""
    text = "Die Straße. ΛΌΓΟΣ und λόγος. नमस्ते दुनिया. Don\u2019t panic, don't."
    docs = [doc("w", f"P{i}", "Misc", text, lang="xx") for i in range(4)]
    with RefineryWriter(tmp_path / "ref", "w", "g1") as w:
        w.write(docs)
    reader = RefineryReader(generation_dir(tmp_path / "ref", "w", "g1"))
    path = tmp_path / "spelling.bin"
    build_vocabulary([("w", reader)], path, SpellConfig(min_count=1, min_count_docs=0))
    s = Speller.open(path)
    assert s is not None
    for query in ("Straße", "λόγος", "ΛΌΓΟΣ", "नमस्ते", "don't", "Don\u2019t", "DON'T"):
        assert s.unknown(query) == [], query
        assert s.correct(query) is None, query
    assert [w for _, w in query_words("Straße ΛΌΓΟΣ नमस्ते 'quoted' Don\u2019t")] == [
        "straße",
        "λόγοσ",
        "नमस्ते",
        "quoted",
        "don't",
    ]


def test_minimum_count_ratio_for_unknown_words() -> None:
    s = Speller.from_counts(
        {"zzz": 7, "zebra": 20, "insulin": 114, "aspirin": 2_000}, floors={"any": 3}, min_ratio=10
    )
    assert s.correct("zzq") is None  # junk is not replaced by a rare neighbour (short: 300 needed)
    assert s.correct("zebrq") is None  # 20 < 10 * floor
    assert s.correct("insulen") == "insulin"  # 114 >= 30 at one edit
    assert s.correct("asprin") == "aspirin"  # one edit
    assert s.correct("aspxyin") is None  # two edits need 3,000
    big = Speller.from_counts({"aspirin": 5_000}, floors={"any": 3}, min_ratio=10)
    assert big.correct("aspxyin") == "aspirin"


def test_vocabularies_are_chosen_by_script_and_language() -> None:
    s = Speller.from_vocabularies(
        {
            "en": {"world": 5000, "war": 8000, "relativity": 150, "einstein": 250},
            "de": {"weltkrieg": 900, "zweiter": 4000, "relativ": 3000, "einstein": 470},
            "ru": {"москва": 900, "столица": 400, "россии": 3000},
        },
        floors={"en": 3, "de": 3, "ru": 3},
        min_ratio=10,
    )
    assert s.correct("zweitter weltkrieg") == "zweiter weltkrieg"  # German words decide
    assert s.correct("Москва столица росии") == "Москва столица россии"  # Cyrillic: Russian
    assert s.correct("einstien relativty") == "einstein relativity"  # unknown everywhere: English prior
    assert s.unknown("weltkrieg") == []
    mixed = s.choose(["zweiter", "world", "war"])  # English knows two of the three words
    assert mixed is not None
    assert mixed.lang == "en"
    assert s.languages == ["de", "en", "ru"]
    assert s.count("weltkrieg") == 900
    assert s.count("weltkrieg", "en") == 0


def test_file_layout_is_aligned_and_version_1_still_reads(tmp_path: Path) -> None:
    for n in range(1, 9):  # blobs of every length modulo 8
        words = {"w" * (k + 3) + "x" * n: 10 + k for k in range(n)}
        data, _ = encode_vocabularies({"en": words, "de": {"wort": 5}}, SpellConfig(min_count=1))
        s = Speller(np.frombuffer(data, dtype=np.uint8))
        for v in s.vocabs:
            for arr in (v._hash, v._offsets, v._del_hash, v._count, v._del_word):  # pyright: ignore[reportPrivateUsage]
                assert arr.ctypes.data % arr.itemsize == 0
                assert not arr.flags.owndata  # views into the file, never copies
    # a WSPSPEL1 file (blob before the delete index) from the previous release
    order = sorted(["apple", "pear"], key=word_hash)
    blob = b"".join(w.encode() for w in order)
    offsets = np.cumsum([0, *(len(w) for w in order)]).astype(np.uint64)
    dels = sorted((word_hash(v), i) for i, w in enumerate(order) for v in deletes(w, 2, 7))
    v1 = (
        b"WSPSPEL1"
        + np.array([len(order), len(blob), len(dels)], dtype=np.uint64).tobytes()
        + np.array([word_hash(w) for w in order], dtype=np.uint64).tobytes()
        + np.array([500 if w == "apple" else 400 for w in order], dtype=np.uint32).tobytes()
        + offsets.tobytes()
        + blob
        + np.array([h for h, _ in dels], dtype=np.uint64).tobytes()
        + np.array([i for _, i in dels], dtype=np.uint32).tobytes()
    )
    (tmp_path / "v1.bin").write_bytes(v1)
    old = Speller.open(tmp_path / "v1.bin")
    assert old is not None
    assert old.count("apple") == 500
    assert old.correct("appel") == "apple"


def test_vocabulary_sample_is_stratified_by_language(tmp_path: Path) -> None:
    """Languages stored in blocks: each vocabulary comes from its own language's parts, spread out."""
    ceb = [doc("w", f"ceb/{i:05d}", "Kategoriya", "mga lungsod kategoriya", lang="ceb") for i in range(300)]
    topics = ("alpha", "bravo", "charlie")
    en = [
        doc("w", f"en/{i:05d}", "Article", f"insulin diabetes {topics[i // 100]}", lang="en")
        for i in range(300)
    ]
    with RefineryWriter(tmp_path / "ref", "w", "g1", docs_per_file=100, row_group_rows=10) as w:
        w.write([*ceb, *en])
    reader = RefineryReader(generation_dir(tmp_path / "ref", "w", "g1"))
    path = tmp_path / "spelling.bin"
    stats = build_vocabulary(
        [("w", reader)], path, SpellConfig(min_count=1, min_count_docs=0, max_docs=60, min_language_docs=10)
    )
    assert stats.languages.keys() == {"ceb", "en"}
    s = Speller.open(path)
    assert s is not None
    assert s.count("kategoriya", "en") == 0  # no Cebuano in the English vocabulary
    assert s.count("insulin", "en") > 0
    # about 60 of 300 English documents, from every English part (the topics are 100 apart)
    assert stats.documents < 200
    assert all(s.count(t, "en") > 0 for t in topics)


def test_a_rare_language_reads_only_the_row_groups_holding_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2026-10-12: sampling by part read every whole part holding one row of a language, so the hundreds
    # of rare languages of Wiktionary read the dump hundreds of times (hours under the index lock).
    from wsp_index.spelling import sample_language
    from wsp_index.text import language_row_groups

    docs = [doc("w", f"e{i:03d}", f"English {i}", "plain english words") for i in range(200)]
    docs[123] = replace(docs[123], lang="fr")
    docs[124] = replace(docs[124], lang="fr")
    with RefineryWriter(tmp_path / "ref", "w", "g1", row_group_rows=10) as w:
        w.write(docs)
    reader = RefineryReader(generation_dir(tmp_path / "ref", "w", "g1"))
    groups = language_row_groups(reader)
    assert sum(n for _, _, n in groups["fr"]) == 2
    assert len(groups["fr"]) == 1
    assert sum(n for _, _, n in groups["en"]) == 198
    reads: list[int] = []
    real = pq.ParquetFile.read_row_group  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]  # pyarrow-stubs leave some parameter types unknown

    def counting(self: pq.ParquetFile, i: int, *args: Any, **kwargs: Any) -> Any:
        reads.append(i)
        return real(self, i, *args, **kwargs)

    monkeypatch.setattr(pq.ParquetFile, "read_row_group", counting)
    rows = sum(t.num_rows for t in sample_language(reader, groups["fr"], "fr", 1000, ("title",)))
    assert rows == 2
    assert len(reads) == 1


def test_a_scattered_language_reads_a_bounded_number_of_row_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2026-10-25: Open Library holds Abkhaz as 102 records in 102 row groups, each read whole from
    # the NAS (23 minutes); 458 such languages kept the nightly build in this step for 40 hours.
    from wsp_index import spelling

    docs = [doc("w", f"e{i:04d}", f"English {i}", "plain english words") for i in range(400)]
    for i in range(0, 400, 10):
        docs[i] = replace(docs[i], lang="ab", title="Аҧсны", text="аҧсуа бызшәа")  # one per row group
    with RefineryWriter(tmp_path / "ref", "w", "g1", row_group_rows=10) as w:
        w.write(docs)
    reader = RefineryReader(generation_dir(tmp_path / "ref", "w", "g1"))
    cfg = SpellConfig(min_count=1, min_count_docs=0, min_language_docs=5, max_sparse_groups=8)
    reads: list[int] = []
    real = pq.ParquetFile.read_row_group  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def counting(self: pq.ParquetFile, i: int, *args: Any, **kwargs: Any) -> Any:
        reads.append(i)
        return real(self, i, *args, **kwargs)

    monkeypatch.setattr(pq.ParquetFile, "read_row_group", counting)
    stats = build_vocabulary(
        [("w", reader)], tmp_path / "s.bin", cfg, cache=spelling.SpellCache(tmp_path / "c")
    )
    assert stats.languages["ab"] == 3  # its three words, from 8 of its 40 row groups
    assert len(reads) == 40 + 8  # English (9 rows per group: not sparse) is read whole
    s = Speller.open(tmp_path / "s.bin")
    assert s is not None
    assert s.count("бызшәа", "ab") > 0
    reads.clear()
    cache = spelling.SpellCache(tmp_path / "c")
    build_vocabulary([("w", reader)], tmp_path / "again.bin", cfg, cache=cache)
    assert (reads, cache.kept) == ([], 2)  # the thinned sample is what the cache key records


def test_untagged_documents_make_no_vocabulary(tmp_path: Path) -> None:
    # 2026-10-25: an "und" vocabulary knew "diabetis" from untagged documents, won the language choice
    # over English and kept the misspelling.
    docs = [doc("w", f"e{i:03d}", "Diabetes", "diabetes insulin glucose", lang="en") for i in range(60)]
    docs += [doc("w", f"u{i:03d}", "Diabetis", "diabetis insulin", lang="und") for i in range(60)]
    with RefineryWriter(tmp_path / "ref", "w", "g1", row_group_rows=10) as w:
        w.write(docs)
    reader = RefineryReader(generation_dir(tmp_path / "ref", "w", "g1"))
    path = tmp_path / "s.bin"
    stats = build_vocabulary(
        [("w", reader)], path, SpellConfig(min_count=1, min_count_docs=0, min_language_docs=10)
    )
    assert set(stats.languages) == {"en"}
    s = Speller.open(path)
    assert s is not None
    assert s.correct("diabetis insulen") == "diabetes insulin"


def test_a_rare_language_that_knows_the_misspellings_does_not_hide_the_correction() -> None:
    """2026-10-17: the Latin vocabulary (Gutenberg's Latin books) knows "diabetis" and "insulen", so
    it won over English, which knows neither, and "diabetis insulen" got no "Did you mean"."""
    s = Speller.from_vocabularies(
        {
            # the library's sizes: 58M English tokens, 17M Latin ones
            "en": {"diabetes": 9380, "insulin": 7469, "the": 58_000_000},
            "la": {"diabetis": 6, "insulen": 13, "et": 17_000_000},
        },
        floors={"en": 3, "la": 3},
        min_ratio=10,
    )
    assert s.correct("diabetis insulen") == "diabetes insulin"
    # Latin that is Latin stays Latin: no English word is near it
    s2 = Speller.from_vocabularies(
        {"en": {"world": 5000}, "la": {"gallia": 900, "divisa": 400}}, floors={"en": 3, "la": 3}, min_ratio=10
    )
    chosen = s2.choose(["gallia", "divisa"])
    assert chosen is not None
    assert chosen.lang == "la"


def test_a_known_word_picks_the_language_without_fuzzy_searches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fuzzy scoring is for words no language knows: it reads all over a multi-GB file."""
    s = Speller.from_vocabularies(
        {
            "en": {"glacier": 900, "calving": 300},
            "de": {"gletscher": 900},
            "fr": {"glacier": 400},
        },
        floors={"en": 3, "de": 3, "fr": 3},
        min_ratio=10,
    )
    fuzzy: list[str] = []

    def counting(
        real: Callable[[str], tuple[Correction | None, float]],
    ) -> Callable[[str], tuple[Correction | None, float]]:
        def best(word: str) -> tuple[Correction | None, float]:
            fuzzy.append(word)
            return real(word)

        return best

    for v in s.vocabs:
        monkeypatch.setattr(v, "best", counting(v.best))
    assert s.unknown("glacier calving") == []
    assert fuzzy == []
    chosen = s.choose(["glacier", "calving"])
    assert chosen is not None
    assert chosen.lang == "en"


def test_unknown_everywhere_is_corrected_in_the_preferred_language_only() -> None:
    """2026-10-17: with every word unknown, a tiny vocabulary (Lezgian, 338 words) won the language
    choice and nothing was corrected; Polish offered "zza" for "zzq"; all 322 languages were searched."""
    english = {"hypertension": 4520, "medication": 2119, "the": 100_000, "pressure": 9000}
    tiny = {"гъвечӏи": 3, "zz": 2}
    polish = {"zza": 50_000, "nie": 90_000, "jest": 80_000}
    sp = Speller.from_vocabularies({"en": english, "lez": tiny, "pl": polish})
    assert sp.correct("hypertenshun") == "hypertension"
    assert sp.correct("zzq") is None  # Polish knows none of it: it does not get to correct it
    assert sp.correct("nie jest zzq") is None or "zza" in (
        sp.correct("nie jest zzq") or ""
    )  # Polish words: Polish may


def _two_sources(tmp_path: Path, en_text: str = "insulin diabetes") -> list[tuple[str, RefineryReader]]:
    ceb = [doc("c", f"ceb/{i:05d}", "Kategoriya", "mga lungsod kategoriya", lang="ceb") for i in range(60)]
    en = [doc("e", f"en/{i:05d}", "Article", en_text, lang="en") for i in range(60)]
    gen = "g2" if en_text != "insulin diabetes" else "g1"
    for sid, gid, docs in (("c", "g1", ceb), ("e", gen, en)):
        if not generation_dir(tmp_path / "ref", sid, gid).is_dir():
            with RefineryWriter(tmp_path / "ref", sid, gid, row_group_rows=10) as w:
                w.write(docs)
    return [
        ("c", RefineryReader(generation_dir(tmp_path / "ref", "c", "g1"))),
        ("e", RefineryReader(generation_dir(tmp_path / "ref", "e", gen))),
    ]


def test_a_rebuild_keeps_the_vocabularies_whose_sources_did_not_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2026-10-22: every nightly build read every language of every source again (8 hours on the NAS).
    from wsp_index import spelling

    cfg = SpellConfig(min_count=1, min_count_docs=0, min_language_docs=10)
    readers = _two_sources(tmp_path)
    first = tmp_path / "first.bin"
    build_vocabulary(readers, first, cfg, cache=spelling.SpellCache(tmp_path / "cache"))

    reads: list[str] = []
    real_rg = pq.ParquetFile.read_row_group  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    from wsp_index.text import language_row_groups as real_groups

    def counting_rg(self: pq.ParquetFile, i: int, *args: Any, **kwargs: Any) -> Any:
        reads.append("row group")
        return real_rg(self, i, *args, **kwargs)

    def counting_groups(reader: RefineryReader) -> Any:
        reads.append(f"lang column {reader.dir.parent.name}")
        return real_groups(reader)

    monkeypatch.setattr(pq.ParquetFile, "read_row_group", counting_rg)
    monkeypatch.setattr(spelling, "language_row_groups", counting_groups)
    cache = spelling.SpellCache(tmp_path / "cache")
    again = tmp_path / "again.bin"
    build_vocabulary(readers, again, cfg, cache=cache)
    assert reads == []  # nothing read again
    assert cache.kept == 2
    assert again.read_bytes() == first.read_bytes()

    # English changes (a new generation): only English is read and counted again.
    readers = _two_sources(tmp_path, en_text="insulin glucose")
    cache = spelling.SpellCache(tmp_path / "cache")
    changed = tmp_path / "changed.bin"
    build_vocabulary(readers, changed, cfg, cache=cache)
    assert cache.kept == 1
    assert "lang column e" in reads
    assert "lang column c" not in reads
    monkeypatch.undo()
    fresh = tmp_path / "fresh.bin"
    build_vocabulary(readers, fresh, cfg)  # no cache: the reference
    assert changed.read_bytes() == fresh.read_bytes()
    s = Speller.open(changed)
    assert s is not None
    assert s.count("glucose", "en") > 0
    assert s.count("kategoriya", "ceb") > 0
    # The old English generation's map and counts are gone; one map per source and one vocabulary
    # per language remain.
    assert len(list((tmp_path / "cache" / "groups").glob("*.json"))) == 2
    assert len(list((tmp_path / "cache" / "vocab").glob("*.parquet"))) == 2


def test_a_damaged_kept_vocabulary_is_counted_again(tmp_path: Path) -> None:
    from wsp_index import spelling

    cfg = SpellConfig(min_count=1, min_count_docs=0, min_language_docs=10)
    readers = _two_sources(tmp_path)
    first = tmp_path / "first.bin"
    build_vocabulary(readers, first, cfg, cache=spelling.SpellCache(tmp_path / "cache"))
    for path in (tmp_path / "cache" / "vocab").glob("*.parquet"):
        path.write_bytes(b"not parquet")
    for path in (tmp_path / "cache" / "groups").glob("*.json"):
        path.write_text("{", encoding="utf-8")
    cache = spelling.SpellCache(tmp_path / "cache")
    again = tmp_path / "again.bin"
    build_vocabulary(readers, again, cfg, cache=cache)
    assert cache.kept == 0
    assert again.read_bytes() == first.read_bytes()
