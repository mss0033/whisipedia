"""Reader banners derived from ``DocRecord.meta`` (PLAN D13; research doc §2.2).

Historical guidance (old Farmers' Bulletins, Cold War civil defense manuals, superseded doctrine)
stays in the library and is shown with a period-style banner instead of being hidden. The adapter
decides the label; this module only turns the stored metadata into what readers see, so the API,
SDK and Navigator agree on one rule.
"""

from __future__ import annotations

from collections.abc import Mapping

HISTORICAL_BANNER = "Historical document — methods may be outdated or unsafe."
# A document the issuer no longer lists ("withdrawn-from-index" names the issuer's index): kept, but
# the reader is told it may have been withdrawn (2026-10-12 audit, CA-8).
WITHDRAWN_BANNER = "No longer listed by the issuer — it may have been withdrawn or replaced."
CURRENCIES = ("current", "superseded", "historical", "rescinded", "withdrawn-from-index")


def is_historical(meta: Mapping[str, str]) -> bool:
    return meta.get("historical", "").lower() in ("true", "1", "yes") or meta.get("currency") == "historical"


def currency_of(meta: Mapping[str, str]) -> str | None:
    value = meta.get("currency")
    return value if value in CURRENCIES else None


def banner_for(meta: Mapping[str, str]) -> str | None:
    """The banner text for a document, or ``None`` for a current document without a warning."""
    withdrawn = meta.get("currency") == "withdrawn-from-index"
    if is_historical(meta):
        return f"{HISTORICAL_BANNER} {WITHDRAWN_BANNER}" if withdrawn else HISTORICAL_BANNER
    explicit = meta.get("banner", "").strip()
    if explicit:
        return f"{explicit} {WITHDRAWN_BANNER}" if withdrawn else explicit
    if meta.get("currency") == "superseded":
        return "Superseded edition — a newer edition exists."
    if meta.get("currency") == "rescinded":
        return "Rescinded by the issuer — do not follow."
    if withdrawn:
        return WITHDRAWN_BANNER
    return None
