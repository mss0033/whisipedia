"""Optional live checks against the real upstream catalogs. Metadata only; nothing large is downloaded.

Run with ``uv run pytest packages/wsp-adapters -m network -s``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from adapters_testkit import UrllibFetcher, make_ctx, make_spec

from wsp_adapters.kiwix import KiwixOpdsAdapter
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
