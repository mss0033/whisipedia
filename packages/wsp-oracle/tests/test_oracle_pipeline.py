"""The pipeline end to end on the fixture corpus (CPU, fake models), embed builds and the eval harness."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from oracle_helpers import CORPUS, FIXTURE_GOLD, OverlapReranker, doc

from wsp_core.ids import DocId
from wsp_core.model import DocRecord, SearchHit
from wsp_index import HashingEmbedder, RefineryReader, RefineryWriter, TantivyConfig, TantivyIndex
from wsp_index.refinery import generation_dir
from wsp_oracle.config import OracleConfig, OracleConfigError
from wsp_oracle.embedbuild import EmbedStats, embed_generation
from wsp_oracle.evaluation import (
    corrected,
    evaluate,
    load_gold,
    ndcg_at,
    parse_weights,
    percentile,
    rank_ablation,
    recall_at,
    replace_searcher,
    spelling_ablation,
    typo,
)
from wsp_oracle.pipeline import OracleIndex, OracleSearcher
from wsp_oracle.router import Intent, Router
from wsp_oracle.services import build_oracle
from wsp_oracle.vectors import ChunkVectorStore, DocVectorIndex

SMALL = TantivyConfig(writer_heap_bytes=20_000_000, writer_threads=1)
DIMS = 256


@pytest.fixture
def lexical(tmp_path: Path) -> Iterator[TantivyIndex]:
    idx = TantivyIndex(tmp_path / "lexical", SMALL)
    idx.upsert(CORPUS)
    idx.commit()
    yield idx
    idx.close()


def write_generation(root: Path, gen: str, docs: tuple[DocRecord, ...]) -> RefineryReader:
    by_source: dict[str, list[DocRecord]] = {}
    for d in docs:
        by_source.setdefault(d.source_id, []).append(d)
    for source, recs in by_source.items():
        with RefineryWriter(root, source, gen) as w:
            w.write(recs)
    return RefineryReader(generation_dir(root, "encyclo", gen))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ChunkVectorStore]:
    s = ChunkVectorStore(tmp_path / "vectors", DIMS)
    root = tmp_path / "refinery"
    write_generation(root, "g1", CORPUS)
    emb = HashingEmbedder(DIMS)
    for source in ("encyclo", "dictionary", "rfc"):
        reader = RefineryReader(generation_dir(root, source, "g1"))
        embed_generation(reader, s, emb, source_id=source, generation_id="g1", chunk_words=50, min_words=0)
    yield s
    s.close()


def make_searcher(lexical: TantivyIndex, store: ChunkVectorStore, *, rerank: bool = True) -> OracleSearcher:
    return OracleSearcher(
        lexical,
        router=Router.from_sources(["encyclo", "dictionary", "rfc"]),
        embedder=HashingEmbedder(DIMS),
        vectors=DocVectorIndex(store),
        reranker=OverlapReranker() if rerank else None,
        resolve=lexical.lookup,
        passage_text=store.texts,
    )


def test_embed_generation_is_resumable_and_incremental(tmp_path: Path) -> None:
    root = tmp_path / "refinery"
    reader = write_generation(root, "g1", CORPUS)
    s = ChunkVectorStore(tmp_path / "v", DIMS)
    emb = HashingEmbedder(DIMS)
    seen: list[EmbedStats] = []
    first = embed_generation(
        reader, s, emb, source_id="encyclo", generation_id="g1", progress=seen.append, min_words=0
    )
    assert first.docs_embedded == 8
    assert seen
    assert "encyclo" in seen[-1].line()
    again = embed_generation(reader, s, emb, source_id="encyclo", generation_id="g1", min_words=0)
    assert (again.docs_embedded, again.docs_unchanged) == (0, 8)

    changed = tuple(d for d in CORPUS if d.source_id == "encyclo" and d.native_id != "Lisbon")
    changed = (
        *[d for d in changed if d.native_id != "Alligator"],
        doc(
            "encyclo", "Alligator", "Alligator", "Alligators live in freshwater swamps of the American south."
        ),
    )
    r2 = write_generation(root, "g2", changed)
    upd = embed_generation(r2, s, emb, source_id="encyclo", generation_id="g2", min_words=0)
    assert (upd.docs_embedded, upd.docs_unchanged, upd.docs_removed) == (1, 6, 1)
    assert s.doc_count("encyclo") == 7
    assert s.texts(["wsp:encyclo/Lisbon#0"]) == {}
    limited = embed_generation(r2, s, emb, source_id="encyclo", generation_id="g3", limit=2, min_words=0)
    assert limited.docs_seen == 2
    assert limited.docs_removed == 0
    s.close()


def test_pipeline_end_to_end(lexical: TantivyIndex, store: ChunkVectorStore) -> None:
    searcher = make_searcher(lexical, store)
    res = searcher.run("how do vaccines train the immune system")
    assert res.route.intent is Intent.HOW_TO
    assert "wsp:encyclo/Vaccine" in {h.doc_id for h in res.hits[:2]}
    assert "immune" in res.passages["wsp:encyclo/Vaccine"].casefold()
    assert res.confidence is not None
    assert res.confidence > 0.3
    assert not res.low_confidence
    assert {"route", "dense", "lexical+fusion", "rerank", "total"} <= set(res.timings_ms)
    # snippets shown to the user are the lexical ones, not the reranked passages
    assert all("#" not in h.doc_id for h in res.hits)


def test_pins_exact_titles_and_comparisons(lexical: TantivyIndex, store: ChunkVectorStore) -> None:
    searcher = make_searcher(lexical, store, rerank=False)
    assert searcher.run("Alan Turing").hits[0].doc_id == "wsp:encyclo/Alan_Turing"
    assert searcher.run("map of Lisbon").hits[0].doc_id == "wsp:encyclo/Lisbon"
    top2 = {h.doc_id for h in searcher.run("alligator vs crocodile").hits[:2]}
    assert top2 == {"wsp:encyclo/Alligator", "wsp:encyclo/Crocodile"}
    nav = searcher.run("rfc 9111")  # site + identifier: searched only there
    assert {h.source_id for h in nav.hits} == {"rfc"}
    soft = searcher.run("rfc http caching")  # site + words: a soft prior, searched everywhere
    assert soft.scope is None
    assert soft.hits[0].source_id == "rfc"


def test_oracle_index_port_and_cache(lexical: TantivyIndex, store: ChunkVectorStore) -> None:
    rr = OverlapReranker()
    searcher = make_searcher(lexical, store)
    searcher.reranker = rr
    index = OracleIndex(searcher, cache_size=2)
    page1 = index.search("immune system", limit=2)
    page2 = index.search("immune system", limit=2, offset=2)
    assert rr.calls == 1
    assert not {h.doc_id for h in page1} & {h.doc_id for h in page2}
    assert index.search("immune system", sources=[]) == []
    assert index.search("   ") == []
    assert index.suggest("Ali") == lexical.suggest("Ali")
    index.upsert([doc("encyclo", "New", "Newt", "A newt is an amphibian.")])
    index.commit()
    assert index.search("newt amphibian")[0].doc_id == "wsp:encyclo/New"


def test_low_confidence_when_nothing_matches(lexical: TantivyIndex, store: ChunkVectorStore) -> None:
    res = make_searcher(lexical, store).run("zzzz qqqq")
    assert res.low_confidence


def test_eval_harness_on_fixtures(lexical: TantivyIndex, store: ChunkVectorStore) -> None:
    gold = load_gold(FIXTURE_GOLD)
    full = make_searcher(lexical, store)
    lex = replace_searcher(full, dense=False, rerank=False)
    reports = [
        evaluate(name, lambda q, s=s: [h.doc_id for h in s.run(q).hits], gold)
        for name, s in (("lexical", lex), ("full", full))
    ]
    for rep in reports:
        assert rep.queries == 8
        assert rep.ndcg10 >= 0.8, rep.summary()
        assert rep.recall50 >= 0.9
        assert rep.p95_ms < 1000
        assert set(rep.by_domain) >= {"medicine", "geography"}


def test_metrics() -> None:
    rel = {"a": 2, "b": 1}
    assert ndcg_at(["a", "b"], rel) == pytest.approx(1.0)
    assert ndcg_at(["x", "a"], rel) < ndcg_at(["a", "x"], rel)
    assert ndcg_at([], {}) == 0.0
    assert recall_at(["b", "z"], rel) == 0.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    assert percentile([], 95) == 0.0


def test_build_oracle_lexical_only(tmp_path: Path, lexical: TantivyIndex) -> None:
    cfg = {"index": {"root": str(tmp_path)}, "oracle": {"reranker": {"enabled": False}}}
    oracle = build_oracle(cfg, lexical=lexical, sources=["encyclo"])
    assert oracle.vectors is None
    assert oracle.index.search("Lisbon")[0].doc_id == "wsp:encyclo/Lisbon"
    with pytest.raises(OracleConfigError, match="generator"):  # removed by owner decision (ADR-0008)
        OracleConfig.from_mapping({"generator": {"enabled": True}})


def test_config_parsing() -> None:
    cfg = OracleConfig.from_mapping(
        {
            "dense_sources": ["wikipedia-en-zim"],
            "source_priors": {"wikipedia-en-zim": 1.2},
            "embedder": {"device": "cpu", "batch_size": 64},
            "vectors": {"path": "/tmp/v", "mode": "view"},
        }
    )
    assert cfg.dense_sources == ("wikipedia-en-zim",)
    assert cfg.embedder.batch_size == 64
    assert cfg.vector_path(None) == Path("/tmp/v")
    assert cfg.model_path("/abs/model") == Path("/abs/model")
    assert cfg.model_path("rel").name == "rel"
    for bad in (
        {"nope": 1},
        {"embedder": {"device": "tpu"}},
        {"vectors": {"mode": "mmap"}},
        {"depth": 0},
        {"dense_sources": "x"},
        {"embedder": "x"},
        {"depth": "many"},
    ):
        with pytest.raises(OracleConfigError):
            OracleConfig.from_mapping(bad)
    with pytest.raises(OracleConfigError, match=r"index\.root"):
        OracleConfig().vector_path(None)


def test_site_scope_is_consistent_and_falls_back(lexical: TantivyIndex) -> None:
    router = Router.from_sources(["encyclo", "dictionary", "rfc", "gutenberg"])
    searcher = OracleSearcher(lexical, router=router)
    index = OracleIndex(searcher)
    # site + identifier with an answer there: results, totals and facets all come from it
    res = searcher.run("rfc 9111")
    assert res.scope is not None
    assert (res.scope.site, res.scope.sources, res.scope.fallback) == ("rfc", ("rfc",), False)
    assert {h.source_id for h in res.hits} == {"rfc"}
    window = index.search_hits("rfc 9111", limit=200)
    assert {h.doc_id for h in window} == {h.doc_id for h in index.search("rfc 9111", limit=50)}
    # the site has nothing for the identifier (or nothing indexed): the whole library, flagged
    for q in ("rfc 9999", "gutenberg 1342"):
        scope = index.scope(q)
        assert scope is not None
        assert scope.fallback, q
        hits = index.search(q, limit=10)
        assert {h.doc_id for h in index.search_hits(q, limit=200)} >= {h.doc_id for h in hits}
    # a site with words is a soft prior, never a scope: other sources still answer
    assert index.scope("rfc alan turing") is None
    assert "wsp:encyclo/Alan_Turing" in {h.doc_id for h in index.search("gutenberg alan turing", limit=10)}
    # explicit filters win over the router; plain queries have no scope
    assert index.scope("rfc 9111", sources=["encyclo"]) is None
    assert index.scope("alan turing") is None
    assert index.search_hits("rfc 9111", sources=[]) == []


def test_typos_are_deterministic_single_edits() -> None:
    from wsp_index.spelling import osa_distance

    q = "how do vaccines train the immune system"
    variants = {typo(q, k) for k in range(6)}
    assert len(variants) > 1
    for v in variants:
        assert v != q
        assert v.split()[2][0] == "v"  # the longest word is edited, never its first letter
        assert osa_distance(q, v, 2) == 1
    assert typo("rfc 9110", 3) == "rfc 9110"  # nothing long enough to misspell
    assert typo(q, 4) == typo(q, 4)
    assert parse_weights("0/0, 1/0.1,2") == [(0.0, 0.0), (1.0, 0.1), (2.0, 2.0)]


def test_rank_and_spelling_ablations_on_fixtures(tmp_path: Path, lexical: TantivyIndex) -> None:
    from wsp_index.linkrank import compute_static_rank
    from wsp_index.spelling import Speller

    gold = load_gold(FIXTURE_GOLD)
    root = tmp_path / "refinery"
    write_generation(root, "g1", CORPUS)
    readers = [(s, RefineryReader(generation_dir(root, s, "g1"))) for s in ("dictionary", "encyclo", "rfc")]
    compute_static_rank(readers, lexical.rank_path, tmp_path / "work")
    assert lexical.reload_static_rank() is not None
    base = OracleSearcher(lexical, router=Router.from_sources(["encyclo", "dictionary", "rfc"]))
    reports = rank_ablation("lexical", base, gold, [(0, 0), (1, 0.1)])
    assert [r.name for r in reports] == ["lexical pr=0/0", "lexical pr=1/0.1"]
    assert all(r.ndcg10 >= 0.8 for r in reports)
    assert lexical.config.rank_weight == 1

    speller = Speller.from_texts(f"{d.title} {d.text}" for d in CORPUS)
    sp = spelling_ablation("lexical", lambda q: [h.doc_id for h in base.run(q).hits], gold, speller)
    assert sp.changed_by_typo == len(gold) - 1  # "why is the sky blue" has no 5+ letter word
    assert sp.restored >= sp.changed_by_typo - 2
    assert sp.typos_corrected.ndcg10 >= sp.typos.ndcg10
    assert sp.typos_corrected.ndcg10 >= 0.8
    assert "restored=" in sp.summary()
    assert corrected(speller, "alligatr vs crocodile") == "alligator vs crocodile"
    assert corrected(speller, "alligator vs crocodile") == "alligator vs crocodile"


class _Many:
    """A lexical index with 100 matches for anything, best first."""

    def __init__(self) -> None:
        self.limits: list[int] = []

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: object = None
    ) -> list[SearchHit]:
        self.limits.append(limit)
        hits = [
            SearchHit(DocId(f"wsp:encyclo/d{i:03}"), 100.0 - i, f"d{i}", "", "encyclo") for i in range(100)
        ]
        return hits[offset : offset + limit]


def test_deep_pages_after_a_shallow_one_are_not_cut_short() -> None:
    lex = _Many()
    searcher = OracleSearcher(lex)  # type: ignore[arg-type]
    searcher.rerank_top, searcher.fusion = 30, replace(searcher.fusion, depth=30)
    index = OracleIndex(searcher)
    assert len(index.search("anything", limit=11)) == 11  # computed 30 deep
    assert [h.title for h in index.search("anything", limit=10, offset=30)] == [
        f"d{i}" for i in range(30, 40)
    ]


def test_first_results_do_not_depend_on_the_page_size() -> None:
    lex = _Many()
    searcher = OracleSearcher(lex, reranker=OverlapReranker())  # type: ignore[arg-type]
    one = searcher.run("anything", depth=1).hits[0]
    ten = searcher.run("anything", depth=10).hits[0]
    assert one.doc_id == ten.doc_id
    assert searcher.depth_for(1) == searcher.depth_for(10) == searcher.depth_for(None)
