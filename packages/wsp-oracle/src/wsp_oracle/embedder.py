"""``Embedder`` implementations: a Hugging Face encoder on the GPU (fp16) with CPU fallback.

``embed(texts)`` embeds *passages* (the ``Embedder`` port); ``embed_queries(texts)`` applies the
model's query prompt where it has one (e5's ``query:``, Qwen3's instruction). Vectors are
L2-normalized float32 lists. Batches are sorted by length so padding stays small.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt

from wsp_core.ports import Embedder
from wsp_oracle import devices

Matrix = npt.NDArray[np.float32]


@runtime_checkable
class QueryEmbedder(Protocol):
    """An ``Embedder`` that also knows how to embed search queries (asymmetric models)."""

    dimensions: int

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...

    def embed_queries(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


@dataclass(frozen=True, slots=True)
class Preset:
    pooling: str  # "cls" | "mean" | "last"
    query_prefix: str = ""
    passage_prefix: str = ""
    padding_side: str = "right"


PRESETS: dict[str, Preset] = {
    "granite": Preset("cls"),
    "bge-m3": Preset("cls"),
    "gte": Preset("cls"),
    "e5": Preset("mean", "query: ", "passage: "),
    "qwen3": Preset(
        "last",
        "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:",
        "",
        padding_side="left",
    ),
}


def preset_for(model: str, preset: str = "auto") -> Preset:
    if preset != "auto":
        try:
            return PRESETS[preset]
        except KeyError:
            raise ValueError(f"unknown embedder preset {preset!r}; one of {sorted(PRESETS)}") from None
    name = model.casefold()
    for key, value in (("e5", "e5"), ("qwen3", "qwen3"), ("bge-m3", "bge-m3"), ("gte", "gte")):
        if key in name:
            return PRESETS[value]
    return PRESETS["granite"]


def half_dtype(torch: Any, device: str) -> str:
    """``bfloat16`` on a GPU that supports it (fp16 overflows in Qwen3's activations), ``float16``
    on older GPUs, ``float32`` on the CPU."""
    if not device.startswith("cuda"):
        return "float32"
    supported = getattr(torch.cuda, "is_bf16_supported", None)
    return "bfloat16" if callable(supported) and supported() else "float16"


def normalize(mat: Matrix) -> Matrix:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32, copy=False)


class TransformerEmbedder:
    """A local Hugging Face encoder. Thread-safe; one forward pass at a time per instance."""

    def __init__(
        self,
        model_dir: Path,
        *,
        preset: str = "auto",
        device: str = "auto",
        batch_size: int = 256,
        max_tokens: int = 512,
        dimensions: int = 0,
    ) -> None:
        if not model_dir.is_dir():
            raise FileNotFoundError(f"embedding model not found: {model_dir}")
        self.model_dir = model_dir
        self.preset = preset_for(model_dir.name, preset)
        self.device = devices.resolve_device(device)
        self.batch_size = batch_size
        self.max_tokens = max_tokens
        torch, tf = devices.torch(), devices.transformers()
        self._torch: Any = torch
        self._tok: Any = tf.AutoTokenizer.from_pretrained(
            str(model_dir), local_files_only=True, padding_side=self.preset.padding_side
        )
        self.dtype_name = half_dtype(torch, self.device)
        dtype = getattr(torch, self.dtype_name)
        model: Any = tf.AutoModel.from_pretrained(str(model_dir), local_files_only=True, dtype=dtype)
        self._model: Any = model.to(self.device).eval()
        full = int(self._model.config.hidden_size)
        self.dimensions = dimensions if 0 < dimensions < full else full
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="tokenize")

    def _tokenize(self, texts: list[str]) -> Any:
        tok = self._tok
        return tok(texts, padding=True, truncation=True, max_length=self.max_tokens, return_tensors="pt")

    def _forward(self, batch: Any) -> Matrix:
        torch = self._torch
        enc = {k: v.to(self.device, non_blocking=True) for k, v in batch.items()}
        with torch.inference_mode():
            hidden = self._model(**enc).last_hidden_state
            mask = enc["attention_mask"]
            if self.preset.pooling == "cls":
                pooled = hidden[:, 0]
            elif self.preset.pooling == "mean":
                m = mask.unsqueeze(-1).to(hidden.dtype)
                pooled = (hidden * m).sum(1) / m.sum(1).clamp(min=1)
            else:  # last token; left padding puts it at -1
                pooled = hidden[:, -1]
            pooled = pooled[:, : self.dimensions].float()
            pooled = torch.nn.functional.normalize(pooled, dim=-1)
        out: Matrix = pooled.cpu().numpy().astype(np.float32, copy=False)
        if not np.isfinite(out).all():  # fp16 overflow (Qwen3) would quantize to garbage silently
            raise FloatingPointError(f"non-finite embeddings from {self.model_dir.name} ({self.dtype_name})")
        return out

    def encode(self, texts: Sequence[str], *, prefix: str = "") -> Matrix:
        """Embed ``texts`` as an ``(n, dimensions)`` float32 matrix (rows L2-normalized)."""
        if not texts:
            return np.zeros((0, self.dimensions), dtype=np.float32)
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]), reverse=True)
        out = np.empty((len(texts), self.dimensions), dtype=np.float32)
        batches = [order[s : s + self.batch_size] for s in range(0, len(order), self.batch_size)]
        if len(batches) == 1:  # a query: no tokenizer thread to start
            with self._lock:
                out[batches[0]] = self._forward(self._tokenize([prefix + texts[i] for i in batches[0]]))
            return out
        with self._lock:
            # tokenize the next batch on the CPU while the GPU runs this one (one long-lived thread)
            pool = self._pool
            pending = pool.submit(self._tokenize, [prefix + texts[i] for i in batches[0]])
            for n, idx in enumerate(batches):
                batch = pending.result()
                if n + 1 < len(batches):
                    pending = pool.submit(self._tokenize, [prefix + texts[i] for i in batches[n + 1]])
                out[idx] = self._forward(batch)
        return out

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return self.encode(texts, prefix=self.preset.passage_prefix).tolist()

    def embed_queries(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return self.encode(texts, prefix=self.preset.query_prefix).tolist()

    def encode_queries(self, texts: Sequence[str]) -> Matrix:
        return self.encode(texts, prefix=self.preset.query_prefix)

    def encode_passages(self, texts: Sequence[str]) -> Matrix:
        return self.encode(texts, prefix=self.preset.passage_prefix)


def as_matrix(vectors: Sequence[Sequence[float]] | Matrix, dimensions: int) -> Matrix:
    mat = np.asarray(vectors, dtype=np.float32)
    mat = mat.reshape(len(mat), -1) if mat.size else np.zeros((0, dimensions), dtype=np.float32)
    if mat.shape[1] != dimensions:
        raise ValueError(f"expected {dimensions} dimensions, got {mat.shape[1]}")
    return mat


def encode_queries(embedder: Embedder, texts: Sequence[str], dimensions: int) -> Matrix:
    """Query vectors from any ``Embedder``; uses ``encode_queries``/``embed_queries`` when offered."""
    if isinstance(embedder, TransformerEmbedder):
        return embedder.encode_queries(texts)
    if isinstance(embedder, QueryEmbedder):
        return as_matrix(embedder.embed_queries(texts), dimensions)
    return as_matrix(embedder.embed(texts), dimensions)


def encode_passages(embedder: Embedder, texts: Sequence[str], dimensions: int) -> Matrix:
    if isinstance(embedder, TransformerEmbedder):
        return embedder.encode_passages(texts)
    return as_matrix(embedder.embed(texts), dimensions)
