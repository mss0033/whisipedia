"""``Reranker`` implementations: a cross-encoder on the GPU, and a pass-through.

The port reranks ``SearchHit``s and scores ``title + snippet``. The Oracle pipeline puts the best
matching passage into ``snippet`` before calling it (and restores the display snippet afterwards),
so the cross-encoder judges the text that actually matched. Scores are probabilities
(``sigmoid(logit)``), which also serve as the retrieval-confidence signal (``low_confidence``).
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from wsp_core.model import SearchHit
from wsp_oracle import devices

_TAGS = re.compile(r"<[^>]+>")


def plain(snippet: str) -> str:
    """Snippet HTML (``<b>`` highlights, escaped entities) as plain text."""
    import html

    return html.unescape(_TAGS.sub("", snippet))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x)) if x >= -60 else 0.0


class NullReranker:
    """Keeps the fused order (no GPU, or reranking switched off)."""

    def rerank(self, query: str, hits: Sequence[SearchHit]) -> Sequence[SearchHit]:
        return list(hits)

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        return [0.0 for _ in passages]


class CrossEncoderReranker:
    """A sequence-classification cross-encoder (``bge-reranker-v2-m3`` by default)."""

    def __init__(
        self,
        model_dir: Path,
        *,
        device: str = "auto",
        max_tokens: int = 256,
        batch_size: int = 64,
    ) -> None:
        if not model_dir.is_dir():
            raise FileNotFoundError(f"reranker model not found: {model_dir}")
        self.model_dir = model_dir
        self.device = devices.resolve_device(device)
        self.max_tokens = max_tokens
        self.batch_size = batch_size
        torch, tf = devices.torch(), devices.transformers()
        self._torch: Any = torch
        self._tok: Any = tf.AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
        from wsp_oracle.embedder import half_dtype

        dtype = getattr(torch, half_dtype(torch, self.device))
        model: Any = tf.AutoModelForSequenceClassification.from_pretrained(
            str(model_dir), local_files_only=True, dtype=dtype
        )
        self._model: Any = model.to(self.device).eval()
        self._lock = threading.Lock()

    def logits(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        torch = self._torch
        out: list[float] = []
        with self._lock, torch.inference_mode():
            for start in range(0, len(passages), self.batch_size):
                part = list(passages[start : start + self.batch_size])
                enc = self._tok(
                    [query] * len(part),
                    part,
                    padding=True,
                    truncation="only_second",
                    max_length=self.max_tokens,
                    return_tensors="pt",
                )
                enc = {k: v.to(self.device) for k, v in enc.items()}
                logits = self._model(**enc).logits.view(-1).float().cpu().tolist()
                out += [float(x) for x in logits]
        return out

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """Relevance probabilities in ``[0, 1]``, one per passage."""
        return [sigmoid(x) for x in self.logits(query, passages)]

    def rerank(self, query: str, hits: Sequence[SearchHit]) -> Sequence[SearchHit]:
        texts = [f"{h.title}\n{plain(h.snippet)}" for h in hits]
        scores = self.score(query, texts)
        ranked = sorted(zip(scores, range(len(hits)), strict=True), key=lambda t: (-t[0], t[1]))
        return [replace(hits[i], score=s) for s, i in ranked]
