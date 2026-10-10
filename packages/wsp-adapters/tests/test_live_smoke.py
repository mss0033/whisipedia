"""Optional live checks against the real upstream catalogs. Metadata only; nothing large is downloaded.

Run with ``uv run pytest packages/wsp-adapters -m network -s``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from adapters_testkit import UrllibFetcher, make_ctx, make_spec

from wsp_adapters._streams import listing_links
from wsp_adapters.arxiv import OAI_URL, parse_page, parse_record
from wsp_adapters.courtlistener import DEFAULT_ENDPOINT, parse_s3_listing
from wsp_adapters.kiwix import KiwixOpdsAdapter
from wsp_adapters.osm import OsmPlanetAdapter
from wsp_adapters.pubmed import DEFAULT_ENDPOINTS as PUBMED_ENDPOINTS
from wsp_adapters.pubmed import file_key, parse_md5
from wsp_adapters.wikimedia import WikimediaDumpsAdapter

pytestmark = pytest.mark.network


def test_kiwix_catalog_live(tmp_path: Path) -> None:
    spec = make_spec(
        "wikipedia-en-zim",
        {"adapter": "kiwix-opds", "selector": {"name": "wikipedia_en_all", "flavour": "maxi"}},
    )
    (release,) = KiwixOpdsAdapter().discover(make_ctx(spec, UrllibFetcher(), tmp_path))
    (remote,) = release.files
    print(f"\nkiwix {release.version} {remote.name} checksums={remote.checksums} mirrors={remote.mirrors}")
    assert remote.checksums
    assert remote.checksums[0].algorithm == "sha256"
    assert remote.metalink is not None
    assert remote.metalink.endswith(".meta4")


def test_wikimedia_enwiki_live(tmp_path: Path) -> None:
    spec = make_spec("wikipedia-en", {"adapter": "wikimedia-dumps", "wikis": ["enwiki"]})
    (release,) = WikimediaDumpsAdapter().discover(make_ctx(spec, UrllibFetcher(), tmp_path))
    print(f"\nenwiki {release.version} published={release.published}")
    for f in release.files:
        print(f"  {f.name} size={f.size} {[c.algorithm for c in f.checksums]} mirrors={len(f.mirrors)}")
    assert release.files
    assert all(f.checksums for f in release.files)


def test_arxiv_oai_live() -> None:
    """One OAI-PMH request (arXiv asks bulk harvesters for at most one request every 3 s)."""
    query = "verb=ListRecords&metadataPrefix=arXiv&from=2026-10-01&until=2026-10-01"
    result = parse_page(UrllibFetcher().get_bytes(f"{OAI_URL}?{query}"))
    papers = [p for r in result.records if (p := parse_record(r)) is not None]
    print(f"\narXiv OAI: {len(papers)} records on the first page, token={bool(result.token)}")
    assert papers
    assert all(p.arxiv_id for p in papers)


def test_courtlistener_listing_live() -> None:
    bucket = DEFAULT_ENDPOINT.rstrip("/").rsplit("/", 1)[0]
    objects, token = parse_s3_listing(UrllibFetcher().get_bytes(f"{bucket}/?list-type=2&prefix=bulk-data/"))
    print(f"\ncourtlistener: {len(objects)} objects on the first page, more={token is not None}")
    assert objects


def test_osm_planet_live(tmp_path: Path) -> None:
    spec = make_spec("osm-planet", {"adapter": "osm-planet"}, license_="ODbL-1.0")
    (release,) = OsmPlanetAdapter().discover(make_ctx(spec, UrllibFetcher(), tmp_path))
    print(f"\nosm planet {release.version} {release.files[0].name} {release.files[0].checksums}")
    assert release.files[0].checksums


def test_pubmed_listing_live() -> None:
    base = PUBMED_ENDPOINTS[0]
    names = [
        u for u in listing_links(UrllibFetcher().get_bytes(base), base) if file_key(u.rsplit("/", 1)[-1])
    ]
    print(f"\npubmed baseline: {len(names)} files, first {names[0]}")
    assert parse_md5(UrllibFetcher().get_bytes(names[0] + ".md5").decode())
