"""Outside links the library holds (wsp_api.outside): URL -> the library's copy (2026-10-17 census)."""

# ruff: noqa: E501 - tables of real URLs read better unwrapped

from pathlib import Path

import pytest

from wsp_api.htmlrewrite import rewrite_html
from wsp_api.outside import OutsideLinks, Site
from wsp_api.testing.zims import write_zim
from wsp_api.zimread import InProcessZimReader

SITES = [
    Site("wiki", "en.wikipedia.org", "wikipedia-en-zim"),
    Site("wiki", "www.appropedia.org", "kiwix-appropedia-en-all"),
    Site("wiki", "en.wikibooks.org", "kiwix-curated-t2", "wikibooks_en_all_maxi/"),
    Site("qa", "askubuntu.com", "stackexchange", "askubuntu.com_en_all/"),
    Site("web", "docs.python.org", "kiwix-docs-python-org-en-all"),
    Site("web", "xkcd.com", "kiwix-xkcd-com-en-all"),
]
HELD = {s.source_id for s in SITES} | {
    "gutenberg",
    "rfc",
    "us-code",
    "ecfr-federal-register",
    "pubmed",
    "open-library",
}


@pytest.mark.parametrize(
    ("url", "target"),
    [
        ("https://en.wikipedia.org/wiki/Alan_Turing#Early_life", (("wikipedia-en-zim", "Alan_Turing"), "Early_life")),
        ("https://en.m.wikipedia.org/wiki/Enigma%20machine", (("wikipedia-en-zim", "Enigma_machine"), "")),
        ("https://en.wikipedia.org/w/index.php?title=Turing_test", (("wikipedia-en-zim", "Turing_test"), "")),
        ("https://www.appropedia.org/Solar_cooker", (("kiwix-appropedia-en-all", "Solar_cooker"), "")),
        ("https://appropedia.org/Solar_cooker", (("kiwix-appropedia-en-all", "Solar_cooker"), "")),
        ("https://en.wikibooks.org/wiki/Cookbook:Bread", (("kiwix-curated-t2", "wikibooks_en_all_maxi/Cookbook:Bread"), "")),
        (
            "https://askubuntu.com/questions/12/how-to-x?noredirect=1",
            (("stackexchange", "askubuntu.com_en_all/questions/12"), ""),  # sotoki redirects the bare id
        ),
        ("https://askubuntu.com/q/12", (("stackexchange", "askubuntu.com_en_all/questions/12"), "")),
        ("https://docs.python.org/3/library/http.html", (("kiwix-docs-python-org-en-all", "docs.python.org/3/library/http.html"), "")),
        ("https://xkcd.com/1478/", (("kiwix-xkcd-com-en-all", "xkcd.com/1478/"), "")),
        # a subdomain of a crawled site is in that site's archive under its own name
        ("https://imgs.xkcd.com/comics/p_values.png", (("kiwix-xkcd-com-en-all", "imgs.xkcd.com/comics/p_values.png"), "")),
        ("http://www.gutenberg.org/ebooks/1342", (("gutenberg", "1342"), "")),
        ("https://www.rfc-editor.org/rfc/rfc9110.html#section-9", (("rfc", "rfc:RFC-9110"), "section-9")),
        ("https://datatracker.ietf.org/doc/html/rfc791", (("rfc", "rfc:RFC-791"), "")),
        ("https://pubmed.ncbi.nlm.nih.gov/21792337/", (("pubmed", "21792337"), "")),
        ("https://openlibrary.org/works/OL45804W/Fantastic_Mr_Fox", (("open-library", "works/OL45804W"), "")),
        ("https://www.law.cornell.edu/uscode/text/26/501", (("us-code", "t26/s501"), "")),
        (
            "https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title42-section1983&num=0",
            (("us-code", "t42/s1983"), ""),
        ),
        ("https://www.ecfr.gov/current/title-21/chapter-I/part-101/section-101.9", (("ecfr-federal-register", "t21/s101.9"), "")),
        ("https://www.law.cornell.edu/cfr/text/21/101.9", (("ecfr-federal-register", "t21/s101.9"), "")),
    ],
)  # fmt: skip
def test_an_outside_url_the_library_holds(url: str, target: object) -> None:
    assert OutsideLinks(SITES, HELD.__contains__).target(url) == target


@pytest.mark.parametrize(
    "url",
    [
        "https://de.wikipedia.org/wiki/Paris",  # not held (and a wiki's parent domain is not its site)
        "https://com/x",
        "https://example.org/x",
        "mailto:someone@example.org",
        "https://askubuntu.com/users/5/someone",  # a Q&A site's pages other than questions
        "https://en.wikipedia.org/",
        "https://arxiv.org/abs/1706.03762",  # no rule (and no such source)
    ],
)
def test_an_outside_url_the_library_does_not_hold(url: str) -> None:
    assert OutsideLinks(SITES, HELD.__contains__).target(url) is None


def test_a_held_rule_needs_its_source() -> None:
    links = OutsideLinks(SITES, lambda s: s != "rfc")
    assert links.target("https://www.rfc-editor.org/rfc/rfc9110") is None


def test_rewrite_sends_outside_links_and_pictures_to_the_library() -> None:
    links = OutsideLinks(SITES, HELD.__contains__)

    def outside(url: str, *, asset: bool) -> str | None:
        hit = links.target(url)
        if hit is None:
            return None
        (sid, native), frag = hit
        return (
            f"/v1/assets/{sid}/G/{native}"
            if asset
            else f"wsp://{sid}/{native}" + (f"#{frag}" if frag else "")
        )

    held = {"learningstatisticswithr.com/own.png"}
    out = rewrite_html(
        '<a href="https://en.wikipedia.org/wiki/P-value#History">p</a>'
        '<a href="https://example.org/x">x</a>'
        '<img src="https://imgs.xkcd.com/comics/p_values.png">'
        '<img src="../imgs.xkcd.com/comics/significant.png">'  # the crawl did not keep it
        '<img src="own.png">'
        '<img src="../xkcd.com/s/logo.png">'  # a copy of an xkcd page here, its logo only in xkcd's archive
        '<a href="https://imgs.xkcd.com/comics/p_values.png">big</a>',
        source_id="lsr",
        generation_id="g",
        native_id="learningstatisticswithr.com/ch1.html",
        find=lambda p: p if p in held else None,
        outside=outside,
    )
    assert out == (
        '<a href="wsp://wikipedia-en-zim/P-value#History">p</a>'
        '<a href="https://example.org/x">x</a>'
        '<img src="/v1/assets/kiwix-xkcd-com-en-all/G/imgs.xkcd.com/comics/p_values.png">'
        '<img src="/v1/assets/kiwix-xkcd-com-en-all/G/imgs.xkcd.com/comics/significant.png">'
        '<img src="/v1/assets/lsr/g/learningstatisticswithr.com/own.png">'
        '<img src="/v1/assets/kiwix-xkcd-com-en-all/G/xkcd.com/s/logo.png">'
        '<a href="/v1/assets/kiwix-xkcd-com-en-all/G/imgs.xkcd.com/comics/p_values.png">big</a>'
    )


def test_the_site_an_archive_copies(tmp_path: Path) -> None:
    reader = InProcessZimReader()
    wiki = write_zim(tmp_path / "w.zim", "wikipedia_en_all", [("Main", "text/html", "<p>x</p>")],
                     metadata={"Scraper": "mwoffliner 1.17.5", "Source": "en.wikipedia.org"})  # fmt: skip
    qa = write_zim(tmp_path / "q.zim", "askubuntu.com_en_all", [("questions", "text/html", "<p>x</p>")],
                   metadata={"Scraper": "sotoki v3.0.2"})  # fmt: skip
    web = write_zim(tmp_path / "d.zim", "docs.python.org_en_all", [("docs.python.org/3/", "text/html", "<p>x</p>")],
                    metadata={"Scraper": "warc2zim 2.3.1,zimit 3.1.3"})  # fmt: skip
    other = write_zim(tmp_path / "o.zim", "gutenberg_mul_all", [("A", "text/html", "<p>x</p>")],
                      metadata={"Scraper": "gutenberg2zim"})  # fmt: skip
    assert reader.site(str(wiki)) == ("wiki", "en.wikipedia.org")
    assert reader.site(str(qa)) == ("qa", "askubuntu.com")
    assert reader.site(str(web)) == ("web", "docs.python.org")
    assert reader.site(str(other)) is None
