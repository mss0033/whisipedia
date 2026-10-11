"""Port contract suites against the wsp-oracle implementations (and the wsp-index references)."""

from __future__ import annotations

from pathlib import Path

import pytest
from oracle_helpers import MODELS, needs_embed_model

from wsp_core.ports import Embedder, VectorIndex
from wsp_core.testing.embedder_contract import EmbedderContract
from wsp_core.testing.vector_index_contract import VectorIndexContract
from wsp_index import HashingEmbedder, NumpyVectorIndex
from wsp_oracle.config import DEFAULT_EMBED_MODEL
from wsp_oracle.vectors import ChunkVectorStore, DocVectorIndex


class TestChunkVectorStoreContract(VectorIndexContract):
    @pytest.fixture
    def index(self, tmp_path: Path, request: pytest.FixtureRequest) -> VectorIndex:
        store = ChunkVectorStore(tmp_path / "v", self.DIMS)
        request.addfinalizer(store.close)  # noqa: PT021  # base fixture returns, not yields
        return store


class TestDocVectorIndexContract(VectorIndexContract):
    @pytest.fixture
    def index(self, tmp_path: Path, request: pytest.FixtureRequest) -> VectorIndex:
        store = ChunkVectorStore(tmp_path / "v", self.DIMS)
        request.addfinalizer(store.close)  # noqa: PT021  # base fixture returns, not yields
        return DocVectorIndex(store)


class TestNumpyVectorIndexContract(VectorIndexContract):
    @pytest.fixture
    def index(self) -> VectorIndex:
        return NumpyVectorIndex(self.DIMS)


class TestHashingEmbedderContract(EmbedderContract):
    @pytest.fixture
    def embedder(self) -> Embedder:
        return HashingEmbedder(64)


@pytest.mark.gpu
@needs_embed_model
class TestTransformerEmbedderContract(EmbedderContract):
    SEMANTIC = True
    TOLERANCE = 5e-3  # fp16 kernels differ slightly with batch padding

    @pytest.fixture
    def embedder(self, gpu_embedder: Embedder) -> Embedder:
        return gpu_embedder


@pytest.fixture(scope="module")
def gpu_embedder() -> Embedder:
    from wsp_oracle.embedder import TransformerEmbedder

    return TransformerEmbedder(MODELS / DEFAULT_EMBED_MODEL)
