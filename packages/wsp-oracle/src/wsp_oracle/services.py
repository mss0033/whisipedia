"""``build_oracle(config) -> OracleServices``: the Oracle's factory for the composition root.

Everything degrades gracefully: no vector store (or no ``wsp-oracle[gpu]``) → lexical + router
only; no GPU → no reranker unless ``reranker.cpu_fallback``; ``generator.enabled = false`` → no
summaries. Each decision is logged once at startup. Nothing here opens a network connection:
models load from local paths with the Hugging Face libraries in offline mode, and the generator
only talks to a loopback llama.cpp server.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from wsp_core.ports import Embedder, Generator, LexicalIndex, Reranker
from wsp_index import FusionConfig
from wsp_oracle import devices
from wsp_oracle.config import OracleConfig
from wsp_oracle.embedder import TransformerEmbedder
from wsp_oracle.generator import CitedGenerator, LlamaServerGenerator, Summarizer
from wsp_oracle.pipeline import OracleIndex, OracleSearcher, Resolver
from wsp_oracle.reranker import CrossEncoderReranker
from wsp_oracle.router import Router
from wsp_oracle.vectors import META_FILE, ChunkVectorStore, DocVectorIndex

log = logging.getLogger("wsp_oracle")


@dataclass(frozen=True, slots=True)
class OracleServices:
    """Ports for the composition root. ``index`` replaces the API's ``Services.index``."""

    config: OracleConfig
    index: OracleIndex
    searcher: OracleSearcher
    router: Router
    embedder: Embedder | None
    vectors: ChunkVectorStore | None
    reranker: Reranker | None
    generator: Generator | None
    summarizer: Summarizer | None

    def close(self) -> None:
        if self.vectors is not None:
            self.vectors.close()
        self.index.close()


def open_embedder(cfg: OracleConfig, *, device: str | None = None) -> TransformerEmbedder:
    e = cfg.embedder
    return TransformerEmbedder(
        cfg.model_path(e.model),
        preset=e.preset,
        device=device or e.device,
        batch_size=e.batch_size,
        max_tokens=e.max_tokens,
        dimensions=e.dimensions,
    )


def open_vector_store(
    cfg: OracleConfig,
    index_root: Path | None,
    *,
    dimensions: int,
    path: Path | None = None,
    mode: str | None = None,
) -> ChunkVectorStore:
    v = cfg.vectors
    return ChunkVectorStore(
        path or cfg.vector_path(index_root),
        dimensions,
        model=Path(cfg.embedder.model).name,
        mode=mode or v.mode,
        connectivity=v.connectivity,
        expansion_add=v.expansion_add,
        expansion_search=v.expansion_search,
        oversample=v.oversample,
        store_text=v.store_text,
    )


def _index_root(cfg: Mapping[str, Any]) -> Path | None:
    index = cfg.get("index", {})
    root = cast(Mapping[str, Any], index).get("root") if isinstance(index, Mapping) else None
    return Path(str(root)).expanduser() if root else None


def _open_lexical(cfg: Mapping[str, Any], index_root: Path | None) -> LexicalIndex:
    from wsp_index import TantivyConfig, TantivyIndex

    if index_root is None:
        raise ValueError("index.root is not set and no lexical index was given")
    index = cfg.get("index", {})
    langs = cast(Mapping[str, Any], index).get("languages", ["en"]) if isinstance(index, Mapping) else ["en"]
    return TantivyIndex(index_root / "lexical", TantivyConfig(languages=tuple(str(x) for x in langs)))


def build_oracle(
    cfg: Mapping[str, Any],
    *,
    lexical: LexicalIndex | None = None,
    sources: Sequence[str] = (),
    resolve: Resolver | None = None,
) -> OracleServices:
    """Build the Oracle from the full layered config (reads ``[oracle]`` and ``[index]``).

    ``lexical`` is the already-open lexical index (opened from ``index.root`` when omitted);
    ``sources`` are the library's source ids (they name the sites the router recognizes).
    """
    section = cfg.get("oracle", {})
    ocfg = OracleConfig.from_mapping(cast(Mapping[str, Any], section) if isinstance(section, Mapping) else {})
    index_root = _index_root(cfg)
    lexical = lexical or _open_lexical(cfg, index_root)
    if resolve is None:
        lookup = getattr(lexical, "lookup", None)
        resolve = cast(Resolver, lookup) if callable(lookup) else None
    router = Router.from_sources(sources)

    embedder: TransformerEmbedder | None = None
    store: ChunkVectorStore | None = None
    vec_path = ocfg.vector_path(index_root) if (ocfg.vectors.path or index_root) else None
    if ocfg.enabled and vec_path is not None and (vec_path / META_FILE).is_file():
        if devices.has_torch():
            embedder = open_embedder(ocfg)
            store = open_vector_store(ocfg, index_root, dimensions=embedder.dimensions, mode="view")
            log.info("oracle: dense retrieval on (%d passages, %s)", len(store), embedder.device)
        else:
            log.warning("oracle: vectors exist but wsp-oracle[gpu] is not installed; dense retrieval off")
    else:
        log.info("oracle: no vector store at %s; lexical retrieval only", vec_path)

    reranker: CrossEncoderReranker | None = None
    rr = ocfg.reranker
    if ocfg.enabled and rr.enabled and devices.has_torch():
        if devices.cuda_available() or rr.cpu_fallback:
            device = rr.device if rr.device != "auto" or devices.cuda_available() else "cpu"
            reranker = CrossEncoderReranker(
                ocfg.model_path(rr.model), device=device, max_tokens=rr.max_tokens, batch_size=rr.batch_size
            )
        else:
            log.info("oracle: no GPU; reranking skipped (reranker.cpu_fallback = false)")

    fusion = FusionConfig(
        k=ocfg.rrf_k,
        lexical_weight=ocfg.lexical_weight,
        dense_weight=ocfg.dense_weight,
        depth=ocfg.depth,
        source_priors=dict(ocfg.source_priors),
    )
    searcher = OracleSearcher(
        lexical,
        router=router,
        embedder=embedder,
        vectors=DocVectorIndex(store) if store is not None else None,
        reranker=reranker,
        fusion=fusion,
        rerank_top=rr.top_k,
        resolve=resolve,
        passage_text=store.texts if store is not None else None,
        budget_ms=ocfg.budget_ms,
    )
    summarizer: Summarizer | None = None
    generator: Generator | None = None
    g = ocfg.generator
    if ocfg.enabled and g.enabled:
        llm = LlamaServerGenerator(g.url, timeout_s=g.timeout_s, temperature=g.temperature)
        summarizer = Summarizer(
            llm,
            scorer=reranker.score if reranker is not None else None,
            min_confidence=g.min_confidence,
            passage_chars=g.passage_chars,
            max_passages=g.passages,
        )
        generator = CitedGenerator(summarizer)
    return OracleServices(
        config=ocfg,
        index=OracleIndex(searcher, cache_size=ocfg.cache_size),
        searcher=searcher,
        router=router,
        embedder=embedder,
        vectors=store,
        reranker=reranker,
        generator=generator,
        summarizer=summarizer,
    )
