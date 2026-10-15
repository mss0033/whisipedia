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


def test_soft_hyphens_and_unmapped_glyphs_are_cleaned() -> None:
    from wsp_api.convert import readable_text

    assert readable_text("inter­national 30�min") == "international 30 min"
    assert plain_snippet("the inter­<b>national</b>") == "the international"
    assert readable_text(None) is None


def test_historical_notice_names_the_year() -> None:
    from datetime import UTC, datetime

    from wsp_api.convert import dated_banner
    from wsp_core.banners import HISTORICAL_BANNER, WITHDRAWN_BANNER

    when = datetime(1975, 1, 1, tzinfo=UTC)
    assert dated_banner(HISTORICAL_BANNER, when) == (
        "Historical document (1975) — methods may be outdated or unsafe."
    )
    both = f"{HISTORICAL_BANNER} {WITHDRAWN_BANNER}"
    assert (
        dated_banner(both, when)
        == f"Historical document (1975) — methods may be outdated or unsafe. {WITHDRAWN_BANNER}"
    )
    assert dated_banner(HISTORICAL_BANNER, None) == HISTORICAL_BANNER
    assert dated_banner("Superseded edition — a newer edition exists.", when) == (
        "Superseded edition — a newer edition exists."
    )
    assert dated_banner(None, when) is None
