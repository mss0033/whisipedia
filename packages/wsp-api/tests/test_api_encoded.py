"""Archived text files still in their transfer encoding (wsp_api.encoded)."""

from __future__ import annotations

import gzip

import pytest

from wsp_api import encoded
from wsp_api.encoded import compress_brotli as br
from wsp_api.encoded import decoded, html_as_image

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 30 30"><circle r="15"/></svg>'


def test_a_text_file_stored_brotli_or_gzip_encoded_is_served_decoded() -> None:
    # anonymousplanet.org's cc.svg was brotli bytes labelled image/svg+xml (2026-10-21 audit)
    assert decoded(br(SVG), "image/svg+xml") == SVG
    assert decoded(gzip.compress(SVG), "image/svg+xml") == SVG
    assert decoded(br(b"body { color: red }"), "text/css; charset=utf-8") == b"body { color: red }"


def test_anything_else_is_served_as_it_is() -> None:
    assert decoded(SVG, "image/svg+xml") == SVG  # already text
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    assert decoded(png, "image/png") == png  # not a text type
    assert decoded(br(SVG), "image/png") == br(SVG)
    noise = bytes(range(1, 9)) * 10
    assert decoded(noise, "image/svg+xml") == noise  # does not decode: left alone
    assert decoded(b"", "text/css") == b""


def test_a_decoding_too_large_is_not_made(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(encoded, "MAX_OUTPUT", 1000)
    big = b"<p>" + b"a" * 5000 + b"</p>"
    assert decoded(br(big), "text/html") == br(big)
    assert decoded(gzip.compress(big), "text/html") == gzip.compress(big)


def test_a_web_page_saved_under_a_pictures_name_is_no_picture() -> None:
    page = b'<!doctype html><html lang="en-US"><head><title>OpenStax</title>'
    assert html_as_image(page, "image/png")
    assert html_as_image(b"\n  <HTML><body>", "image/jpeg")
    assert not html_as_image(b"\x89PNG\r\n\x1a\n", "image/png")
    assert not html_as_image(SVG, "image/svg+xml")
    assert not html_as_image(page, "text/html")
