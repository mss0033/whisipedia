"""Secrets out of text that is printed, logged or stored (trawl 2026-11-10, core-correctness-04).

The api.data.gov key travelled as ``?api_key=...``, so every error that named the URL printed it: 21
lane logs held it in clear. One helper masks the value of every secret-named query parameter; the
fetch errors apply it to their own text (:class:`wsp_core.errors.FetchError`), so a new error site
cannot forget it. It changes text only: a ``.url`` attribute keeps the real URL for the code that
needs it, and must never be printed without this helper.
"""

from __future__ import annotations

import re

# ``?api_key=``, ``&api-key=``, ``&apikey=``, ``access_token``, ``token``, ``signature``, ``sig``; the
# name must be the whole parameter name (``resumptionToken=`` is a page mark, not a secret).
_SECRET_PARAM = re.compile(r"(?i)([?&](?:api[_-]?key|access_token|token|signature|sig)=)[^&#\s\"'<>]+")

MASK = "***"


def redact_url_secrets(text: str) -> str:
    """``text`` with the value of every secret-named query parameter replaced by ``***``."""
    return _SECRET_PARAM.sub(rf"\1{MASK}", text)
