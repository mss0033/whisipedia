"""``build_oracle(config) -> OracleServices``: the Oracle's factory for the composition root.

Everything degrades gracefully: no vector store (or no ``wsp-oracle[gpu]``) → lexical + router
only; no GPU → no reranker unless ``reranker.cpu_fallback``. Each decision is logged once at
startup. Nothing here opens a network connection: models load from local paths with the Hugging
Face libraries in offline mode. There is no generative model (ADR-0008).
"""

from __future__ import annotations

import contextlib
import ctypes
import gc
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from wsp_core.ports import Embedder, LexicalIndex, Reranker
from wsp_index import FusionConfig
from wsp_index.hybrid import StaticFeatures
from wsp_index.linkrank import StaticRank
from wsp_oracle import devices
from wsp_oracle.config import OracleConfig
from wsp_oracle.embedder import TransformerEmbedder
from wsp_oracle.pipeline import ContentHashes, OracleIndex, OracleSearcher, Resolver
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

    def close(self) -> None:
        if self.vectors is not None:
            self.vectors.close()
        self.index.close()


def release_freed_memory() -> None:
    """Return freed heap pages to the OS (glibc ``malloc_trim``); a no-op elsewhere.

    Loading a model stages its weights in host memory before moving them to the GPU; glibc keeps those
    freed pages, ~200 MB a model, for the life of the API (measured 2026-10-11).
    """
    gc.collect()
    with contextlib.suppress(OSError, AttributeError):  # not glibc
        ctypes.CDLL("libc.so.6").malloc_trim(0)


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


def _static_features(lexical: LexicalIndex) -> StaticFeatures | None:
    """Link-analysis features from the lexical index's static-rank sidecar (re-read when rebuilt)."""
    current = getattr(lexical, "static_rank", None)
    if not callable(current):
        return None

    def features(ids: Sequence[str]) -> Sequence[float]:
        rank = cast(Callable[[], StaticRank | None], current)()
        return [0.0] * len(ids) if rank is None else [float(x) for x in rank.features(ids)]

    return features


def build_oracle(
    cfg: Mapping[str, Any],
    *,
    lexical: LexicalIndex | None = None,
    sources: Sequence[str] = (),
    resolve: Resolver | None = None,
    site_words: Mapping[str, Sequence[str]] | None = None,
    content_hashes: ContentHashes | None = None,
) -> OracleServices:
    """Build the Oracle from the full layered config (reads ``[oracle]`` and ``[index]``).

    ``lexical`` is the already-open lexical index (opened from ``index.root`` when omitted);
    ``sources`` are the library's source ids and ``site_words`` their registry site words (source
    id -> words): together they name the sites the router recognizes (``Router.from_sources``).
    ``content_hashes`` (``ParquetDocumentStore.content_hashes``) lets the Oracle collapse duplicate
    records of one document (read from ``index.root``'s Refinery when the lexical index is opened here
    too); without it no hit is collapsed.
    """
    section = cfg.get("oracle", {})
    ocfg = OracleConfig.from_mapping(cast(Mapping[str, Any], section) if isinstance(section, Mapping) else {})
    index_root = _index_root(cfg)
    if lexical is None:
        lexical = _open_lexical(cfg, index_root)
        if content_hashes is None and index_root is not None:
            from wsp_index import ParquetDocumentStore
            from wsp_index.refinery import configured_root

            index = cfg.get("index", {})
            value = (
                cast(Mapping[str, Any], index).get("refinery_root") if isinstance(index, Mapping) else None
            )
            refinery = configured_root(index_root, value)
            if refinery.is_dir():
                maps = (
                    index_root / "refinery-maps" if str(value or "").strip() else None
                )  # as bootstrap.Paths
                content_hashes = ParquetDocumentStore(refinery, map_root=maps).content_hashes
    if resolve is None:
        lookup = getattr(lexical, "lookup", None)
        resolve = cast(Resolver, lookup) if callable(lookup) else None
    router = Router.from_sources(sources, site_words)

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

    if embedder is not None or reranker is not None:
        release_freed_memory()  # the weights were staged in host memory on their way to the device
    fusion = FusionConfig(
        k=ocfg.rrf_k,
        lexical_weight=ocfg.lexical_weight,
        dense_weight=ocfg.dense_weight,
        depth=ocfg.depth,
        source_priors=dict(ocfg.source_priors),
        static_rank=_static_features(lexical),
        static_weight=ocfg.static_rank_weight,
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
        content_hashes=content_hashes,
    )
    return OracleServices(
        config=ocfg,
        index=OracleIndex(
            searcher, cache_size=ocfg.cache_size, refresh=store.refresh if store is not None else None
        ),
        searcher=searcher,
        router=router,
        embedder=embedder,
        vectors=store,
        reranker=reranker,
    )
