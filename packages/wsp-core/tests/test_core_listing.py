import re

import pytest

from wsp_core.listing import DEFAULT_SKIP_PATHS, is_default_listing, skip_paths, skip_regex


@pytest.mark.parametrize(
    ("path", "listing"),
    [
        ("questions/tagged/first-aid", True),
        ("questions/tagged/first-aid_page=3", True),
        ("questions_page=10", True),
        ("users_page=3", True),
        ("questions/13068/first-aid-for-minor-burns", False),
        ("tags", False),
        ("A/Tagged_union", False),
    ],
)
def test_default_listing_paths(path: str, listing: bool) -> None:
    assert bool(re.fullmatch(skip_regex(DEFAULT_SKIP_PATHS), path)) is listing


def test_registry_overrides_and_bad_patterns() -> None:
    assert skip_paths({}) == DEFAULT_SKIP_PATHS
    assert skip_paths({"skip_paths": ["x/.*"]}) == ("x/.*",)
    assert skip_paths({"skip_paths": "x"}) == ("x",)
    with pytest.raises(re.error):
        skip_regex(["("])


def test_a_wiki_resource_page_is_never_a_document() -> None:
    """Appropedia's file pages under ``_assets_/`` hold only the wiki's menus (2026-10-16)."""
    assert is_default_listing("_assets_/f8eae675/File:Main_board.pdf")
    assert is_default_listing("appropedia_en_all/_assets_/f8eae675/File:Main_board.pdf")  # in a bundle
    assert is_default_listing("questions/tagged/first-aid")
    assert not is_default_listing("Solar_cooker")
    assert not is_default_listing("Notes_on_assets_/x")
    # third-party frames a web archive kept (2026-10-17 gap census)
    for frame in (
        "challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/f/ov2/av0/rch/oyf0k/light/normal?lang=auto",
        "ghbtns.com/github-btn.html?user=less&repo=less.js&type=fork",
        "www.google.com/recaptcha/api2/anchor?ar=1&k=6Lce",
        "www.google.com/maps/embed?pb=!1m18",
        "youtube.fuzzy.replayweb.page/embed/aF5vI0NnSdA",
        "womenshistory.si.edu/media/oembed?url=https:/www.youtube.com/watch?v=6elfkbQQPew",
        "embed.spotify.com/?uri=spotify:track:2wd52lU3agY0P3x2hxPYhm",
        "lesscss.org_en_all/ghbtns.com/github-btn.html",  # in a bundle
    ):
        assert is_default_listing(frame), frame
    assert not is_default_listing("www.google.com/search/howsearchworks")
    assert not is_default_listing("womenshistory.si.edu/media/oembed-guide")
    assert re.fullmatch(skip_regex(DEFAULT_SKIP_PATHS), "_assets_/f8/File:OS_policy.pdf")
