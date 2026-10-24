"""Anonymous FTP for issuers whose bulk files are served over ``ftp://`` (owner decision D16, 2026-10-24).

NCBI serves PubMed, PubChem and PMC from ``ftp.ncbi.nlm.nih.gov`` over HTTPS and FTP; the HTTPS host's
robots.txt disallows every agent, and FTP has no robots.txt. The owner allowed the FTP channel for
issuers that document it as their bulk channel (PLAN §16, D16). This module is the FTP half of
:class:`~wsp_fetch.http.HttpFetcher`: the fetcher routes ``ftp://`` URLs here, so adapters need no
change.

* A directory (a URL ending in ``/``) is returned as a minimal HTML index, one
  ``<a href="NAME">NAME</a> YYYY-MM-DD HH:MM SIZE`` line per file from ``MLSD``, the form adapters
  already read from HTTP directory listings (``listing_links``, ``listing_rows``).
* A file is read with ``RETR`` in binary mode, resumed with ``REST`` from a partial download.

The login is the anonymous convention with the password ``anonymous@``: never an email address.
NCBI's server offers no TLS (``AUTH TLS`` is refused), so the bytes travel unencrypted; integrity rests
on the issuer's MD5 sidecars, which arrive over the same channel (docs/runbooks/document-collections.md).
"""

from __future__ import annotations

import ftplib
import html
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from urllib.parse import unquote, urlsplit

ANON_USER = "anonymous"
ANON_PASS = "anonymous@"  # noqa: S105 - the anonymous-FTP convention; never the user's email (privacy rule)

TRANSIENT = (ftplib.error_temp, ftplib.error_reply, ftplib.error_proto, EOFError, OSError)


class FtpNotFoundError(Exception):
    """The server answered 550 (no such file or directory)."""


def _split(url: str) -> tuple[str, int, str]:
    parts = urlsplit(url)
    if parts.scheme != "ftp" or not parts.hostname:
        raise ValueError(f"not an ftp:// URL: {url!r}")
    return parts.hostname, parts.port or 21, unquote(parts.path) or "/"


def connect(url: str, *, timeout: float) -> ftplib.FTP:
    host, port, _ = _split(url)
    ftp = ftplib.FTP(timeout=timeout)  # noqa: S321 - the issuer offers no TLS (D16; see the module notes)
    ftp.connect(host, port)
    ftp.login(ANON_USER, ANON_PASS)
    ftp.voidcmd("TYPE I")
    return ftp


def _modified(fact: str) -> str:
    try:
        return datetime.strptime(fact[:14], "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ""


def listing_html(rows: list[tuple[str, dict[str, str]]]) -> bytes:
    """An ``MLSD`` listing as the HTML index adapters read (files and subdirectories, sorted)."""
    lines = ["<html><body><pre>"]
    for name, facts in sorted(rows):
        kind = facts.get("type", "file")
        if kind in ("cdir", "pdir") or name in (".", ".."):
            continue
        href = name + "/" if kind == "dir" else name
        size = facts.get("size", "-") if kind == "file" else "-"
        esc = html.escape(href, quote=True)
        lines.append(f'<a href="{esc}">{html.escape(href)}</a> {_modified(facts.get("modify", ""))} {size}')
    lines.append("</pre></body></html>")
    return ("\n".join(lines) + "\n").encode()


def _not_found(exc: ftplib.error_perm) -> bool:
    return str(exc).startswith("550")


def get(url: str, *, max_bytes: int, timeout: float, on_chunk: Callable[[int], None]) -> bytes:
    """A directory's index (``url`` ends in ``/``) or a small file's bytes, at most ``max_bytes``."""
    _, _, path = _split(url)
    with closing(connect(url, timeout=timeout)) as ftp:
        try:
            if path.endswith("/"):
                ftp.cwd(path)
                return listing_html(list(ftp.mlsd(facts=["type", "size", "modify"])))
            out = bytearray()

            def sink(chunk: bytes) -> None:
                out.extend(chunk)
                on_chunk(len(chunk))
                if len(out) > max_bytes:
                    raise FtpTooLargeError(f"{url}: body exceeds max {max_bytes} bytes")

            ftp.retrbinary(f"RETR {path}", sink)
            return bytes(out)
        except ftplib.error_perm as exc:
            if _not_found(exc):
                raise FtpNotFoundError(f"{url}: {exc}") from exc
            raise


def size(url: str, *, timeout: float) -> int | None:
    """The file's size (``SIZE`` in binary mode), or None when the server does not say."""
    _, _, path = _split(url)
    with closing(connect(url, timeout=timeout)) as ftp:
        try:
            got = ftp.size(path)
        except ftplib.error_perm as exc:
            if _not_found(exc):
                raise FtpNotFoundError(f"{url}: {exc}") from exc
            return None
        return int(got) if got is not None else None


def retrieve(url: str, *, offset: int, timeout: float, sink: Callable[[bytes], None]) -> None:
    """Stream the file from byte ``offset`` (``REST``) into ``sink``."""
    _, _, path = _split(url)
    with closing(connect(url, timeout=timeout)) as ftp:
        try:
            ftp.retrbinary(f"RETR {path}", sink, rest=offset or None)
        except ftplib.error_perm as exc:
            if _not_found(exc):
                raise FtpNotFoundError(f"{url}: {exc}") from exc
            raise


class FtpTooLargeError(Exception):
    """A small-file read went over its byte budget."""
