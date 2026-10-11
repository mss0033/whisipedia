"""``doc-collection``: curated document sets with document-level policy (PLAN §6.2, D11, D13, D14).

* :mod:`~wsp_adapters.doccollection.policy`: the deterministic filter (series allow/deny lists,
  front-matter marking parse, keyword screen) and edition currency;
* :mod:`~wsp_adapters.doccollection.discovery`: index pages, APIs, OAI-PMH, archive.org, S3, git;
* :mod:`~wsp_adapters.doccollection.catalog`: the per-generation ``meta/documents.jsonl``;
* :mod:`~wsp_adapters.doccollection.adapter`: the ``SourceAdapter``.
"""

from __future__ import annotations

from wsp_adapters.doccollection.adapter import DocCollectionAdapter

__all__ = ["DocCollectionAdapter"]
