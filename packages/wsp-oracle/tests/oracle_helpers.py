"""Shared fixtures for wsp-oracle tests: a tiny corpus, fake models, GPU/model availability."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import DocRecord, SearchHit
from wsp_oracle import devices
from wsp_oracle.config import DEFAULT_EMBED_MODEL, DEFAULT_MODELS_DIR, DEFAULT_RERANK_MODEL

MODELS = Path(DEFAULT_MODELS_DIR).expanduser()
FIXTURE_GOLD = Path(__file__).resolve().parents[1] / "eval" / "gold" / "fixture.jsonl"


def doc(source: str, native: str, title: str, text: str) -> DocRecord:
    return DocRecord(
        doc_id=make_doc_id(source, native),
        source_id=source,
        native_id=native,
        title=title,
        lang="en",
        license="CC0-1.0",
        text=text,
        content_hash=content_hash(text),
    )


CORPUS: tuple[DocRecord, ...] = (
    doc(
        "encyclo",
        "Vaccine",
        "Vaccine",
        "Vaccine\nA vaccine is a biological preparation that provides active acquired immunity.\n"
        "Mechanism\nThe vaccine trains the immune system to recognize a pathogen. Memory cells remain "
        "after vaccination, so a later infection is fought off quickly.\nReferences\nSmith 2001.",
    ),
    doc(
        "encyclo",
        "Immune_system",
        "Immune system",
        "The immune system is a network of biological processes that protects an organism from diseases. "
        "It detects pathogens such as viruses and bacteria.",
    ),
    doc(
        "encyclo",
        "Lisbon",
        "Lisbon",
        "Lisbon is the capital and largest city of Portugal, on the Tagus estuary.",
    ),
    doc(
        "encyclo",
        "Rayleigh_scattering",
        "Rayleigh scattering",
        "Rayleigh scattering of sunlight by air molecules is why the sky is blue: short blue wavelengths "
        "scatter much more strongly than red ones.",
    ),
    doc("encyclo", "Alligator", "Alligator", "An alligator is a large reptile of the Americas and China."),
    doc("encyclo", "Crocodile", "Crocodile", "Crocodiles are large aquatic reptiles of the tropics."),
    doc("encyclo", "Alan_Turing", "Alan Turing", "Alan Turing was a mathematician and computer scientist."),
    doc(
        "encyclo",
        "Turing_machine",
        "Turing machine",
        "A Turing machine is a model of computation by Alan Turing.",
    ),
    doc(
        "dictionary",
        "serendipity",
        "serendipity",
        "serendipity: finding something good without looking for it.",
    ),
    doc(
        "rfc",
        "9111",
        "HTTP Caching",
        "RFC 9111 defines HTTP caches and the header fields that control them.",
    ),
)

_WORD = re.compile(r"\w+")


def overlap(a: str, b: str) -> float:
    wa = {w.casefold() for w in _WORD.findall(a) if len(w) > 2}
    wb = {w.casefold() for w in _WORD.findall(b) if len(w) > 2}
    return len(wa & wb) / len(wa) if wa else 0.0


class OverlapReranker:
    """Deterministic stand-in for the cross-encoder: query-word overlap with title + passage."""

    def __init__(self) -> None:
        self.calls = 0

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        return [overlap(query, p) for p in passages]

    def rerank(self, query: str, hits: Sequence[SearchHit]) -> Sequence[SearchHit]:
        self.calls += 1
        scored = [replace(h, score=overlap(query, f"{h.title} {h.snippet}")) for h in hits]
        return sorted(scored, key=lambda h: -h.score)


def needs_model(name: str) -> pytest.MarkDecorator:
    ok = devices.cuda_available() and (MODELS / name).is_dir()
    return pytest.mark.skipif(not ok, reason=f"needs a CUDA GPU and {MODELS / name}")


needs_embed_model = needs_model(DEFAULT_EMBED_MODEL)
needs_rerank_model = needs_model(DEFAULT_RERANK_MODEL)
