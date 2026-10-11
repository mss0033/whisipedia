"""Re-export of :mod:`wsp_adapters.doccollection.testing` for the adapter tests."""

from __future__ import annotations

from wsp_adapters.doccollection.testing import (
    EXPORT_WARNING,
    PUBLIC_A,
    RESTRICTED_C,
    make_pdf,
    md5,
    s3_listing,
)

__all__ = ["EXPORT_WARNING", "PUBLIC_A", "RESTRICTED_C", "make_pdf", "md5", "s3_listing"]
