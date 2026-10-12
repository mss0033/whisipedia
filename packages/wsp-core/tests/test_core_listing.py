import re

import pytest

from wsp_core.listing import DEFAULT_SKIP_PATHS, skip_paths, skip_regex


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
