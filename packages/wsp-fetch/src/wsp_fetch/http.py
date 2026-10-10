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
"""

from __future__ import annotations

import bisect
import dataclasses
import email.utils
import logging
import os
import random
import re
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from wsp_core.model import RemoteFile
from wsp_fetch.config import FetchConfig, host_key
from wsp_fetch.errors import (
    FetchError,
    MirrorsExhaustedError,
    ResponseTooLargeError,
    SizeMismatchError,
)
from wsp_fetch.finish import FetchResult, finalize, fsync_dir
from wsp_fetch.hashing import InOrderHasher, MultiHasher, hash_file, new_hasher
from wsp_fetch.ratelimit import TokenBucket
from wsp_fetch.state import PartialState, load_state, partial_path, save_state, state_path
from wsp_fetch.target import Target, resolve_target

log = logging.getLogger(__name__)

_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")
_CONTENT_RANGE_EMPTY = re.compile(r"bytes\s+\*/(\d+)")
_RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}


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
    def __init__(self, msg: str, retry_after: float | None = None, *, fatal_for_mirror: bool = False) -> None:
        super().__init__(msg)
        self.retry_after = retry_after
        self.fatal_for_mirror = fatal_for_mirror


class _CancelledError(Exception):
    pass


@dataclass(slots=True)
class _Probe:
    url: str
    final_url: str
    size: int | None
    ranges: bool
    validator: str | None


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


@dataclass(slots=True)
class _Piece:
    index: int
    attempts: int = 0
    excluded: set[int] = field(default_factory=set[int])
    active: dict[int, float] = field(default_factory=dict[int, float])  # mirror id -> start time


# --- fetcher ------------------------------------------------------------------------------------


class HttpFetcher:
    """Reference ``Fetcher`` (PLAN §6.5). Thread-safe; host caps apply across concurrent downloads."""

    def __init__(self, config: FetchConfig | None = None, *, transport: httpx.BaseTransport | None = None):
        self.config = config or FetchConfig()
        c = self.config
        self._client = httpx.Client(
            headers={"User-Agent": c.user_agent, "Accept-Encoding": "identity"},
            timeout=httpx.Timeout(c.read_timeout, connect=c.connect_timeout),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=64),
            transport=transport,
        )
        self._bucket = TokenBucket(c.bandwidth_limit) if c.bandwidth_limit else None
        self._hosts: dict[str, threading.BoundedSemaphore] = {}
        self._hosts_lock = threading.Lock()

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

    # --- small documents ---------------------------------------------------------------------

    def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        return self._retrying(f"GET {url}", lambda: self._get_once(url, max_bytes), self.config.max_attempts)

    def _retrying[T](self, label: str, fn: Callable[[], T], attempts: int) -> T:
        """Retry transient failures with jittered backoff, honouring ``Retry-After``."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return fn()
            except _RetryableError as exc:
                if attempt >= attempts or exc.fatal_for_mirror:
                    raise FetchError(f"{label} failed after {attempt} attempt(s): {exc}") from exc
                delay = backoff_delay(attempt, self.config.backoff_base, self.config.backoff_max)
                if exc.retry_after is not None:
                    if exc.retry_after > self.config.max_retry_after:
                        raise FetchError(f"{label}: Retry-After {exc.retry_after:.0f}s too long") from exc
                    delay = max(delay, exc.retry_after)
                log.info("%s: %s; retrying in %.1fs", label, exc, delay)
                time.sleep(delay)

    def _get_once(self, url: str, max_bytes: int) -> bytes:
        with self.host_slot(url):
            try:
                with self._client.stream("GET", url) as r:
                    _raise_for_status(r)
                    declared = r.headers.get("Content-Length")
                    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                        raise ResponseTooLargeError(f"{url}: {declared} bytes > max {max_bytes}")
                    out = bytearray()
                    for chunk in r.iter_raw():
                        out += chunk
                        self.throttle(len(chunk))
                        if len(out) > max_bytes:
                            raise ResponseTooLargeError(f"{url}: body exceeds max {max_bytes} bytes")
                    return bytes(out)
            except httpx.TransportError as exc:
                raise _RetryableError(f"{type(exc).__name__}: {exc}") from exc

    # --- files -------------------------------------------------------------------------------

    def download(self, remote: RemoteFile, dest: Path) -> Path:
        return self.fetch(remote, dest).path

    def fetch(self, remote: RemoteFile, dest: Path) -> FetchResult:
        """Download and verify ``remote`` into ``dest``; returns digests and transfer statistics."""
        t0 = time.monotonic()
        target = resolve_target(remote, lambda u, n: self.get_bytes(u, max_bytes=n), self.config)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and target.checksums:
            existing = _already_complete(dest, target)
            if existing is not None:
                return existing
        probes = self._probe_all(target)
        ranged = [p for p in probes if p.ranges and p.size is not None]
        if not ranged:
            return self._single_stream(target, probes, dest, t0)
        size = ranged[0].size
        assert size is not None
        validator = next((p.validator for p in probes if p.url == target.urls[0]), None)
        mirrors = [
            _Mirror(i, p.url, p.final_url, self.config.connections_for(p.final_url))
            for i, p in enumerate(ranged)
        ]
        for m in mirrors:
            m.allowed = m.workers
        if target.pieces is not None:
            piece_size, algo = target.pieces.length, target.pieces.algorithm
        else:
            piece_size, algo = choose_piece_size(size, self.config), "blake3"
        plan = _Plan(target, dest, size, piece_size, algo, mirrors, validator)
        return _SegmentedDownload(self, plan, t0).run()

    def _probe_all(self, target: Target) -> list[_Probe]:
        results: list[_Probe | None] = [None] * len(target.urls)
        errors: list[str] = []
        # A lone source gets the full retry budget; with alternatives, don't stall on a dead mirror.
        attempts = self.config.max_attempts if len(target.urls) == 1 else min(2, self.config.max_attempts)

        def run(i: int, url: str) -> None:
            try:
                results[i] = self._probe(url, attempts)
            except (FetchError, httpx.HTTPError) as exc:
                errors.append(f"{url}: {exc}")
                log.info("mirror %s unusable: %s", url, exc)

        threads = [threading.Thread(target=run, args=(i, u), daemon=True) for i, u in enumerate(target.urls)]
        for t in threads:
            t.start()
        for t in threads:
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
                continue
            if p.size is None:
                p.size = expected
            good.append(p)
        if not good:
            raise MirrorsExhaustedError(f"no usable source for {target.url}: {'; '.join(errors)}")
        return good

    def _probe(self, url: str, attempts: int) -> _Probe:
        return self._retrying(f"probe {url}", lambda: self._probe_once(url), attempts)

    def _probe_once(self, url: str) -> _Probe:
        try:
            with (
                self.host_slot(url),
                self._client.stream("GET", url, headers={"Range": "bytes=0-0"}) as r,
            ):
                validator = r.headers.get("ETag") or r.headers.get("Last-Modified")
                final = str(r.url)
                if r.status_code == 206:
                    m = _CONTENT_RANGE.match(r.headers.get("Content-Range", ""))
                    size = int(m.group(3)) if m and m.group(3) != "*" else None
                    return _Probe(url, final, size, size is not None, validator)
                if r.status_code == 416:
                    m = _CONTENT_RANGE_EMPTY.match(r.headers.get("Content-Range", ""))
                    if m:
                        return _Probe(url, final, int(m.group(1)), True, validator)
                if r.status_code == 200:
                    cl = r.headers.get("Content-Length")
                    size = int(cl) if cl and cl.isdigit() else None
                    return _Probe(url, final, size, False, validator)
                _raise_for_status(r)
                raise _RetryableError(f"unexpected status {r.status_code}", fatal_for_mirror=True)
        except httpx.TransportError as exc:
            raise _RetryableError(f"{type(exc).__name__}: {exc}") from exc

    def _single_stream(self, target: Target, probes: list[_Probe], dest: Path, t0: float) -> FetchResult:
        """Fallback when no source supports Range: sequential download with inline hashing."""
        partial = partial_path(dest)
        errors: list[str] = []
        for p in probes:
            hasher = MultiHasher(c.algorithm for c in target.checksums)
            got = 0
            try:
                with (
                    self.host_slot(p.final_url),
                    self._client.stream("GET", p.final_url) as r,
                    partial.open("wb") as fh,
                ):
                    _raise_for_status(r)
                    for chunk in r.iter_raw():
                        fh.write(chunk)
                        hasher.update(chunk)
                        self.throttle(len(chunk))
                        got += len(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
            except (httpx.TransportError, _RetryableError, OSError) as exc:
                errors.append(f"{p.url}: {exc}")
                hasher.close()
                continue
            digests = hasher.hexdigests()
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
            return FetchResult(
                dest, got, digests, target.checksums, got, 0, time.monotonic() - t0, {p.url: got}
            )
        raise MirrorsExhaustedError(f"all sources failed for {target.url}: {'; '.join(errors)}")


def _already_complete(dest: Path, target: Target) -> FetchResult | None:
    if target.size is not None and dest.stat().st_size != target.size:
        return None
    digests = hash_file(dest, [c.algorithm for c in target.checksums])
    if all(digests.get(c.algorithm) == c.hexdigest for c in target.checksums):
        return FetchResult(dest, dest.stat().st_size, digests, target.checksums)
    return None


def _raise_for_status(r: httpx.Response) -> None:
    if r.status_code < 400:
        return
    retry_after = parse_retry_after(r.headers.get("Retry-After"))
    if r.status_code in _RETRY_STATUSES:
        raise _RetryableError(f"HTTP {r.status_code}", retry_after)
    raise _RetryableError(f"HTTP {r.status_code}", retry_after, fatal_for_mirror=True)


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
    def run(self) -> FetchResult:
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
                        target=self._worker, args=(m, slot), name=f"wsp-fetch-{m.id}.{slot}", daemon=True
                    )
                )
        self.live_workers = len(threads)
        for t in threads:
            t.start()
        try:
            while not self.inorder.done.wait(0.1):
                with self.cond:
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
            for t in threads:
                t.join(timeout=self.cfg.read_timeout + 5)
        if self.error is not None:
            hasher.close()
            self._checkpoint(force=True)
            os.close(self.fd)
            raise self.error
        os.fsync(self.fd)
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
        return FetchResult(
            self.plan.dest,
            self.size,
            digests,
            self.plan.target.checksums,
            self.bytes_downloaded,
            self.bytes_resumed,
            time.monotonic() - self.t0,
            {m.url: m.bytes for m in self.plan.mirrors},
        )

    def _open_partial(self) -> set[int]:
        """Open ``.partial``; returns pieces a previous run recorded (still to be re-verified)."""
        old = load_state(self.state_file)
        resumable = (
            old is not None
            and old.same_target(self.state)
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
                try:
                    data, seconds = self._download_piece(m, i)
                except _CancelledError:
                    self._release(m, i)
                    continue
                except (_RetryableError, httpx.HTTPError, SizeMismatchError) as exc:
                    delay = self._failed(m, i, exc)
                    if delay:
                        time.sleep(delay)
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

    def _download_piece(self, m: _Mirror, i: int) -> tuple[bytearray, float]:
        start, length = self.span(i)
        end = start + length - 1
        buf = bytearray(length)
        view = memoryview(buf)
        pos = 0
        with self.f.host_slot(m.final_url):
            t = time.monotonic()
            with self.f.client.stream("GET", m.final_url, headers={"Range": f"bytes={start}-{end}"}) as r:
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
                    if i in self.claimed or self.finished:
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
        with self.cond:
            self.done.add(i)
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
