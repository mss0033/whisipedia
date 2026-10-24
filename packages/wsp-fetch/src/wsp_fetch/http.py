"""``HttpFetcher``: segmented, multi-mirror, hash-verified HTTP downloads (PLAN §6.5, §8.1).

Concurrency model: plain threads over one shared ``httpx.Client``. Every hot operation releases the
GIL (socket reads, ``os.pwrite``, ``hashlib``/``blake3`` on large buffers), connection counts are
small (per-host caps of 2-8), and a sync API keeps the ``Fetcher`` port simple; asyncio would still
need executor threads for hashing and disk I/O.

Pipeline for one file::

    probe mirrors (Range 0-0) ─► piece queue (lowest index first, work-stealing)
        N workers per mirror (N = host cap) ─► piece buffer in RAM ─► piece digest check
        ─► pwrite into <dest>.partial ─► in-order hasher (SHA-256 + BLAKE3 + upstream algos)
    checkpoint: fdatasync(.partial) then write <dest>.partial.state.json
    finish: fsync, compare digests, optional read-back, rename

Failing mirrors are dropped after consecutive errors, slow ones are demoted to one connection, a
piece that fails its Metalink hash is never retried on the mirror that served it, and when the
queue drains idle connections duplicate straggling pieces (endgame) — first verified copy wins.

Safety rules (vault-fetch audit #8, #15, #16, LOW):

* Without upstream digests *and* piece hashes, bytes are never spliced from several mirrors (two
  mirrors of the same size may hold different files); one source serves the whole file, and the
  ``unverified_policy`` (see :class:`FetchConfig`) decides: corroborate with a second full
  download, accept visibly, or refuse.
* Resuming requires the same validator (ETag/Last-Modified) as the interrupted run, unless every
  resumed piece is checked against upstream piece hashes.
* A body without a checksum must have the size the probe saw (Content-Length or Content-Range); a
  short, close-delimited body is retried, also when it is the only source.
* A signed redirect URL that expires mid-download (401/403/410) is re-resolved from the original
  URL instead of dropping the mirror.
* HTTPS is never downgraded: a redirect from ``https`` to ``http`` fails the request.
* Every host's robots.txt is honoured (``FetchConfig.robots``): a request, a mirror or a redirect
  target it disallows is never asked for (read once per host; unreadable sets no rules). 2026-10-23:
  only document collections had checked it, and PubMed, the OSM planet and some Kiwix mirrors and
  checksum files were on hosts that disallow us.
* A caller may guard where redirects lead (:func:`wsp_core.redirects.redirect_guard`): each hop is
  checked before it is followed, in every thread of the download (2026-10-23: BODC's GEBCO link
  redirected to CEDA, whose robots.txt disallows us; 7.5 GB came from there before any check ran).
* Politeness caps count against the host we were asked to fetch from, not the host a redirect
  lands on; one destination is fetched by one process at a time (a local ``flock``).
* Spot checks pick their ranges at random (unpredictable to a mirror) and run before the file is
  published.
"""

from __future__ import annotations

import bisect
import contextvars
import dataclasses
import email.utils
import fcntl
import hashlib
import logging
import os
import random
import re
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from wsp_core.errors import RedirectRefusedError, RobotsDisallowedError
from wsp_core.model import RemoteFile, RemoteHead
from wsp_core.redirects import current_guard, unguarded
from wsp_fetch import ftp
from wsp_fetch import provenance as prov
from wsp_fetch.config import FetchConfig, host_key
from wsp_fetch.errors import (
    ChecksumMismatchError,
    FetchCancelledError,
    FetchError,
    FetchLockedError,
    MirrorsExhaustedError,
    NotFoundError,
    ResponseTooLargeError,
    SizeMismatchError,
    UnverifiableError,
)
from wsp_fetch.finish import FetchReport, finalize, fsync_dir, make_report
from wsp_fetch.hashing import InOrderHasher, MultiHasher, hash_file, new_hasher, pread_exact
from wsp_fetch.ratelimit import TokenBucket
from wsp_fetch.state import PartialState, load_state, partial_path, save_state, state_path
from wsp_fetch.target import Target, resolve_target

log = logging.getLogger(__name__)

_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")
_CONTENT_RANGE_EMPTY = re.compile(r"bytes\s+\*/(\d+)")
_RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_NOT_FOUND = {404, 410}
_EXPIRED = {401, 403, 410}  # what a signed redirect URL answers once it has expired


def _spot_rng() -> random.Random:
    """Where spot-check offsets come from: unpredictable, so a mirror cannot serve the right bytes
    only where it expects to be checked."""
    return random.SystemRandom()


_IN_ROBOTS: contextvars.ContextVar[bool] = contextvars.ContextVar("wsp_fetch_in_robots", default=False)


def _guard_redirect(response: httpx.Response) -> None:
    """Response hook: the caller's :func:`wsp_core.redirects.redirect_guard`, if any, on the target."""
    check = current_guard()
    if check is None or not response.has_redirect_location:
        return
    check(str(response.request.url.join(response.headers["Location"])))


def _refuse_downgrade(response: httpx.Response) -> None:
    """Response hook: an ``https`` request may not be redirected to plain ``http``."""
    if not response.has_redirect_location or response.request.url.scheme != "https":
        return
    target = response.request.url.join(response.headers["Location"])
    if target.scheme != "https":
        raise _RetryableError(
            f"refusing redirect from {response.request.url} to insecure {target}", fatal_for_mirror=True
        )


# --- helpers ------------------------------------------------------------------------------------


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    current = datetime.now(UTC).timestamp() if now is None else now
    return max(0.0, when.timestamp() - current)


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with "equal jitter": half fixed, half random, never zero."""
    ceiling = min(cap, base * (2 ** max(0, attempt - 1)))
    return ceiling / 2 + random.uniform(0, ceiling / 2)  # noqa: S311 - jitter, not crypto


def choose_piece_size(size: int, config: FetchConfig) -> int:
    """Deterministic in ``size`` and config (so resume state stays valid across runs), 64 KiB aligned."""
    want = max(config.min_piece_size, min(config.piece_size, -(-size // 64)))
    return max(64 * 1024, -(-want // (64 * 1024)) * (64 * 1024))


class _RetryableError(FetchError):
    def __init__(
        self,
        msg: str,
        retry_after: float | None = None,
        *,
        fatal_for_mirror: bool = False,
        status: int | None = None,
    ) -> None:
        super().__init__(msg)
        self.retry_after = retry_after
        self.fatal_for_mirror = fatal_for_mirror
        self.status = status


class _CancelledError(Exception):
    pass


@dataclass(slots=True)
class _Probe:
    url: str
    final_url: str
    size: int | None
    ranges: bool
    validator: str | None
    observation: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(slots=True)
class _Mirror:
    id: int
    url: str  # as configured
    final_url: str  # after redirects; requests go here
    workers: int
    allowed: int = 0  # live connection budget; demotion lowers it
    disabled: bool = False
    consecutive_failures: int = 0
    corrupt: int = 0
    cooldown_until: float = 0.0
    bytes: int = 0
    rate: float = 0.0  # EWMA bytes/s per connection
    samples: int = 0
    pieces: list[int] = field(default_factory=list[int])  # pieces this mirror served (provenance)
    observation: dict[str, Any] = field(default_factory=dict[str, Any])
    spot_check: str = "skipped"
    reresolved: int = 0  # times final_url was re-resolved after it expired

    def record(self, nbytes: int, seconds: float) -> None:
        r = nbytes / max(seconds, 1e-6)
        self.rate = r if self.samples == 0 else 0.7 * self.rate + 0.3 * r
        self.samples += 1


@dataclass(slots=True)
class _Plan:
    target: Target
    dest: Path
    size: int
    piece_size: int
    piece_algorithm: str
    mirrors: list[_Mirror]
    validator: str | None
    rejected: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


@dataclass(slots=True)
class _Piece:
    index: int
    attempts: int = 0
    excluded: set[int] = field(default_factory=set[int])
    active: dict[int, float] = field(default_factory=dict[int, float])  # mirror id -> start time


# --- fetcher ------------------------------------------------------------------------------------


class HttpFetcher:
    """Reference ``Fetcher`` (PLAN §6.5). Thread-safe; host caps apply across concurrent downloads."""

    def __init__(
        self,
        config: FetchConfig | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        pace: Callable[[int], None] | None = None,
    ):
        """``pace``: called with the size of each chunk written, after the bandwidth limit: the library's
        shared budget for writes to the NAS (``wsp_cli.bootstrap.ingest_pace``), which also yields to a
        reader. Six collection lanes writing at internet speed made the API's reads take 0.5 s each
        and pages time out (2026-10-16)."""
        self._pace = pace
        self._pace_lock = threading.Lock()
        self.config = config or FetchConfig()
        c = self.config
        self._client = httpx.Client(
            headers={"User-Agent": c.user_agent, "Accept-Encoding": "identity"},
            timeout=httpx.Timeout(c.read_timeout, connect=c.connect_timeout),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=64),
            transport=transport,
            event_hooks={"response": [_refuse_downgrade, _guard_redirect, self._robots_hop]},
        )
        self._robots: dict[str, RobotFileParser | None] = {}  # "scheme://host" -> its rules
        self._robots_lock = threading.Lock()
        self._bucket = TokenBucket(c.bandwidth_limit) if c.bandwidth_limit else None
        self._hosts: dict[str, threading.BoundedSemaphore] = {}
        self._hosts_lock = threading.Lock()
        self._cancel = threading.Event()
        self._observed: dict[str, dict[str, Any]] = {}
        self._observed_lock = threading.Lock()

    # --- cancellation and provenance -----------------------------------------------------------

    def cancel(self) -> None:
        """Abort running and future transfers promptly (daemon shutdown): they raise
        :class:`FetchCancelledError`; resume state stays on disk. :meth:`reset` re-enables."""
        self._cancel.set()

    def reset(self) -> None:
        self._cancel.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def wait_cancelled(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; True (early) if the fetcher was cancelled meanwhile."""
        return self._cancel.wait(seconds)

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise FetchCancelledError("fetch cancelled (shutdown)")

    def observation(self, url: str) -> dict[str, Any] | None:
        """What the last request for ``url`` saw (final URL, redirects, TLS peer, validators)."""
        with self._observed_lock:
            seen = self._observed.get(url)
            return dict(seen) if seen is not None else None

    def _observe(self, url: str, response: httpx.Response) -> dict[str, Any]:
        seen = prov.observe(response, url)
        with self._observed_lock:
            if len(self._observed) > 4096:  # bounded: keep the newest observations
                self._observed.pop(next(iter(self._observed)))
            self._observed[url] = seen
        return seen

    @property
    def client(self) -> httpx.Client:
        return self._client

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def host_slot(self, url: str) -> Generator[None]:
        """Hold one of the per-host connection slots (politeness cap) for the duration."""
        key = host_key(url)
        with self._hosts_lock:
            sem = self._hosts.get(key)
            if sem is None:
                sem = self._hosts[key] = threading.BoundedSemaphore(self.config.connections_for(key))
        with sem:
            yield

    def throttle(self, nbytes: int) -> None:
        if self._bucket is not None:
            self._bucket.consume(nbytes)

    def wrote(self, nbytes: int) -> None:
        """Bytes written to the destination: paced by ``pace`` (one caller at a time: pieces complete
        on worker threads, and the pace's limiter is not thread-safe)."""
        if self._pace is not None:
            with self._pace_lock:
                self._pace(nbytes)

    # --- small documents ---------------------------------------------------------------------

    # --- robots.txt ------------------------------------------------------------------------------

    def robots_allows(self, url: str) -> bool:
        """Whether ``url``'s host lets us fetch it (its robots.txt, read once; unreadable: yes). An FTP
        service's rules are its ``ftp://HOST/robots.txt`` (RFC 9309 covers FTP: NCBI's disallows every
        agent, 2026-10-24)."""
        if not self.config.robots or _IN_ROBOTS.get():
            return True
        parts = urlsplit(url)
        site = f"{parts.scheme}://{parts.netloc.lower()}"
        with self._robots_lock:
            known = site in self._robots
            rules = self._robots.get(site)
        if not known:
            rules = self._read_robots(site)
            with self._robots_lock:
                self._robots[site] = rules
        return rules is None or rules.can_fetch(self.config.user_agent, url)

    def check_robots(self, url: str) -> None:
        if not self.robots_allows(url):
            raise RobotsDisallowedError(f"{url}: disallowed by {urlsplit(url).netloc}/robots.txt")

    def _read_robots(self, site: str) -> RobotFileParser | None:
        token = _IN_ROBOTS.set(True)
        try:
            if site.startswith("ftp://"):
                return _parse_robots(self._ftp_robots(site))
            with unguarded():
                r = self._client.get(f"{site}/robots.txt")
            if r.status_code != 200:
                return None
            return _parse_robots(r.content)
        except (httpx.HTTPError, OSError):
            return None
        finally:
            _IN_ROBOTS.reset(token)

    def _ftp_robots(self, site: str) -> bytes | None:
        try:
            return ftp.get(
                f"{site}/robots.txt",
                max_bytes=512 * 1024,
                timeout=self.config.read_timeout,
                on_chunk=self.throttle,
            )
        except (ftp.FtpNotFoundError, ftp.FtpTooLargeError, *ftp.TRANSIENT):
            return None

    def _robots_hop(self, response: httpx.Response) -> None:
        """Response hook: a redirect to a host whose robots.txt disallows the target is not followed."""
        if _IN_ROBOTS.get() or not response.has_redirect_location:
            return
        target = str(response.request.url.join(response.headers["Location"]))
        if not self.robots_allows(target):
            raise RedirectRefusedError(f"redirected to {target}: disallowed by its robots.txt")

    def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        self.check_robots(url)
        if url.startswith("ftp://"):
            return self._retrying(
                f"RETR {url}", lambda: self._ftp_get_once(url, max_bytes), self.config.max_attempts
            )
        return self._retrying(f"GET {url}", lambda: self._get_once(url, max_bytes), self.config.max_attempts)

    def _ftp_get_once(self, url: str, max_bytes: int) -> bytes:
        with self.host_slot(url):
            try:
                return ftp.get(
                    url, max_bytes=max_bytes, timeout=self.config.read_timeout, on_chunk=self.throttle
                )
            except ftp.FtpNotFoundError as exc:
                raise _RetryableError(str(exc), status=404) from exc
            except ftp.FtpTooLargeError as exc:
                raise ResponseTooLargeError(str(exc)) from exc
            except ftp.TRANSIENT as exc:
                raise _RetryableError(f"{type(exc).__name__}: {exc}") from exc

    def _retrying[T](self, label: str, fn: Callable[[], T], attempts: int) -> T:
        """Retry transient failures with jittered backoff, honouring ``Retry-After``."""
        attempt = 0
        while True:
            attempt += 1
            self.check_cancelled()
            try:
                return fn()
            except _RetryableError as exc:
                if exc.status in _NOT_FOUND:
                    raise NotFoundError(label.split(" ", 1)[-1], f"HTTP {exc.status}") from exc
                if attempt >= attempts or exc.fatal_for_mirror:
                    raise FetchError(f"{label} failed after {attempt} attempt(s): {exc}") from exc
                delay = backoff_delay(attempt, self.config.backoff_base, self.config.backoff_max)
                if exc.retry_after is not None:
                    if exc.retry_after > self.config.max_retry_after:
                        raise FetchError(f"{label}: Retry-After {exc.retry_after:.0f}s too long") from exc
                    delay = max(delay, exc.retry_after)
                log.info("%s: %s; retrying in %.1fs", label, exc, delay)
                if self._cancel.wait(delay):
                    raise FetchCancelledError(f"{label}: cancelled (shutdown)") from exc

    def _get_once(self, url: str, max_bytes: int) -> bytes:
        with self.host_slot(url):
            try:
                with self._client.stream("GET", url) as r:
                    self._observe(url, r)
                    _raise_for_status(r)
                    declared = r.headers.get("Content-Length")
                    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                        raise ResponseTooLargeError(f"{url}: {declared} bytes > max {max_bytes}")
                    out = bytearray()
                    for chunk in r.iter_raw():
                        self.check_cancelled()
                        out += chunk
                        self.throttle(len(chunk))
                        if len(out) > max_bytes:
                            raise ResponseTooLargeError(f"{url}: body exceeds max {max_bytes} bytes")
                    return bytes(out)
            except httpx.TransportError as exc:
                raise _RetryableError(f"{type(exc).__name__}: {exc}") from exc

    def get_range(self, url: str, start: int, length: int) -> bytes:
        """Bytes ``start``..``start+length-1`` of ``url`` (a 206 for exactly that range; retried).

        For readers of random-access files over HTTP (map archives cut by ``wsp maps fetch``): a server
        that ignores ``Range`` or answers another range is an error, never a silent whole body."""
        self.check_robots(url)
        if length <= 0:
            return b""
        end = start + length - 1

        def once() -> bytes:
            with self.host_slot(url):
                try:
                    with self._client.stream("GET", url, headers={"Range": f"bytes={start}-{end}"}) as r:
                        self._observe(url, r)
                        if r.status_code == 200:
                            raise _RetryableError("server ignored Range", fatal_for_mirror=True)
                        _raise_for_status(r)
                        cr = _CONTENT_RANGE.match(r.headers.get("Content-Range", ""))
                        if (
                            r.status_code != 206
                            or cr is None
                            or (int(cr.group(1)), int(cr.group(2))) != (start, end)
                        ):
                            raise _RetryableError(
                                f"bad partial response {r.status_code} {r.headers.get('Content-Range', '')}",
                                fatal_for_mirror=True,
                            )
                        out = bytearray()
                        for chunk in r.iter_raw():
                            self.check_cancelled()
                            out += chunk
                            self.throttle(len(chunk))
                            if len(out) > length:
                                raise _RetryableError("server sent more bytes than requested")
                except httpx.TransportError as exc:
                    raise _RetryableError(f"{type(exc).__name__}: {exc}") from exc
            if len(out) != length:
                raise _RetryableError(f"short read: {len(out)}/{length} bytes")
            return bytes(out)

        return self._retrying(f"GET {url} bytes={start}-{end}", once, self.config.max_attempts)

    def head(self, url: str) -> RemoteHead:
        """``HEAD`` with redirects followed (optional ``SupportsHead`` capability of the port); for
        ``ftp://``, the file's ``SIZE``."""
        self.check_robots(url)
        if url.startswith("ftp://"):

            def ftp_once() -> RemoteHead:
                with self.host_slot(url):
                    try:
                        return RemoteHead(url=url, size=ftp.size(url, timeout=self.config.read_timeout))
                    except ftp.FtpNotFoundError as exc:
                        raise _RetryableError(str(exc), status=404) from exc
                    except ftp.TRANSIENT as exc:
                        raise _RetryableError(f"{type(exc).__name__}: {exc}") from exc

            return self._retrying(f"SIZE {url}", ftp_once, self.config.max_attempts)

        def once() -> RemoteHead:
            with self.host_slot(url):
                try:
                    r = self._client.head(url)
                except httpx.TransportError as exc:
                    raise _RetryableError(f"{type(exc).__name__}: {exc}") from exc
            _raise_for_status(r)
            cl = r.headers.get("Content-Length")
            modified = r.headers.get("Last-Modified")
            when = None
            if modified:
                try:
                    when = email.utils.parsedate_to_datetime(modified)
                except (TypeError, ValueError):
                    when = None
            return RemoteHead(
                url=str(r.url),
                size=int(cl) if cl and cl.isdigit() else None,
                etag=r.headers.get("ETag"),
                last_modified=when,
                accepts_ranges=r.headers.get("Accept-Ranges", "").lower() == "bytes",
            )

        return self._retrying(f"HEAD {url}", once, self.config.max_attempts)

    # --- files -------------------------------------------------------------------------------

    def download(self, remote: RemoteFile, dest: Path) -> FetchReport:
        return self.fetch(remote, dest)

    def fetch(self, remote: RemoteFile, dest: Path) -> FetchReport:
        """Download and verify ``remote`` into ``dest``; returns digests and transfer statistics."""
        t0 = time.monotonic()
        self.check_cancelled()
        target = resolve_target(remote, lambda u, n: self.get_bytes(u, max_bytes=n), self.config)
        return self.fetch_target(target, dest, t0=t0)

    @contextmanager
    def destination_lock(self, dest: Path) -> Generator[None]:
        """Hold the local lock of ``dest``; :class:`FetchLockedError` if another fetch holds it.

        Every process must use the same directory (``config.lock_dir``; the CLI sets the machine's
        lock directory, audit 2026-10-12 M4). The lock file is removed when the fetch ends, so the
        directory does not grow by one file per download: a holder unlinks its file while still
        holding the lock, and a taker that locked a file unlinked meanwhile (its inode no longer at
        the path) closes it and tries again.
        """
        directory = (
            Path(self.config.lock_dir)
            if self.config.lock_dir
            else Path(tempfile.gettempdir()) / f"whisipedia-fetch-locks-{os.getuid()}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(str(dest.resolve()).encode()).hexdigest()[:32]
        path = directory / f"{name}.lock"
        while True:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise FetchLockedError(f"{dest} is already being downloaded by another fetch") from None
            try:
                current = os.stat(path)
            except FileNotFoundError:
                current = None
            if current is not None and current.st_ino == os.fstat(fd).st_ino:
                break
            os.close(fd)  # the previous holder removed this file after we opened it: take a fresh one
        try:
            yield
        finally:
            with suppress(OSError):
                os.unlink(path)
            os.close(fd)

    def fetch_target(self, target: Target, dest: Path, *, t0: float | None = None) -> FetchReport:
        """:meth:`fetch` for an already resolved target (used by :class:`~wsp_fetch.aria2.Aria2Fetcher`)."""
        t0 = time.monotonic() if t0 is None else t0
        allowed = tuple(u for u in target.urls if self.robots_allows(u))
        if not allowed:
            raise RobotsDisallowedError(f"{target.url}: every source's robots.txt disallows us")
        if len(allowed) < len(target.urls):
            log.info("%s: %d mirror(s) dropped by robots.txt", target.url, len(target.urls) - len(allowed))
            target = dataclasses.replace(target, urls=allowed)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.destination_lock(dest):
            upstream_verified = bool(target.checksums) or target.pieces is not None
            if not upstream_verified and self.config.unverified_policy == "refuse":
                raise UnverifiableError(
                    f"{target.url}: upstream publishes no checksum and acquirer.unverified_policy = 'refuse'"
                )
            if dest.exists() and target.checksums:
                existing = _already_complete(dest, target)
                if existing is not None:
                    return existing
            report = self._download(target, dest, t0, single_source=not upstream_verified)
            if upstream_verified:
                return report
            if self.config.unverified_policy == "allow":
                return dataclasses.replace(report, integrity="unverified")
            return self._corroborate(target, dest, report)

    def _download(self, target: Target, dest: Path, t0: float, *, single_source: bool) -> FetchReport:
        if target.urls[0].startswith("ftp://"):
            return self._ftp_download(target, dest, t0)
        probes, rejected = self._probe_all(target)
        ranged = [p for p in probes if p.ranges and p.size is not None]
        if single_source and len(ranged) > 1:  # never splice unverifiable bytes from several mirrors
            primary = next((p for p in ranged if p.url == target.urls[0]), ranged[0])
            rejected += [
                {"url": p.url, "reason": "not used: no upstream digest to check spliced pieces"}
                for p in ranged
                if p is not primary
            ]
            ranged = [primary]
        if not ranged:
            return self._single_stream(target, probes, dest, t0, rejected)
        size = ranged[0].size
        assert size is not None
        validator = next((p.validator for p in probes if p.url == target.urls[0]), None)
        mirrors = [
            _Mirror(i, p.url, p.final_url, self.config.connections_for(p.url), observation=p.observation)
            for i, p in enumerate(ranged)
        ]
        rejected += [
            {"url": p.url, "reason": "no Range support", "observation": p.observation}
            for p in probes
            if p not in ranged
        ]
        for m in mirrors:
            m.allowed = m.workers
        if target.pieces is not None:
            piece_size, algo = target.pieces.length, target.pieces.algorithm
        else:
            piece_size, algo = choose_piece_size(size, self.config), "blake3"
        plan = _Plan(target, dest, size, piece_size, algo, mirrors, validator, rejected)
        return _SegmentedDownload(self, plan, t0).run()

    def _probe_all(self, target: Target) -> tuple[list[_Probe], list[dict[str, Any]]]:
        """Usable probes, and the mirrors rejected with the reason (kept for provenance)."""
        results: list[_Probe | None] = [None] * len(target.urls)
        errors: list[str] = []
        rejected: list[dict[str, Any]] = []
        # A lone source gets the full retry budget; with alternatives, don't stall on a dead mirror.
        attempts = self.config.max_attempts if len(target.urls) == 1 else min(2, self.config.max_attempts)

        def run(i: int, url: str) -> None:
            try:
                results[i] = self._probe(url, attempts)
            except (FetchError, httpx.HTTPError) as exc:
                errors.append(f"{url}: {exc}")
                rejected.append({"url": url, "reason": f"unreachable: {exc}"})
                log.info("mirror %s unusable: %s", url, exc)

        threads = [
            threading.Thread(target=contextvars.copy_context().run, args=(run, i, u), daemon=True)
            for i, u in enumerate(target.urls)
        ]
        started: list[threading.Thread] = []
        for i, t in enumerate(threads):
            try:
                t.start()
            except RuntimeError:  # "can't start new thread": the machine is short for a moment
                # Probe the rest here, one by one, rather than end the fetch: NPS Calhoun's nine-hour
                # listing of 17,757 documents ended so on 2026-10-19.
                for j in range(i, len(threads)):
                    run(j, target.urls[j])
                break
            started.append(t)
        for t in started:
            t.join()
        probes = [p for p in results if p is not None]
        expected = target.size
        if expected is None:
            sizes = [p.size for p in probes if p.size is not None]
            expected = max(set(sizes), key=sizes.count) if sizes else None
        good: list[_Probe] = []
        for p in probes:
            if expected is not None and p.size is not None and p.size != expected:
                log.warning("mirror %s advertises %d bytes, expected %d; skipping", p.url, p.size, expected)
                errors.append(f"{p.url}: size {p.size} != {expected}")
                rejected.append(
                    {
                        "url": p.url,
                        "reason": "size-disagreement",
                        "size": p.size,
                        "expected": expected,
                        "observation": p.observation,
                    }
                )
                continue
            if p.size is None:
                p.size = expected
            good.append(p)
        self.check_cancelled()
        if not good:
            raise MirrorsExhaustedError(f"no usable source for {target.url}: {'; '.join(errors)}")
        return good, rejected

    def _probe(self, url: str, attempts: int) -> _Probe:
        return self._retrying(f"probe {url}", lambda: self._probe_once(url), attempts)

    def _probe_once(self, url: str) -> _Probe:
        try:
            with (
                self.host_slot(url),
                self._client.stream("GET", url, headers={"Range": "bytes=0-0"}) as r,
            ):
                seen = self._observe(url, r)
                validator = r.headers.get("ETag") or r.headers.get("Last-Modified")
                final = str(r.url)
                if r.status_code == 206:
                    m = _CONTENT_RANGE.match(r.headers.get("Content-Range", ""))
                    size = int(m.group(3)) if m and m.group(3) != "*" else None
                    return _Probe(url, final, size, size is not None, validator, seen)
                if r.status_code == 416:
                    m = _CONTENT_RANGE_EMPTY.match(r.headers.get("Content-Range", ""))
                    if m:
                        return _Probe(url, final, int(m.group(1)), True, validator, seen)
                if r.status_code == 200:
                    cl = r.headers.get("Content-Length")
                    size = int(cl) if cl and cl.isdigit() else None
                    return _Probe(url, final, size, False, validator, seen)
                _raise_for_status(r)
                raise _RetryableError(f"unexpected status {r.status_code}", fatal_for_mirror=True)
        except httpx.TransportError as exc:
            raise _RetryableError(f"{type(exc).__name__}: {exc}") from exc

    def _single_stream(
        self, target: Target, probes: list[_Probe], dest: Path, t0: float, rejected: list[dict[str, Any]]
    ) -> FetchReport:
        """Fallback when no source supports Range: sequential download with inline hashing.

        Each source gets the full retry budget (a lone source is retried, not given up after one
        dropped connection). The request goes to the URL we were given, so an expired signed
        redirect is resolved afresh on every attempt. The body must have the size the probe saw:
        a close-delimited response that ends early is a failed attempt, not a smaller file.
        """
        partial = partial_path(dest)
        errors: list[str] = []
        for p in probes:
            expected = target.size if target.size is not None else p.size
            for attempt in range(1, self.config.max_attempts + 1):
                try:
                    got, digests = self._stream_once(target, p.url, partial, expected)
                except FetchCancelledError:
                    raise
                except (httpx.TransportError, _RetryableError, OSError) as exc:
                    errors.append(f"{p.url} (attempt {attempt}): {exc}")
                    if getattr(exc, "fatal_for_mirror", False) or attempt == self.config.max_attempts:
                        break
                    delay = backoff_delay(attempt, self.config.backoff_base, self.config.backoff_max)
                    if self._cancel.wait(delay):
                        raise FetchCancelledError(f"{target.url}: cancelled (shutdown)") from exc
                    continue
                return self._single_report(target, probes, p, dest, partial, got, digests, t0, rejected)
        partial.unlink(missing_ok=True)
        raise MirrorsExhaustedError(f"all sources failed for {target.url}: {'; '.join(errors)}")

    def _stream_once(
        self, target: Target, url: str, partial: Path, expected: int | None
    ) -> tuple[int, dict[str, str]]:
        hasher = MultiHasher(c.algorithm for c in target.checksums)
        got = 0
        try:
            with (
                self.host_slot(url),
                self._client.stream("GET", url) as r,
                partial.open("wb") as fh,
            ):
                _raise_for_status(r)
                for chunk in r.iter_raw():
                    if self._cancel.is_set():
                        raise FetchCancelledError(f"{target.url}: cancelled (shutdown)")
                    fh.write(chunk)
                    hasher.update(chunk)
                    self.throttle(len(chunk))
                    self.wrote(len(chunk))
                    got += len(chunk)
                fh.flush()
                os.fsync(fh.fileno())
            if expected is not None and got != expected:
                raise _RetryableError(f"body ended after {got} of {expected} bytes")
            return got, hasher.hexdigests()
        finally:
            hasher.close()

    def _single_report(
        self,
        target: Target,
        probes: list[_Probe],
        p: _Probe,
        dest: Path,
        partial: Path,
        got: int,
        digests: dict[str, str],
        t0: float,
        rejected: list[dict[str, Any]],
    ) -> FetchReport:
        finalize(
            target.url,
            partial,
            dest,
            expected_size=target.size,
            checksums=target.checksums,
            digests=digests,
            readback=self.config.verify_readback,
        )
        state_path(dest).unlink(missing_ok=True)
        sources = [
            _source_json(
                q.url,
                q.observation,
                ranges=[[0, got - 1]] if q is p and got else [],
                nbytes=got if q is p else 0,
            )
            for q in probes
        ]
        return make_report(
            dest,
            got,
            digests,
            target.checksums,
            got,
            0,
            time.monotonic() - t0,
            {p.url: got},
            provenance=_file_provenance(target, got, sources, rejected, resumed=0),
        )

    # --- FTP (D16) ----------------------------------------------------------------------------

    def _ftp_download(self, target: Target, dest: Path, t0: float) -> FetchReport:
        """One stream per attempt from the target's ``ftp://`` sources in order, resumed with ``REST``
        from the partial file (whose bytes are hashed again first), checked like an HTTP single
        stream (size, then every upstream digest in :func:`finalize`)."""
        urls = [u for u in target.urls if u.startswith("ftp://")]
        partial = partial_path(dest)
        errors: list[str] = []
        missing = 0
        for url in urls:
            for attempt in range(1, self.config.max_attempts + 1):
                try:
                    got, digests = self._ftp_once(target, url, partial)
                except FetchCancelledError:
                    raise
                except ftp.FtpNotFoundError as exc:
                    errors.append(f"{url}: {exc}")
                    missing += 1
                    break
                except (*ftp.TRANSIENT, _RetryableError) as exc:
                    errors.append(f"{url} (attempt {attempt}): {exc}")
                    if attempt == self.config.max_attempts:
                        break
                    delay = backoff_delay(attempt, self.config.backoff_base, self.config.backoff_max)
                    if self._cancel.wait(delay):
                        raise FetchCancelledError(f"{target.url}: cancelled (shutdown)") from exc
                    continue
                probe = _Probe(url, url, got, False, None)
                return self._single_report(target, [probe], probe, dest, partial, got, digests, t0, [])
        if missing == len(urls):
            partial.unlink(missing_ok=True)
            raise NotFoundError(target.url, "; ".join(errors))
        raise MirrorsExhaustedError(f"all sources failed for {target.url}: {'; '.join(errors)}")

    def _ftp_once(self, target: Target, url: str, partial: Path) -> tuple[int, dict[str, str]]:
        expected = target.size if target.size is not None else ftp.size(url, timeout=self.config.read_timeout)
        hasher = MultiHasher(c.algorithm for c in target.checksums)
        try:
            have = partial.stat().st_size if partial.exists() else 0
            if expected is not None and have > expected:
                partial.unlink()
                have = 0
            if have:
                with partial.open("rb") as fh:
                    while chunk := fh.read(1 << 20):
                        hasher.update(chunk)
            got = have
            with self.host_slot(url), partial.open("ab") as fh:

                def sink(chunk: bytes) -> None:
                    nonlocal got
                    if self._cancel.is_set():
                        raise FetchCancelledError(f"{target.url}: cancelled (shutdown)")
                    fh.write(chunk)
                    hasher.update(chunk)
                    self.throttle(len(chunk))
                    self.wrote(len(chunk))
                    got += len(chunk)

                ftp.retrieve(url, offset=have, timeout=self.config.read_timeout, sink=sink)
                fh.flush()
                os.fsync(fh.fileno())
            if expected is not None and got != expected:
                raise _RetryableError(f"transfer ended after {got} of {expected} bytes")
            return got, hasher.hexdigests()
        finally:
            hasher.close()

    # --- corroboration (sources without upstream digests) -------------------------------------

    def _corroborate(self, target: Target, dest: Path, report: FetchReport) -> FetchReport:
        """Download the file a second time, in full, and require identical size and digests.

        The second download prefers a source that did not serve the first; with a single source it
        repeats that source (which still catches truncation and transport corruption, not a
        malicious origin). Disagreement keeps the first copy as ``<name>.disputed`` and raises
        :class:`ChecksumMismatchError`; failing to make a second download keeps it as
        ``<name>.uncorroborated`` and raises :class:`UnverifiableError`.
        """
        served = max(report.per_mirror_bytes.items(), key=lambda kv: kv[1], default=(target.url, 0))[0]
        order = [u for u in target.urls if u != served] + [served]
        errors: list[str] = []
        for url in order:
            try:
                size, digests = self._retrying(
                    f"corroborate {url}", lambda u=url: self._hash_remote(u), self.config.max_attempts
                )
            except FetchCancelledError:
                raise
            except (FetchError, httpx.HTTPError) as exc:
                errors.append(f"{url}: {exc}")
                continue
            if (
                size != report.size
                or digests["sha256"] != report.sha256
                or digests["blake3"] != report.blake3
            ):
                disputed = dest.with_name(dest.name + ".disputed")
                os.replace(dest, disputed)
                raise ChecksumMismatchError(
                    target.url,
                    "sha256 (independent second download)",
                    report.sha256,
                    f"{digests['sha256']} ({size} bytes from {url}); first copy kept as {disputed.name}",
                )
            provenance = {
                **report.provenance,
                "corroboration": {"url": url, "independent_source": url != served, "bytes": size},
            }
            return dataclasses.replace(
                report, corroborated=True, integrity="corroborated", provenance=provenance
            )
        kept = dest.with_name(dest.name + ".uncorroborated")
        os.replace(dest, kept)
        raise UnverifiableError(
            f"{target.url}: upstream publishes no checksum and no second download succeeded "
            f"({'; '.join(errors)}); first copy kept as {kept.name}"
        )

    def _hash_remote(self, url: str) -> tuple[int, dict[str, str]]:
        """Stream ``url`` completely, hashing only (nothing written); size and digests."""
        hasher = MultiHasher(())
        got = 0
        try:
            with self.host_slot(url):
                try:
                    with self._client.stream("GET", url) as r:
                        _raise_for_status(r)
                        declared = r.headers.get("Content-Length")
                        for chunk in r.iter_raw():
                            self.check_cancelled()
                            hasher.update(chunk)
                            self.throttle(len(chunk))
                            got += len(chunk)
                except httpx.TransportError as exc:
                    raise _RetryableError(f"{type(exc).__name__}: {exc}") from exc
            if declared is not None and declared.isdigit() and int(declared) != got:
                raise _RetryableError(f"body ended after {got} of {declared} bytes")
            return got, hasher.hexdigests()
        finally:
            hasher.close()


def _already_complete(dest: Path, target: Target) -> FetchReport | None:
    if target.size is not None and dest.stat().st_size != target.size:
        return None
    digests = hash_file(dest, [c.algorithm for c in target.checksums])
    if all(digests.get(c.algorithm) == c.hexdigest for c in target.checksums):
        size = dest.stat().st_size
        provenance = _file_provenance(target, size, [], [], resumed=size)
        provenance["reused_local_file"] = True
        return make_report(dest, size, digests, target.checksums, provenance=provenance)
    return None


def _source_json(
    url: str, observation: dict[str, Any], *, ranges: list[list[int]], nbytes: int, **extra: Any
) -> dict[str, Any]:
    return {
        "url": url,
        "final_url": observation.get("final_url", url),
        "host": observation.get("host") or prov.host_of(url),
        "redirects": observation.get("redirects", []),
        "tls": observation.get("tls"),
        "etag": observation.get("etag"),
        "last_modified": observation.get("last_modified"),
        "ranges": ranges,
        "bytes": nbytes,
        **extra,
    }


def _file_provenance(
    target: Target, size: int, sources: list[dict[str, Any]], rejected: list[dict[str, Any]], *, resumed: int
) -> dict[str, Any]:
    """The per-file provenance record carried by :class:`FetchReport` (``wsp_fetch.provenance``)."""
    ml = target.metalink
    return {
        "format": prov.FORMAT,
        "url": target.url,
        "size": size,
        "metalink": target.metalink_url,
        "checksums": {
            "declared": prov.checksum_list(target.checksums),
            "metalink": prov.checksum_list(ml.checksums) if ml is not None else [],
            "piece_hashes": ml.pieces.algorithm if ml is not None and ml.pieces is not None else None,
        },
        "sources": sources,
        "rejected": [dict(r) for r in rejected],
        "resumed_bytes": resumed,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _parse_robots(raw: bytes | None) -> RobotFileParser | None:
    if raw is None:
        return None
    body = raw[: 512 * 1024].decode("utf-8", "replace").lstrip("\ufeff")
    if body.lstrip()[:20].lower().startswith(("<!doctype", "<html")):
        return None  # a site that answers every path with a page has no robots.txt
    rules = RobotFileParser()
    rules.parse(body.splitlines())
    return rules


def _raise_for_status(r: httpx.Response) -> None:
    if r.status_code < 400:
        return
    retry_after = parse_retry_after(r.headers.get("Retry-After"))
    if r.status_code in _RETRY_STATUSES:
        raise _RetryableError(f"HTTP {r.status_code}", retry_after)
    raise _RetryableError(f"HTTP {r.status_code}", retry_after, fatal_for_mirror=True, status=r.status_code)


# --- segmented engine ---------------------------------------------------------------------------


class _SegmentedDownload:
    def __init__(self, fetcher: HttpFetcher, plan: _Plan, t0: float) -> None:
        self.f = fetcher
        self.cfg = fetcher.config
        self.plan = plan
        self.t0 = t0
        self.size = plan.size
        self.piece_size = plan.piece_size
        self.n = -(-plan.size // plan.piece_size)
        self.partial = partial_path(plan.dest)
        self.state_file = state_path(plan.dest)
        self.cond = threading.Condition()
        self.pieces = [_Piece(i) for i in range(self.n)]
        self.pending: list[int] = []
        self.claimed: set[int] = set()
        self.done: set[int] = set()
        self.error: BaseException | None = None
        self.finished = False
        self.live_workers = 0
        self.bytes_downloaded = 0
        self.bytes_resumed = 0
        self._ckpt_lock = threading.Lock()
        self._last_ckpt = time.monotonic()
        target = plan.target
        self.state = PartialState(
            url=target.url,
            size=plan.size,
            piece_size=plan.piece_size,
            piece_algorithm=plan.piece_algorithm,
            checksums={c.algorithm: c.hexdigest for c in target.checksums},
            validator=plan.validator,
        )
        self.fd = -1

    # geometry
    def span(self, i: int) -> tuple[int, int]:
        start = i * self.piece_size
        return start, min(self.piece_size, self.size - start)

    def piece_digest(self, data: bytes | bytearray | memoryview) -> str:
        h = new_hasher(self.plan.piece_algorithm)
        h.update(data)
        return h.hexdigest()

    def piece_ok(self, i: int, digest: str) -> bool:
        pieces = self.plan.target.pieces
        return pieces is None or pieces.hashes[i] == digest

    # lifecycle
    def run(self) -> FetchReport:
        resumed = self._open_partial()
        hasher = MultiHasher(c.algorithm for c in self.plan.target.checksums)
        self.inorder = InOrderHasher(
            hasher,
            self.fd,
            self.span,
            self.n,
            window_bytes=self.cfg.hash_window_bytes,
            verify=self._verify_resumed,
            on_bad=self._requeue_resumed,
        )
        for i in range(self.n):
            if i in resumed:
                self.done.add(i)
                self.claimed.add(i)
                self.bytes_resumed += self.span(i)[1]
                self.inorder.offer(i, None, verified=False)
            else:
                self.pending.append(i)
        self.inorder.start()
        threads: list[threading.Thread] = []
        for m in self.plan.mirrors:
            for slot in range(m.workers):
                threads.append(
                    threading.Thread(
                        target=contextvars.copy_context().run,  # the caller's redirect guard
                        args=(self._worker, m, slot),
                        name=f"wsp-fetch-{m.id}.{slot}",
                        daemon=True,
                    )
                )
        self.live_workers = len(threads)
        for t in threads:
            t.start()
        try:
            while not self.inorder.done.wait(0.1):
                with self.cond:
                    if self.f.cancelled and self.error is None:
                        self.error = FetchCancelledError(f"{self.plan.target.url}: cancelled (shutdown)")
                    if self.error is not None:
                        break
                    if self.live_workers == 0:
                        self.error = MirrorsExhaustedError(
                            f"all mirrors failed for {self.plan.target.url} with {self.n - len(self.done)} "
                            "pieces outstanding"
                        )
                        break
            if self.error is None and self.inorder.error is not None:
                self.error = self.inorder.error
        except BaseException as exc:  # KeyboardInterrupt: keep resume state consistent
            self.error = exc
        finally:
            with self.cond:
                self.finished = True
                self.cond.notify_all()
            self.inorder.abort()
            for t in threads:  # workers notice ``finished`` between reads; a cancel need not wait long
                t.join(timeout=2.0 if self.f.cancelled else self.cfg.read_timeout + 5)
        if self.error is not None:
            hasher.close()
            self._checkpoint(force=True)
            os.close(self.fd)
            raise self.error
        os.fsync(self.fd)
        try:
            self._spot_check()  # before publishing, from the fsynced partial, at random offsets
        finally:
            os.close(self.fd)
        digests = hasher.hexdigests()
        try:
            finalize(
                self.plan.target.url,
                self.partial,
                self.plan.dest,
                expected_size=self.size,
                checksums=self.plan.target.checksums,
                digests=digests,
                readback=self.cfg.verify_readback,
            )
        finally:
            if not self.partial.exists():
                self.state_file.unlink(missing_ok=True)
        sources = [
            _source_json(
                m.url,
                m.observation,
                ranges=prov.byte_ranges(m.pieces, self.piece_size, self.size),
                nbytes=m.bytes,
                corrupt_pieces=m.corrupt,
                disabled=m.disabled,
                spot_check=m.spot_check,
            )
            for m in self.plan.mirrors
        ]
        return make_report(
            self.plan.dest,
            self.size,
            digests,
            self.plan.target.checksums,
            self.bytes_downloaded,
            self.bytes_resumed,
            time.monotonic() - self.t0,
            {m.url: m.bytes for m in self.plan.mirrors},
            provenance=_file_provenance(
                self.plan.target, self.size, sources, self.plan.rejected, resumed=self.bytes_resumed
            ),
        )

    def _spot_check(self) -> None:
        """Re-read random ranges from every mirror and compare them with the verified file.

        The file already matched every upstream digest; a mirror that serves *other* bytes for the
        same name is a disagreement the integrity guard must see (ADR-0009). Errors are not
        disagreements (a mirror may be busy); they are recorded as such.
        """
        k, n = self.cfg.spot_checks, min(self.cfg.spot_check_bytes, self.size)
        if k <= 0 or n <= 0 or len(self.plan.mirrors) < 2:
            return
        rng = _spot_rng()
        offsets = sorted({rng.randrange(0, self.size - n + 1) for _ in range(k)})
        expected = {off: pread_exact(self.fd, n, off) for off in offsets}
        for m in self.plan.mirrors:
            if m.disabled:  # already known bad (corrupt pieces, fatal errors): see corrupt_pieces
                m.spot_check = "skipped: mirror dropped"
                continue
            verdict = "ok"
            for off in offsets:
                if self.f.cancelled:
                    return
                try:
                    got = self._read_range(m, off, off + n - 1)
                except (_RetryableError, httpx.HTTPError, FetchError) as exc:
                    verdict = f"error: {exc}"
                    continue
                if got != expected[off]:
                    log.warning("mirror %s serves other bytes at %d of %s", m.url, off, self.plan.dest.name)
                    verdict = "mismatch"
                    break
            m.spot_check = verdict

    def _read_range(self, m: _Mirror, start: int, end: int) -> bytes:
        with (
            self.f.host_slot(m.url),
            self.f.client.stream("GET", m.final_url, headers={"Range": f"bytes={start}-{end}"}) as r,
        ):
            _raise_for_status(r)
            if r.status_code != 206:
                raise _RetryableError(f"spot check: HTTP {r.status_code} without a range")
            out = bytearray()
            for chunk in r.iter_raw():
                out += chunk
                self.f.throttle(len(chunk))
                if len(out) > end - start + 1:
                    break
            return bytes(out)

    def _open_partial(self) -> set[int]:
        """Open ``.partial``; returns pieces a previous run recorded (still to be re-verified)."""
        old = load_state(self.state_file)
        hashed_pieces = self.plan.target.pieces is not None  # resumed pieces are checked upstream
        resumable = (
            old is not None
            and old.same_target(self.state, require_validator=not hashed_pieces)
            and self.partial.exists()
            and self.partial.stat().st_size == self.size
        )
        self.fd = os.open(self.partial, os.O_RDWR | os.O_CREAT, 0o644)
        if resumable and old is not None:
            self.state.done = {i: d for i, d in old.done.items() if 0 <= i < self.n}
            log.info("resuming %s: %d/%d pieces on disk", self.plan.dest.name, len(self.state.done), self.n)
            return set(self.state.done)
        os.ftruncate(self.fd, 0)
        os.ftruncate(self.fd, self.size)  # sparse; pieces land at their offsets
        save_state(self.state_file, self.state)
        fsync_dir(self.partial.parent)
        return set()

    def _verify_resumed(self, i: int, data: bytes) -> bool:
        digest = self.piece_digest(data)
        with self.cond:
            recorded = self.state.done.get(i)
        return digest == recorded and self.piece_ok(i, digest)

    def _requeue_resumed(self, i: int) -> None:
        log.warning("resumed piece %d of %s failed verification; re-downloading", i, self.plan.dest.name)
        with self.cond:
            self.state.done.pop(i, None)
            self.done.discard(i)
            self.claimed.discard(i)
            self.bytes_resumed -= self.span(i)[1]
            bisect.insort(self.pending, i)
            self.cond.notify_all()

    # workers
    def _worker(self, m: _Mirror, slot: int) -> None:
        try:
            while True:
                i = self._next_piece(m, slot)
                if i is None:
                    return
                url = m.final_url
                try:
                    data, seconds = self._download_piece(m, i, url)
                except _CancelledError:
                    self._release(m, i)
                    continue
                except (_RetryableError, httpx.HTTPError, SizeMismatchError) as exc:
                    if (
                        isinstance(exc, _RetryableError)
                        and exc.status in _EXPIRED
                        and m.url != url
                        and self._reresolve(m, url)
                    ):
                        self._requeue(m, i)
                        continue
                    delay = self._failed(m, i, exc)
                    if delay and self.f.wait_cancelled(delay):
                        return
                    continue
                self._completed(m, i, data, seconds)
        except BaseException as exc:
            with self.cond:
                self.error = self.error or exc
                self.cond.notify_all()
        finally:
            with self.cond:
                self.live_workers -= 1
                self.cond.notify_all()

    def _next_piece(self, m: _Mirror, slot: int) -> int | None:
        with self.cond:
            while True:
                if self.finished or self.error is not None or m.disabled or slot >= m.allowed:
                    return None
                now = time.monotonic()
                if now < m.cooldown_until:
                    self.cond.wait(m.cooldown_until - now)
                    continue
                for pos, i in enumerate(self.pending):
                    if m.id not in self.pieces[i].excluded:
                        del self.pending[pos]
                        self.pieces[i].active[m.id] = now
                        return i
                dup = self._endgame_candidate(m, now)
                if dup is not None:
                    self.pieces[dup].active[m.id] = now
                    log.debug("endgame: mirror %s duplicates piece %d", m.url, dup)
                    return dup
                self.cond.wait(0.25)

    def _endgame_candidate(self, m: _Mirror, now: float) -> int | None:
        if not self.cfg.endgame or self.pending:
            return None
        best: int | None = None
        for p in self.pieces:
            if (
                p.index not in self.claimed
                and len(p.active) == 1
                and m.id not in p.active
                and m.id not in p.excluded
                and now - next(iter(p.active.values())) > 0.5
            ):
                best = p.index
                break
        return best

    def _download_piece(self, m: _Mirror, i: int, url: str) -> tuple[bytearray, float]:
        start, length = self.span(i)
        end = start + length - 1
        buf = bytearray(length)
        view = memoryview(buf)
        pos = 0
        with self.f.host_slot(m.url):  # politeness: the host we were asked to use, not a redirect's
            t = time.monotonic()
            with self.f.client.stream("GET", url, headers={"Range": f"bytes={start}-{end}"}) as r:
                if r.status_code == 200:
                    raise _RetryableError("server ignored Range", fatal_for_mirror=True)
                _raise_for_status(r)
                cr = _CONTENT_RANGE.match(r.headers.get("Content-Range", ""))
                if r.status_code != 206 or cr is None:
                    raise _RetryableError(f"bad partial response {r.status_code}", fatal_for_mirror=True)
                if (int(cr.group(1)), int(cr.group(2))) != (start, end) or cr.group(3) not in (
                    str(self.size),
                    "*",
                ):
                    raise _RetryableError(
                        f"Content-Range {cr.group(0)} != {start}-{end}/{self.size}", fatal_for_mirror=True
                    )
                for chunk in r.iter_raw():
                    if i in self.claimed or self.finished or self.f.cancelled:
                        raise _CancelledError
                    n = len(chunk)
                    if pos + n > length:
                        raise _RetryableError("server sent more bytes than requested")
                    view[pos : pos + n] = chunk
                    pos += n
                    m.bytes += n
                    self.f.throttle(n)
            seconds = time.monotonic() - t
        if pos != length:
            raise _RetryableError(f"short read: {pos}/{length} bytes")
        return buf, seconds

    def _release(self, m: _Mirror, i: int) -> None:
        with self.cond:
            self.pieces[i].active.pop(m.id, None)
            self.cond.notify_all()

    def _requeue(self, m: _Mirror, i: int) -> None:
        """Give a piece back without counting a failure (its URL expired and was re-resolved)."""
        with self.cond:
            piece = self.pieces[i]
            piece.active.pop(m.id, None)
            if i not in self.claimed and not piece.active and i not in self.pending:
                bisect.insort(self.pending, i)
            self.cond.notify_all()

    def _reresolve(self, m: _Mirror, failed_url: str) -> bool:
        """Probe ``m.url`` again for a fresh redirect target after ``failed_url`` expired (audit #15).

        True when ``m.final_url`` is usable again (another worker may already have refreshed it).
        Limited to ``max_reresolve`` per mirror; the new target must still have the same size.
        """
        with self.cond:
            if m.final_url != failed_url:
                return True
            if m.reresolved >= self.cfg.max_reresolve or m.disabled:
                return False
            m.reresolved += 1
        try:
            probe = self.f._probe(m.url, 2)  # pyright: ignore[reportPrivateUsage]
        except (FetchError, httpx.HTTPError) as exc:
            log.info("re-resolving %s failed: %s", m.url, exc)
            return False
        if probe.size is not None and probe.size != self.size:
            log.warning("mirror %s now advertises %s bytes, not %d", m.url, probe.size, self.size)
            return False
        with self.cond:
            m.final_url = probe.final_url
            log.info("mirror %s: expired redirect re-resolved to %s", m.url, probe.final_url)
        return True

    def _failed(self, m: _Mirror, i: int, exc: BaseException, *, corrupt: bool = False) -> float:
        """Requeue a failed piece; returns how long this worker should back off."""
        retry_after = getattr(exc, "retry_after", None)
        fatal = bool(getattr(exc, "fatal_for_mirror", False))
        with self.cond:
            piece = self.pieces[i]
            piece.active.pop(m.id, None)
            m.consecutive_failures += 1
            if corrupt:
                piece.excluded.add(m.id)
                m.corrupt += 1
            others = any(not x.disabled and x is not m for x in self.plan.mirrors)
            if (
                fatal
                or m.corrupt >= 2
                or (others and m.consecutive_failures >= self.cfg.mirror_failure_limit)
            ):
                m.disabled = True
            if retry_after is not None and retry_after > self.cfg.max_retry_after:
                m.disabled = True
            log.info(
                "mirror %s piece %d failed (%s)%s", m.url, i, exc, "; mirror dropped" if m.disabled else ""
            )
            if i not in self.claimed:
                piece.attempts += 1
                live = [x for x in self.plan.mirrors if not x.disabled and x.id not in piece.excluded]
                if piece.attempts >= self.cfg.max_attempts * max(1, len(self.plan.mirrors)) or not live:
                    self.error = MirrorsExhaustedError(
                        f"piece {i} of {self.plan.target.url} failed on every mirror (last: {exc})"
                    )
                elif not piece.active and i not in self.pending:
                    bisect.insort(self.pending, i)
            self.cond.notify_all()
            if m.disabled:
                return 0.0
            delay = backoff_delay(m.consecutive_failures, self.cfg.backoff_base, self.cfg.backoff_max)
            if retry_after is not None:
                delay = max(delay, retry_after)
                m.cooldown_until = time.monotonic() + delay
            return delay

    def _completed(self, m: _Mirror, i: int, data: bytearray, seconds: float) -> None:
        digest = self.piece_digest(data)
        if not self.piece_ok(i, digest):
            log.warning("piece %d from %s failed its %s check", i, m.url, self.plan.piece_algorithm)
            self._failed(m, i, FetchError("piece hash mismatch"), corrupt=True)
            return
        with self.cond:
            self.pieces[i].active.pop(m.id, None)
            if i in self.claimed:  # an endgame duplicate won the race
                self.cond.notify_all()
                return
            self.claimed.add(i)
        offset, _ = self.span(i)
        written = os.pwrite(self.fd, data, offset)
        if written != len(data):
            raise OSError(f"short write at {offset}")
        self.f.wrote(written)
        with self.cond:
            self.done.add(i)
            m.pieces.append(i)
            self.state.done[i] = digest
            self.bytes_downloaded += len(data)
            m.consecutive_failures = 0
            m.record(len(data), seconds)
            self._maybe_demote()
            self.cond.notify_all()
        self.inorder.offer(i, data)
        self._checkpoint()

    def _maybe_demote(self) -> None:
        active = [x for x in self.plan.mirrors if not x.disabled and x.samples >= 2]
        if len(active) < 2:
            return
        best = max(x.rate for x in active)
        for x in active:
            if x.allowed > 1 and x.rate * self.cfg.slow_mirror_factor < best:
                log.info("demoting slow mirror %s (%.0f vs %.0f B/s per connection)", x.url, x.rate, best)
                x.allowed = 1

    def _checkpoint(self, *, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_ckpt < self.cfg.state_interval:
            return
        if not self._ckpt_lock.acquire(blocking=force):
            return
        try:
            with self.cond:
                snapshot = dict(self.state.done)
            os.fdatasync(self.fd)  # every piece in the snapshot was pwritten before this sync
            save_state(self.state_file, dataclasses.replace(self.state, done=snapshot))
            self._last_ckpt = time.monotonic()
        except OSError as exc:
            log.warning("checkpoint failed for %s: %s", self.plan.dest.name, exc)
        finally:
            self._ckpt_lock.release()
