"""The ``[oracle]`` configuration section (documented in ``docs/components/oracle.md``).

Every key has a default, so an empty section gives a lexical-only Oracle with the router, the
dense path switched on only when vectors exist, and no generator. Relative model paths resolve
against ``models_dir`` (default ``~/.cache/whisipedia/models``); weights never live in the repo.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

DEFAULT_MODELS_DIR = "~/.cache/whisipedia/models"
DEFAULT_EMBED_MODEL = "intfloat--multilingual-e5-base"
DEFAULT_RERANK_MODEL = "BAAI--bge-reranker-v2-m3"
DEFAULT_LLM_MODEL = "llm/Qwen3.5-9B-Q6_K.gguf"
DEFAULT_LLM_URL = "http://127.0.0.1:8738"
DEFAULT_LLAMA_SERVER = "~/.local/share/whisipedia/llama.cpp/current/llama-server"


class OracleConfigError(ValueError):
    """The ``[oracle]`` section is malformed."""


def _section(m: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = m.get(key, {})
    if not isinstance(value, Mapping):
        raise OracleConfigError(f"[oracle.{key}] must be a table")
    return cast(Mapping[str, Any], value)


def _strs(value: object, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise OracleConfigError(f"oracle.{key} must be a list of strings")
    return tuple(str(x) for x in cast(Sequence[object], value))


def _floats(value: object, key: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise OracleConfigError(f"oracle.{key} must be a table of numbers")
    return {str(k): float(v) for k, v in cast(Mapping[str, Any], value).items()}


def _device(value: object) -> str:
    dev = str(value)
    if dev not in ("auto", "cuda", "cpu") and not dev.startswith("cuda:"):
        raise OracleConfigError(f"device must be auto, cuda, cuda:N or cpu (got {dev!r})")
    return dev


@dataclass(frozen=True, slots=True)
class EmbedderConfig:
    model: str = DEFAULT_EMBED_MODEL
    """Directory of a Hugging Face model (absolute, or relative to ``models_dir``)."""
    preset: str = "auto"
    """Pooling and prefixes: ``auto`` (from the model name), ``granite``, ``e5``, ``bge-m3``, ``qwen3``."""
    device: str = "auto"
    batch_size: int = 256
    max_tokens: int = 512
    dimensions: int = 0
    """Truncate to this many dimensions (Matryoshka models only); 0 keeps the model's size."""


@dataclass(frozen=True, slots=True)
class RerankerConfig:
    enabled: bool = True
    model: str = DEFAULT_RERANK_MODEL
    device: str = "auto"
    cpu_fallback: bool = False
    """Without a GPU the reranker is skipped unless this is true (a CPU cross-encoder misses 50 ms)."""
    top_k: int = 50
    max_tokens: int = 256
    batch_size: int = 64


@dataclass(frozen=True, slots=True)
class VectorConfig:
    path: str = ""
    """Vector store directory; ``""`` = ``<index.root>/vectors``. Local disk only (ADR-0002)."""
    mode: str = "load"
    """``load`` keeps the binary HNSW graph in RAM (writable); ``view`` memory-maps it read-only."""
    oversample: int = 8
    """Binary candidates per wanted result, rescored with the int8 vectors."""
    connectivity: int = 16
    expansion_add: int = 128
    expansion_search: int = 96
    store_text: bool = True
    """Keep chunk text next to the vectors so the reranker scores the passage that matched."""


@dataclass(frozen=True, slots=True)
class GeneratorConfig:
    enabled: bool = False
    url: str = DEFAULT_LLM_URL
    """llama.cpp server (OpenAI-compatible). Must be a loopback address (PLAN §10.5)."""
    model: str = DEFAULT_LLM_MODEL
    server_binary: str = DEFAULT_LLAMA_SERVER
    context: int = 8192
    gpu_layers: int = 999
    timeout_s: float = 60.0
    temperature: float = 0.2
    min_confidence: float = 0.2
    """Refuse to summarize when the best passage's relevance (reranker probability) is below this."""
    passages: int = 8
    passage_chars: int = 1500


@dataclass(frozen=True, slots=True)
class OracleConfig:
    enabled: bool = True
    models_dir: str = DEFAULT_MODELS_DIR
    dense_sources: tuple[str, ...] = ()
    """Sources that get dense vectors (PLAN §9.4 priority corpora); the rest are lexical only."""
    rrf_k: int = 60
    lexical_weight: float = 1.0
    dense_weight: float = 1.0
    depth: int = 50
    source_priors: Mapping[str, float] = field(default_factory=dict[str, float])
    chunk_words: int = 200
    max_chunks_per_doc: int = 256
    cache_size: int = 256
    """Recent result lists kept so paging and facets do not re-run the pipeline."""
    budget_ms: float = 100.0
    """Target for results on screen (PLAN §9.3); logged when exceeded, never enforced by dropping work."""
    embedder: EmbedderConfig = field(default_factory=EmbedderConfig)
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    vectors: VectorConfig = field(default_factory=VectorConfig)
    generator: GeneratorConfig = field(default_factory=GeneratorConfig)

    def model_path(self, name: str) -> Path:
        p = Path(name).expanduser()
        return p if p.is_absolute() else Path(self.models_dir).expanduser() / p

    def vector_path(self, index_root: Path | None) -> Path:
        if self.vectors.path:
            return Path(self.vectors.path).expanduser()
        if index_root is None:
            raise OracleConfigError("oracle.vectors.path is empty and index.root is not set")
        return index_root / "vectors"

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> OracleConfig:
        """Build from the ``[oracle]`` table (unknown keys are rejected so typos fail loudly)."""
        known = {
            "enabled",
            "models_dir",
            "dense_sources",
            "rrf_k",
            "lexical_weight",
            "dense_weight",
            "depth",
            "source_priors",
            "chunk_words",
            "max_chunks_per_doc",
            "cache_size",
            "budget_ms",
            "embedder",
            "reranker",
            "vectors",
            "generator",
        }
        unknown = sorted(set(section) - known)
        if unknown:
            raise OracleConfigError(f"unknown [oracle] keys: {', '.join(unknown)}")
        emb, rr, vec, gen = (_section(section, k) for k in ("embedder", "reranker", "vectors", "generator"))
        try:
            embedder = EmbedderConfig(
                model=str(emb.get("model", DEFAULT_EMBED_MODEL)),
                preset=str(emb.get("preset", "auto")),
                device=_device(emb.get("device", "auto")),
                batch_size=int(emb.get("batch_size", 256)),
                max_tokens=int(emb.get("max_tokens", 512)),
                dimensions=int(emb.get("dimensions", 0)),
            )
            reranker = RerankerConfig(
                enabled=bool(rr.get("enabled", True)),
                model=str(rr.get("model", DEFAULT_RERANK_MODEL)),
                device=_device(rr.get("device", "auto")),
                cpu_fallback=bool(rr.get("cpu_fallback", False)),
                top_k=int(rr.get("top_k", 50)),
                max_tokens=int(rr.get("max_tokens", 256)),
                batch_size=int(rr.get("batch_size", 64)),
            )
            vectors = VectorConfig(
                path=str(vec.get("path", "")),
                mode=str(vec.get("mode", "load")),
                oversample=int(vec.get("oversample", 8)),
                connectivity=int(vec.get("connectivity", 16)),
                expansion_add=int(vec.get("expansion_add", 128)),
                expansion_search=int(vec.get("expansion_search", 96)),
                store_text=bool(vec.get("store_text", True)),
            )
            generator = GeneratorConfig(
                enabled=bool(gen.get("enabled", False)),
                url=str(gen.get("url", DEFAULT_LLM_URL)),
                model=str(gen.get("model", DEFAULT_LLM_MODEL)),
                server_binary=str(gen.get("server_binary", DEFAULT_LLAMA_SERVER)),
                context=int(gen.get("context", 8192)),
                gpu_layers=int(gen.get("gpu_layers", 999)),
                timeout_s=float(gen.get("timeout_s", 60.0)),
                temperature=float(gen.get("temperature", 0.2)),
                min_confidence=float(gen.get("min_confidence", 0.2)),
                passages=int(gen.get("passages", 8)),
                passage_chars=int(gen.get("passage_chars", 1500)),
            )
            cfg = cls(
                enabled=bool(section.get("enabled", True)),
                models_dir=str(section.get("models_dir", DEFAULT_MODELS_DIR)),
                dense_sources=_strs(section.get("dense_sources", ()), "dense_sources"),
                rrf_k=int(section.get("rrf_k", 60)),
                lexical_weight=float(section.get("lexical_weight", 1.0)),
                dense_weight=float(section.get("dense_weight", 1.0)),
                depth=int(section.get("depth", 50)),
                source_priors=_floats(section.get("source_priors", {}), "source_priors"),
                chunk_words=int(section.get("chunk_words", 200)),
                max_chunks_per_doc=int(section.get("max_chunks_per_doc", 256)),
                cache_size=int(section.get("cache_size", 256)),
                budget_ms=float(section.get("budget_ms", 100.0)),
                embedder=embedder,
                reranker=reranker,
                vectors=vectors,
                generator=generator,
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, OracleConfigError):
                raise
            raise OracleConfigError(f"invalid [oracle] value: {exc}") from exc
        if cfg.vectors.mode not in ("load", "view"):
            raise OracleConfigError("oracle.vectors.mode must be 'load' or 'view'")
        if cfg.depth < 1 or cfg.reranker.top_k < 1 or cfg.vectors.oversample < 1:
            raise OracleConfigError("oracle depth, reranker.top_k and vectors.oversample must be positive")
        return cfg
