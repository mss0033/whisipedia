"""Unit tests: Metalink, hashing, mirror catalogs, token bucket, resume state."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import time
from pathlib import Path

import pytest

from wsp_core.model import Checksum, RemoteFile
from wsp_fetch import ConflictingChecksumsError, FetchConfig, UnsupportedChecksumError
from wsp_fetch.hashing import InOrderHasher, MultiHasher, hash_file, merge_checksums, normalize_algorithm
from wsp_fetch.metalink import MetalinkError, parse_metalink
from wsp_fetch.mirrors import kiwix_mirror_bases, kiwix_mirrors, load_wikimedia_catalog
from wsp_fetch.ratelimit import TokenBucket
from wsp_fetch.state import PartialState, load_state, save_state

KIWIX_META4 = b"""<?xml version="1.0" encoding="UTF-8"?>
<metalink xmlns="urn:ietf:params:xml:ns:metalink">
  <generator>MirrorBrain/2.19.0</generator>
  <file name="wikipedia_xx_test.zim">
    <size>10</size>
    <hash type="md5">e00f3861b76dd768ab73db8fcb757bc2</hash>
    <hash type="sha-256">2eb7fd115bfb8644a08a9482f01f52b507428b1b8a30129fead92f7b6715440b</hash>
    <pieces length="4" type="sha-1">
      <hash>aa</hash><hash>bb</hash><hash>cc</hash>
    </pieces>
    <!-- Mirrors which handle this country -->
    <url location="us" priority="2">https://b.example.org/kiwix/zim/wikipedia/wikipedia_xx_test.zim</url>
    <url location="nl" priority="1">https://a.example.org/pub/kiwix/zim/wikipedia/wikipedia_xx_test.zim</url>
  </file>
</metalink>
"""


def test_parse_kiwix_style_metalink() -> None:
    (f,) = parse_metalink(KIWIX_META4)
    assert f.name == "wikipedia_xx_test.zim"
    assert f.size == 10
    assert {c.algorithm for c in f.checksums} == {"md5", "sha256"}
    assert f.pieces is not None
    assert f.pieces.algorithm == "sha1"
    assert f.pieces.hashes == ("aa", "bb", "cc")
    assert f.mirror_urls()[0].startswith("https://a.example.org/")
    assert kiwix_mirror_bases(KIWIX_META4, "zim/wikipedia/wikipedia_xx_test.zim") == (
        "https://a.example.org/pub/kiwix/",
        "https://b.example.org/kiwix/",
    )
    assert len(kiwix_mirrors(KIWIX_META4)) == 2


@pytest.mark.parametrize(
    "doc",
    [
        b"not xml",
        b"<metalink xmlns='urn:other'/>",
        b"<metalink xmlns='urn:ietf:params:xml:ns:metalink'/>",
        b"<metalink xmlns='urn:ietf:params:xml:ns:metalink'><file name='x'><size>9</size>"
        b"<pieces length='4' type='sha-1'><hash>a</hash></pieces></file></metalink>",
    ],
)
def test_bad_metalinks_rejected(doc: bytes) -> None:
    with pytest.raises(MetalinkError):
        parse_metalink(doc)


def test_algorithm_names_and_merge() -> None:
    assert normalize_algorithm("SHA-256") == "sha256"
    assert normalize_algorithm("sha-1") == "sha1"
    merged = merge_checksums([Checksum("SHA-256", "AB")], [Checksum("sha256", "ab"), Checksum("md5", "cd")])
    assert merged == (Checksum("sha256", "ab"), Checksum("md5", "cd"))
    with pytest.raises(ConflictingChecksumsError):
        merge_checksums([Checksum("sha256", "ab")], [Checksum("sha-256", "ac")])
    with pytest.raises(UnsupportedChecksumError):
        MultiHasher(["crc-nonsense"])


def test_multihasher_parallel_matches_hashlib(tmp_path: Path) -> None:
    data = os.urandom(3 << 20)
    p = tmp_path / "x"
    p.write_bytes(data)
    d = hash_file(p, ["md5", "sha1"], block=1 << 20)
    assert d["sha256"] == hashlib.sha256(data).hexdigest()
    assert d["md5"] == hashlib.md5(data).hexdigest()
    assert set(d) == {"sha256", "blake3", "md5", "sha1"}


def test_inorder_hasher_out_of_order_with_tiny_window(tmp_path: Path) -> None:
    data = os.urandom(10 * 1000)
    p = tmp_path / "f"
    p.write_bytes(data)
    fd = os.open(p, os.O_RDONLY)
    bad: list[int] = []
    try:
        h = InOrderHasher(
            MultiHasher([]),
            fd,
            lambda i: (i * 1000, 1000),
            10,
            window_bytes=1500,  # forces most pieces to be re-read from disk
            verify=lambda i, b: i != 4,
            on_bad=bad.append,
        )
        h.start()
        for i in [9, 7, 5, 3, 1, 8, 6, 2, 0]:
            h.offer(i, data[i * 1000 : (i + 1) * 1000])
        h.offer(4, None, verified=False)  # resumed piece that fails verification
        deadline = time.monotonic() + 5
        while not bad and time.monotonic() < deadline:
            time.sleep(0.01)
        assert bad == [4]
        h.offer(4, data[4000:5000])  # re-downloaded
        assert h.done.wait(5)
        assert h.error is None
        assert h.hasher.hexdigests()["sha256"] == hashlib.sha256(data).hexdigest()
    finally:
        os.close(fd)


def test_token_bucket_rate() -> None:
    b = TokenBucket(rate=1_000_000, capacity=100_000)
    t0 = time.monotonic()
    for _ in range(4):
        b.consume(100_000)
    assert time.monotonic() - t0 >= 0.25


def test_state_roundtrip_and_target_identity(tmp_path: Path) -> None:
    s = PartialState("u", 10, 4, "blake3", {"sha256": "ab"}, '"e"', {0: "x", 2: "y"})
    p = tmp_path / "s.json"
    save_state(p, s)
    loaded = load_state(p)
    assert loaded == s
    assert loaded is not None
    # a missing validator on either side forbids resuming (upstream may have changed), unless every
    # resumed piece is checked against upstream piece hashes
    assert not loaded.same_target(PartialState("u", 10, 4, "blake3", {"sha256": "ab"}, None))
    assert loaded.same_target(
        PartialState("u", 10, 4, "blake3", {"sha256": "ab"}, None), require_validator=False
    )
    assert loaded.same_target(PartialState("u", 10, 4, "blake3", {"sha256": "ab"}, loaded.validator))
    assert not loaded.same_target(PartialState("u", 10, 4, "blake3", {"sha256": "ab"}, '"other"'))
    assert not loaded.same_target(PartialState("u", 11, 4, "blake3", {"sha256": "ab"}))
    p.write_text("{garbage")
    assert load_state(p) is None


def test_wikimedia_catalog() -> None:
    cat = load_wikimedia_catalog()
    assert cat.origin == "https://dumps.wikimedia.org/"
    assert cat.reviewed is not None
    assert cat.refresh_by is not None
    assert cat.is_stale(cat.refresh_by + dt.timedelta(days=1))
    url = "https://dumps.wikimedia.org/dewiki/20261001/dewiki-20261001-pages-articles-multistream.xml.bz2"
    urls = cat.urls_for(url)
    assert urls[0] == url
    assert len(urls) > 3
    assert all(u.endswith("dewiki/20261001/dewiki-20261001-pages-articles-multistream.xml.bz2") for u in urls)
    assert not any("scatter" in u for u in urls)  # Scatter only carries enwiki/wikidata/commons
    assert any("scatter" in u for u in cat.urls_for(url.replace("dewiki", "enwiki")))
    assert cat.urls_for("https://example.org/x") == ("https://example.org/x",)
    assert cat.host_connections()["dumps.wikimedia.org"] == 2
    remote = cat.with_mirrors(RemoteFile(url, "x.bz2"))
    assert len(remote.mirrors) == len(urls) - 1
    cfg = FetchConfig(host_connections=cat.host_connections())
    assert cfg.connections_for(url) == 2


def test_config_from_mapping() -> None:
    cfg = FetchConfig.from_mapping(
        {
            "user_agent": "UA",
            "max_connections_per_host": 3,
            "cycle": "monthly",  # unrelated acquirer keys are ignored
            "metalink_hosts": ["a.org"],
            "host_connections": {"Mirror.Example:8080": 6},
        }
    )
    assert cfg.user_agent == "UA"
    assert cfg.metalink_hosts == ("a.org",)
    assert cfg.connections_for("http://mirror.example:8080/x") == 6
    assert cfg.connections_for("https://other.example/x") == 3
