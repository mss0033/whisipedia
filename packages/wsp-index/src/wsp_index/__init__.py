"""Whisipedia index: Refinery storage, lexical index, suggest, document store, change feed, hybrid search."""

from wsp_index.changefeed import InvalidCursorError, SqliteChangeFeed
from wsp_index.docstore import ParquetDocumentStore
from wsp_index.hybrid import FusionConfig, HybridSearcher, embed_documents
from wsp_index.lexical import TantivyConfig, TantivyIndex
from wsp_index.localfs import RemoteFilesystemError, ensure_local
from wsp_index.refinery import RefineryReader, RefineryWriter, diff_generations, inlink_counts
from wsp_index.suggest import SuggestEntry, Suggester, TitleTrie
from wsp_index.sync import apply_changes, index_generation, refresh
from wsp_index.vector import HashingEmbedder, NullVectorIndex, NumpyVectorIndex

__version__ = "0.1.0"

__all__ = [
    "FusionConfig",
    "HashingEmbedder",
    "HybridSearcher",
    "InvalidCursorError",
    "NullVectorIndex",
    "NumpyVectorIndex",
    "ParquetDocumentStore",
    "RefineryReader",
    "RefineryWriter",
    "RemoteFilesystemError",
    "SqliteChangeFeed",
    "SuggestEntry",
    "Suggester",
    "TantivyConfig",
    "TantivyIndex",
    "TitleTrie",
    "apply_changes",
    "diff_generations",
    "embed_documents",
    "ensure_local",
    "index_generation",
    "inlink_counts",
    "refresh",
]
