"""'Did you mean': symmetric-delete candidates, the noisy-channel choice, the vocabulary file."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from wsp_index import RefineryReader, RefineryWriter
from wsp_index.refinery import generation_dir
from wsp_index.spelling import (
    ReloadingSpeller,
    SpellConfig,
    Speller,
    build_vocabulary,
    deletes,
    osa_distance,
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
        for i in range(3)
    ] + [doc("enc", "X", "Misc", "A singular typo: immuen.")]
    with RefineryWriter(tmp_path / "ref", "enc", "g1") as w:
        w.write(docs)
    reader = RefineryReader(generation_dir(tmp_path / "ref", "enc", "g1"))
    path = tmp_path / "lexical" / "spelling.bin"
    stats = build_vocabulary([("enc", reader)], path, SpellConfig(min_count=3, min_count_docs=0))
    assert stats.documents == 4
    assert "words from 4 documents" in stats.line()
    s = Speller.open(path)
    assert s is not None
    assert s.count("vaccine") == 3 * 5  # titles count five times
    assert s.count("immune") == 6
    assert s.count("immuen") == 0  # below min_count: corpus typos stay out
    assert s.correct("imune sytem") == "immune system"
    capped = build_vocabulary([("enc", reader)], tmp_path / "c.bin", SpellConfig(min_count=1, max_words=2))
    assert capped.words == 2


def test_file_errors_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "spelling.bin"
    assert Speller.open(path) is None
    write_vocabulary(path, {"apple": 5})
    data = path.read_bytes()
    path.write_bytes(data[:-1])
    assert Speller.open(path) is None
    path.write_bytes(b"nonsense" * 10)
    assert Speller.open(path) is None
    empty = Speller.from_counts({})
    assert empty.correct("anything") is None
    assert len(empty) == 0

    write_vocabulary(path, {"apple": 5})
    live = ReloadingSpeller(path, every=0.0)
    assert live.correct("appel") == "apple"
    write_vocabulary(path, {"orange": 5})
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    time.sleep(0.01)
    assert live.correct("ornage") == "orange"
    assert live.unknown("apple") == ["apple"]
    gone = ReloadingSpeller(tmp_path / "missing.bin")
    assert gone.correct("x") is None
    assert gone.unknown("abc") == []
