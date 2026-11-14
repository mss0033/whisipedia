"""Secrets never reach error text (trawl 2026-11-10, core-correctness-04: the api.data.gov key was in
2,884 lines of 21 lane logs, printed by errors that named the request URL)."""

from __future__ import annotations

import pytest

from wsp_core.errors import (
    ChecksumMismatchError,
    FetchError,
    NotFoundError,
    RobotsDisallowedError,
    RobotsUnreachableError,
)
from wsp_core.redact import redact_url_secrets

KEY = "SECRETSECRETSECRETSECRET"
URL = f"https://api.govinfo.gov/collections/BILLS/2020?pageSize=1000&offsetMark=%2A&api_key={KEY}"


@pytest.mark.parametrize(
    "name", ["api_key", "API_KEY", "apikey", "api-key", "access_token", "token", "signature", "sig"]
)
def test_api_key_and_token_values_are_masked(name: str) -> None:
    for text in (f"https://h/x?{name}={KEY}", f"GET https://h/x?a=1&{name}={KEY}&b=2: HTTP 500"):
        out = redact_url_secrets(text)
        assert KEY not in out
        assert f"{name}=***" in out
    assert redact_url_secrets(f"https://h/x?a=1&{name}={KEY}&b=2 failed").endswith("=***&b=2 failed")


def test_other_parameters_and_plain_text_are_untouched() -> None:
    for text in (
        "https://h/x?pageSize=1000&offsetMark=%2A",
        "https://h/oai?verb=ListRecords&resumptionToken=abc123",  # a page mark, not a secret
        "https://h/x?design=1&monkey=2",
        "md5 mismatch for https://h/a.pdf: expected ab, got cd",
        "the token=abc in prose without a query mark",
        "",
    ):
        assert redact_url_secrets(text) == text


def test_fetch_error_text_never_holds_a_key() -> None:
    errors: list[FetchError] = [
        FetchError(f"GET {URL} failed after 5 attempt(s): HTTP 500"),
        RobotsDisallowedError(f"{URL}: disallowed by api.govinfo.gov/robots.txt"),
        RobotsUnreachableError(f"{URL}: disallowed by api.govinfo.gov/robots.txt (unreachable)"),
        NotFoundError(URL, "HTTP 404"),
        ChecksumMismatchError(URL, "md5", "ab", "cd"),
    ]
    for exc in errors:
        assert KEY not in str(exc), type(exc).__name__
        assert KEY not in repr(exc), type(exc).__name__
        assert "api_key=***" in str(exc)
        assert "pageSize=1000&offsetMark=%2A" in str(exc)  # the rest of the URL is still told


def test_the_url_attribute_keeps_the_real_url_and_other_arguments_their_type() -> None:
    assert NotFoundError(URL).url == URL
    assert ChecksumMismatchError(URL, "md5", "ab", "cd").url == URL
    exc = FetchError("plain text", 7)
    assert exc.args == ("plain text", 7)
