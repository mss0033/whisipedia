"""RFC 9309 robots.txt matching (2026-10-24: urllib.robotparser ignored ``*`` and ``$``)."""

from __future__ import annotations

from wsp_core.robots import Robots, parse, product_token

UA = "Whisipedia/0.1 (personal offline archive)"


def allowed(body: str, url: str) -> bool:
    return Robots(body).can_fetch(UA, url)


def test_wildcards_and_end_anchors_match_as_the_rfc_says() -> None:
    leg = "User-agent: *\nCrawl-delay: 5\nDisallow: */data.pdf\nDisallow: /defralex\n"
    assert not allowed(leg, "https://www.legislation.gov.uk/ukpga/2010/15/data.pdf")
    assert allowed(leg, "https://www.legislation.gov.uk/ukpga/2010/15/data.html")
    assert not allowed("User-agent: *\nDisallow: /*_pdf\n", "https://j.example/article/x/1_1/_pdf")
    assert not allowed("User-agent: *\nDisallow: /server/oai/*\n", "https://iris.who.int/server/oai/request")
    end = "User-agent: *\nDisallow: /*.pdf$\n"
    assert not allowed(end, "https://e.org/a/b.pdf")
    assert allowed(end, "https://e.org/a/b.pdf?page=2")  # $ ends the path: a query follows it here
    assert Robots(leg).crawl_delay(UA) == 5.0


def test_the_longest_rule_wins_and_allow_wins_a_tie() -> None:
    dspace = (
        "User-agent: *\nAllow: /server/api/core/bitstreams\nDisallow: /server/api/*\nDisallow: /server/api\n"
    )
    assert allowed(dspace, "https://iris.who.int/server/api/core/bitstreams/abc/content")
    assert not allowed(dspace, "https://iris.who.int/server/api/core/items/abc")
    assert allowed("User-agent: *\nDisallow: /a\nAllow: /a\n", "https://e.org/a/b")
    assert not allowed("User-agent: *\nAllow: /\nDisallow: /private\n", "https://e.org/private/x")


def test_our_own_group_beats_the_star_group_and_groups_merge() -> None:
    body = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: whisipedia\nDisallow: /secret\n\n"
        "User-agent: Googlebot\nUser-agent: WHISIPEDIA\nDisallow: /also\n"
    )
    assert allowed(body, "https://e.org/page")
    assert not allowed(body, "https://e.org/secret/1")
    assert not allowed(body, "https://e.org/also")
    assert product_token(UA) == "whisipedia"
    assert not allowed("User-agent: *\nDisallow: /\n", "https://e.org/x")
    assert allowed("User-agent: Googlebot\nDisallow: /\n", "https://e.org/x")  # no group for us
    assert allowed("User-agent: *\nDisallow:\n", "https://e.org/x")  # an empty Disallow allows all


def test_robots_txt_itself_percent_encoding_and_non_files() -> None:
    assert allowed("User-agent: *\nDisallow: /\n", "https://e.org/robots.txt")
    assert not allowed("User-agent: *\nDisallow: /a%20b\n", "https://e.org/a b/c")
    assert parse(b"<!DOCTYPE html><html>not found</html>") is None
    assert parse(None) is None
    bom = parse("﻿User-agent: *\nDisallow: /x\n".encode())
    assert bom is not None
    assert not bom.can_fetch(UA, "https://e.org/x")  # a byte-order mark does not hide the first rule
