from types import SimpleNamespace
from typing import Any, cast

from wsp_api.searching import Searcher
from wsp_core.model import SourceSpec


def _spec(sid: str, **raw: Any) -> SourceSpec:
    return cast(Any, SimpleNamespace(id=sid, raw=raw))


def test_an_unindexed_source_is_searched_through_its_indexed_copy() -> None:
    specs = [
        _spec("wikipedia-top10", access={"rendition": [{"lang": "en", "source": "wikipedia-en-zim"}]}),
        _spec("wikipedia-en-zim", index=False, access={"adapter": "kiwix-opds"}),
        _spec("gutenberg"),
        _spec("gutenberg-zim", index=False, access={"searched_as": ["gutenberg"]}),
        _spec("osm-tiles", index=False),
        _spec("rfc"),
    ]
    by_id = {s.id: s for s in specs}
    lib = SimpleNamespace(s=SimpleNamespace(source=by_id.get, sources=specs))
    searcher = Searcher(cast(Any, lib))
    wanted, replaced = searcher.searched_instead(["wikipedia-en-zim", "gutenberg-zim", "rfc", "osm-tiles"])
    assert wanted == ["wikipedia-top10", "gutenberg", "rfc", "osm-tiles"]
    assert replaced == [("wikipedia-en-zim", ["wikipedia-top10"]), ("gutenberg-zim", ["gutenberg"])]
    assert searcher.searched_instead(None) == (None, [])
