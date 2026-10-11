from wsp_api.browse import _readable  # pyright: ignore[reportPrivateUsage]
from wsp_api.search import highlights, plain_snippet


def test_plain_snippet_strips_markup_and_highlights_plain_text() -> None:
    plain = plain_snippet("the <b>insulin</b> &amp; <b>insulins</b>")
    assert plain == "the insulin & insulins"
    assert [plain[a:b] for a, b in highlights(plain, ["insulin"])] == ["insulin", "insulins"]


def test_readable_titles_for_the_start_page() -> None:
    assert _readable("Insulin")
    assert _readable("Ångström")
    assert not _readable("\U0003f850")  # supplementary-plane glyph: renders blank
    assert not _readable("")
