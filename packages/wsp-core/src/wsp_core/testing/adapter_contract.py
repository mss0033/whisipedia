"""Contract suite for ``SourceAdapter`` (PLAN §6.1, §12.2) and an in-memory ``Fetcher`` for tests.

An adapter's test module subclasses :class:`SourceAdapterContract` and provides three fixtures:

* ``adapter``: the adapter under test;
* ``ctx``: a ``Ctx`` whose fetcher serves a tiny fake upstream (see :class:`FakeFetcher`);
* ``generation_dir``: a generation directory holding fixture upstream files.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

import blake3

from wsp_core.errors import NotFoundError
from wsp_core.ids import content_hash, make_doc_id
from wsp_core.model import FetchResult, RemoteFile, VerdictStatus
from wsp_core.ports import Ctx, SourceAdapter


class FakeFetcher:
    """Serves byte strings by URL. A URL with a query string falls back to its path-only route."""

    def __init__(self, routes: Mapping[str, bytes] | None = None) -> None:
        self.routes: dict[str, bytes] = dict(routes or {})
        self.requests: list[str] = []

    def _lookup(self, url: str) -> bytes:
        self.requests.append(url)
        if url in self.routes:
            return self.routes[url]
        base = url.split("?", 1)[0]
        if base in self.routes:
            return self.routes[base]
        raise NotFoundError(url)

    def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        data = self._lookup(url)
        if len(data) > max_bytes:
            raise ValueError(f"{url}: {len(data)} bytes exceeds max_bytes={max_bytes}")
        return data

    def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
        """Writes the first URL that resolves. Does not check checksums (adapters verify)."""
        for url in (remote.url, *remote.mirrors):
            try:
                data = self._lookup(url)
            except NotFoundError:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return FetchResult(
                dest, len(data), hashlib.sha256(data).hexdigest(), blake3.blake3(data).hexdigest()
            )
        raise NotFoundError(remote.url)


class SourceAdapterContract:
    """Behaviour every ``SourceAdapter`` must show, independent of its upstream."""

    def test_implements_port(self, adapter: SourceAdapter) -> None:
        assert isinstance(adapter, SourceAdapter)
        assert adapter.id

    def test_discover_lists_releases_newest_first(self, adapter: SourceAdapter, ctx: Ctx) -> None:
        releases = list(adapter.discover(ctx))
        assert releases, "fixture upstream should offer at least one release"
        versions = [r.version for r in releases]
        assert versions == sorted(versions, reverse=True)
        for release in releases:
            assert release.source_id == ctx.source.id
            names = [f.name for f in release.files]
            assert names
            assert len(names) == len(set(names))
            assert all(n and "/" not in n for n in names)

    def test_fetch_then_verify(self, adapter: SourceAdapter, ctx: Ctx) -> None:
        release = next(iter(adapter.discover(ctx)))
        plan = adapter.plan(ctx, None, release)
        assert {f.name for f in plan.files} == {f.name for f in release.files}
        results = list(adapter.fetch(ctx, plan))
        assert len(results) == len(plan.files)
        for remote, result in zip(plan.files, results, strict=True):
            assert result.path.name == remote.name
            assert result.size == result.path.stat().st_size
            assert adapter.verify(ctx, remote, result.path).ok

    def test_verify_detects_corruption(self, adapter: SourceAdapter, ctx: Ctx) -> None:
        release = next(iter(adapter.discover(ctx)))
        plan = adapter.plan(ctx, None, release)
        remote = next(f for f in plan.files if f.checksums)
        path = next(iter(adapter.fetch(ctx, plan))).path
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0xFF
        path.write_bytes(bytes(data))
        assert adapter.verify(ctx, remote, path).status is VerdictStatus.MISMATCH
        path.unlink()
        assert adapter.verify(ctx, remote, path).status is VerdictStatus.MISSING

    def test_documents_are_well_formed(self, adapter: SourceAdapter, ctx: Ctx, generation_dir: Path) -> None:
        docs = list(adapter.documents(ctx, generation_dir))
        assert docs, "fixture generation should contain documents"
        ids = [d.doc_id for d in docs]
        assert len(ids) == len(set(ids))
        for doc in docs:
            assert doc.source_id == ctx.source.id
            assert doc.doc_id == make_doc_id(doc.source_id, doc.native_id)
            assert doc.content_hash == content_hash(doc.text)
            assert doc.title
            assert doc.license

    def test_documents_are_deterministic(
        self, adapter: SourceAdapter, ctx: Ctx, generation_dir: Path
    ) -> None:
        first = [(d.doc_id, d.content_hash) for d in adapter.documents(ctx, generation_dir)]
        second = [(d.doc_id, d.content_hash) for d in adapter.documents(ctx, generation_dir)]
        assert first == second
