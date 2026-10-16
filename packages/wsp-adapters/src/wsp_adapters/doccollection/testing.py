"""Tiny synthetic documents for doc-collection tests (no real upstream content is stored)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: Sequence[Sequence[str | tuple[float, float, str]]]) -> bytes:
    """A valid PDF with one Helvetica text line per string, one page per inner sequence. A line given
    as ``(x, y, text)`` is drawn there (PDF points from the bottom left); a string is the next line
    down the left margin."""
    objects: list[bytes] = []
    n_pages = len(pages)
    page_ids = [3 + 2 * i for i in range(n_pages)]
    font_id = 3 + 2 * n_pages
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    for i, lines in enumerate(pages):
        placed = [
            line if isinstance(line, tuple) else (50.0, 750.0 - 14 * n, line) for n, line in enumerate(lines)
        ]
        content = (
            "BT /F1 11 Tf "
            + " ".join(f"1 0 0 1 {x:g} {y:g} Tm ({_escape(t)}) Tj" for x, y, t in placed)
            + " ET"
        )
        page = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {page_ids[i] + 1} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>"
        )
        objects.append(page.encode())
        data = content.encode("latin-1")
        objects.append(b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for num, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def s3_listing(bucket_prefix: str, objects: Sequence[tuple[str, bytes]]) -> bytes:
    rows = "".join(
        f"<Contents><Key>{bucket_prefix}{key}</Key><LastModified>2026-10-01T00:00:00.000Z</LastModified>"
        f"<ETag>&quot;{md5(data)}&quot;</ETag><Size>{len(data)}</Size></Contents>"
        for key, data in objects
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<Name>bucket</Name><Prefix>{bucket_prefix}</Prefix><IsTruncated>false</IsTruncated>{rows}"
        "</ListBucketResult>"
    ).encode()


PUBLIC_A = [
    "FIELD MANUAL 5-0 PLANNING AND ORDERS PRODUCTION",
    "DISTRIBUTION RESTRICTION: Approved for public release; distribution is unlimited.",
    "This publication supersedes FM 5-0, dated 16 May 2022.",
]
RESTRICTED_C = [
    "TECHNICAL REPORT",
    "DISTRIBUTION STATEMENT C. Distribution authorized to U.S. Government agencies and their contractors.",
]
EXPORT_WARNING = [
    "ENGINEERING NOTE",
    "WARNING - This document contains technical data whose export is restricted",
    "by the Arms Export Control Act.",
]
