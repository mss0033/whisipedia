from __future__ import annotations

from pathlib import Path

import pytest

from wsp_core.errors import NotFoundError
from wsp_core.model import RemoteFile
from wsp_core.ports import SupportsHead
from wsp_core.testing.fetcher_contract import RangeServer, payload, sha256_checksum
from wsp_fetch import FetchConfig, HttpFetcher, MetalinkFile, MetalinkUrl, build_metalink

FAST = FetchConfig(user_agent="wsp-test", backoff_base=0.01, backoff_max=0.05, max_attempts=2)


def test_head_reports_size_and_validators() -> None:
    with RangeServer() as server, HttpFetcher(FAST) as fetcher:
        url = server.add("h.bin", b"x" * 1234)
        assert isinstance(fetcher, SupportsHead)
        head = fetcher.head(url)
        assert head.size == 1234
        assert head.accepts_ranges
        assert head.etag
        with pytest.raises(NotFoundError):
            fetcher.head(server.url("missing.bin"))


def test_remote_metalink_field_supplies_mirrors(tmp_path: Path) -> None:
    data = payload(200_000, seed=9)
    with RangeServer() as primary, RangeServer() as mirror, HttpFetcher(FAST) as fetcher:
        primary.add("f.bin", data)
        primary.faults.missing = True
        murl = mirror.add("f.bin", data)
        meta = build_metalink(
            [MetalinkFile("f.bin", len(data), (sha256_checksum(data),), urls=(MetalinkUrl(murl),))]
        )
        meta_url = mirror.add("f.bin.meta4", meta)
        remote = RemoteFile(primary.url("f.bin"), "f.bin", metalink=meta_url, mirrors=(meta_url,))
        result = fetcher.download(remote, tmp_path / "f.bin")
        assert result.path.read_bytes() == data
        assert result.verdict.ok
