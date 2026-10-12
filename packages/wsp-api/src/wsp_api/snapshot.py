"""The library's metadata as one immutable snapshot, refreshed in the background (audit 2026-10-11 #4).

Every Navigator page needs the site list, and ``/v1/system`` needs every source's generations, its
``CURRENT`` pointer, the degraded flags, the ledger and the operations status. On the NAS that is
thousands of SMB round trips (2.6-5 s warm, 10-20 s cold, recomputed on every request). Here it is
read by background threads into immutable values that requests only *look at*:

* :class:`LibrarySnapshot`: per source the current generation (and a partitioned source's current
  set), its generations and their directories; plus document counts. Swapped atomically.
* the operations status, the proposals, the ledger tail and the ledger verification: separate
  :class:`Refreshed` values with their own periods. Until the first ledger replay has finished,
  ``ledger_ok`` is ``None`` ("unknown") instead of blocking a request for tens of seconds.
* ZIM landing pages, which never change for a generation, computed once per generation.

With ``background=False`` (tests, the in-process CLI) nothing runs in the background: a value older
than its period is rebuilt by the request that needs it, and the vault is used directly.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MethodType
from typing import Protocol

from wsp_api.errors import ApiError
from wsp_api.schemas import OpsStatus, ProposalInfo
from wsp_api.seams import OpsProvider, VaultView
from wsp_core.ids import make_doc_id
from wsp_core.model import Generation, GenerationStatus, LedgerEvent, SourceSpec
from wsp_core.ports import Catalog, Ledger

log = logging.getLogger(__name__)

MAX_LEDGER_TAIL = 500  # the largest ``ledger_tail`` /v1/system accepts
ZIM_ADAPTER = "kiwix-opds"


class HomeFinder(Protocol):
    def home(self, source_id: str, generation_id: str) -> str | None: ...


# --- values -----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceState:
    current: Generation | None = None
    members: tuple[Generation, ...] = ()  # a partitioned source's current set (ADR-0010)
    generations: tuple[Generation, ...] = ()  # oldest first
    paths: Mapping[str, Path] = field(default_factory=dict[str, Path])
    error: str | None = None  # the last refresh failed; the values are from the one before


_EMPTY = SourceState()


class _Failed:
    """A landing page that could not be read this time."""


_FAILED = _Failed()


@dataclass(frozen=True, slots=True)
class LibrarySnapshot:
    built_at: float  # time.monotonic()
    sources: Mapping[str, SourceState]
    doc_counts: Mapping[str, int | None]

    def source(self, source_id: str) -> SourceState:
        return self.sources.get(source_id, _EMPTY)

    def degraded(self) -> list[tuple[Generation, bool]]:
        """Degraded generations (with "is it current") of every source, by source id."""
        out: list[tuple[Generation, bool]] = []
        for sid in sorted(self.sources):
            st = self.sources[sid]
            cur = None if st.current is None else st.current.generation_id
            out += [
                (g, g.generation_id == cur) for g in st.generations if g.status is GenerationStatus.DEGRADED
            ]
        return out


def build_snapshot(
    vault: VaultView,
    specs: Sequence[SourceSpec],
    catalog: Catalog,
    previous: LibrarySnapshot | None = None,
) -> LibrarySnapshot:
    """Read every source's metadata. A source that fails keeps its previous values, marked with the error."""
    sources: dict[str, SourceState] = {}
    counts: dict[str, int | None] = {}
    for spec in specs:
        try:
            gens = tuple(sorted(vault.generations(spec.id), key=lambda g: g.generation_id))
            current = vault.current(spec.id)
            members = tuple(vault.current_set(spec.id)) if spec.partitioned else ()
            usable = {g.generation_id for g in gens if g.status is not GenerationStatus.STAGING}
            paths = {gid: vault.generation_path(spec.id, gid) for gid in sorted(usable)}
            sources[spec.id] = SourceState(current, members, gens, paths)
        except Exception as exc:
            log.warning("snapshot: cannot read %s: %s", spec.id, exc)
            old = previous.source(spec.id) if previous is not None else _EMPTY
            sources[spec.id] = SourceState(old.current, old.members, old.generations, old.paths, str(exc))
        try:
            counts[spec.id] = catalog.doc_count(spec.id)
        except Exception:
            counts[spec.id] = None if previous is None else previous.doc_counts.get(spec.id)
    return LibrarySnapshot(time.monotonic(), sources, counts)


# --- a value refreshed on a period ------------------------------------------------------------------


class Refreshed[T]:
    """A value rebuilt every ``every`` seconds: by a background thread (``background``), or by the
    first caller that finds it older than that (single-flight: concurrent callers share one build)."""

    def __init__(self, name: str, build: Callable[[], T], *, every: float, background: bool) -> None:
        self.name = name
        self._build = build
        self.every = every
        self.background = background
        self._value: tuple[float, T] | None = None
        self._ready = threading.Event()
        self._build_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.after: Callable[[T], None] | None = None  # runs after each background build
        self.errors = 0

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def peek(self) -> T | None:
        value = self._value
        return None if value is None else value[1]

    def age(self) -> float | None:
        value = self._value
        return None if value is None else time.monotonic() - value[0]

    def refresh(self) -> T:
        """Build now (in this thread) and publish the result."""
        with self._build_lock:
            started = time.monotonic()
            value = self._build()
            self._value = (started, value)
            self._ready.set()
            return value

    def get(self) -> T | None:
        """The value. In background mode: the latest one, or ``None`` before the first build. Otherwise
        rebuilt here when older than ``every`` (or never built)."""
        if self.background:
            return self.peek()
        current = self._value
        if current is not None and time.monotonic() - current[0] < self.every:
            return current[1]
        with self._build_lock:  # another caller may have just rebuilt it
            current = self._value
            if current is not None and current[0] >= time.monotonic() - self.every and self.every > 0:
                return current[1]
            started = time.monotonic()
            value = self._build()
            self._value = (started, value)
            self._ready.set()
            return value

    def wait(self, timeout: float) -> T | None:
        """Background mode: wait up to ``timeout`` for the first build."""
        if not self.background:
            return self.get()
        self._ready.wait(timeout)
        return self.peek()

    def poke(self) -> None:
        """Ask the background thread to rebuild now."""
        self._wake.set()

    def start(self) -> None:
        if not self.background or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name=f"refresh-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                value = self.refresh()
                if self.after is not None:
                    self.after(value)
            except Exception:
                self.errors += 1
                log.warning("refreshing %s failed", self.name, exc_info=True)
            self._wake.wait(self.every)
            self._wake.clear()


# --- the API's view of the library ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StateSettings:
    background: bool = False
    snapshot_seconds: float = 0.0
    ops_seconds: float = 0.0
    ledger_verify_seconds: float = 300.0
    wait_seconds: float = 20.0  # background: how long a request waits for the very first snapshot


class LibraryState:
    """Snapshot, ops, proposals and ledger for the routes (see the module docstring)."""

    def __init__(
        self,
        *,
        vault: VaultView,
        specs: Sequence[SourceSpec],
        catalog: Catalog,
        ledger: Ledger,
        ops: OpsProvider | None,
        clock: Callable[[], datetime],
        settings: StateSettings,
    ) -> None:
        self.settings = settings
        self._vault = vault
        self._specs = list(specs)
        self._ledger = ledger
        self._ops_provider = ops
        self._clock = clock
        self._homes: dict[tuple[str, str], str | None] = {}
        self._homes_lock = threading.Lock()
        self.home_finder: HomeFinder | None = None
        bg = settings.background
        self.core = Refreshed(
            "snapshot",
            lambda: build_snapshot(vault, self._specs, catalog, self.core.peek()),
            every=settings.snapshot_seconds,
            background=bg,
        )
        self.core.after = self._find_homes
        self.ops = Refreshed("ops", self._build_ops, every=settings.ops_seconds, background=bg)
        self.proposals = Refreshed(
            "proposals",
            lambda: tuple(ops.proposals(None)) if ops is not None else (),
            every=settings.ops_seconds,
            background=bg,
        )
        self.ledger_tail = Refreshed(
            "ledger-tail",
            lambda: tuple(ledger.tail(MAX_LEDGER_TAIL)),
            every=settings.ops_seconds,
            background=bg,
        )
        self.ledger_ok = Refreshed(
            "ledger-verify", ledger.verify, every=settings.ledger_verify_seconds, background=bg
        )
        self.vault: VaultView = SnapshotVault(self, vault) if bg else vault

    @property
    def parts(self) -> tuple[Refreshed[object], ...]:
        return (self.core, self.ops, self.proposals, self.ledger_tail, self.ledger_ok)  # pyright: ignore[reportReturnType]

    def start(self) -> None:
        for part in self.parts:
            part.start()

    def stop(self) -> None:
        for part in self.parts:
            part.stop()

    @property
    def background(self) -> bool:
        return self.settings.background

    # --- the snapshot ---

    def snapshot(self) -> LibrarySnapshot:
        snap = self.core.wait(self.settings.wait_seconds) if self.background else self.core.get()
        if snap is None:
            raise ApiError(503, "warming_up", "the library index is still loading; try again shortly")
        return snap

    async def run[R](self, fn: Callable[[], R]) -> R:
        """Run a snapshot-only computation: inline when the snapshot is in memory (server), else in a
        worker thread (it may read the vault)."""
        if self.background and self.core.ready:
            return fn()
        return await asyncio.to_thread(fn)

    def _build_ops(self) -> OpsStatus | None:
        return None if self._ops_provider is None else self._ops_provider.status(self._clock())

    # --- ZIM landing pages ---

    def home_doc_id(self, spec: SourceSpec, current: Generation | None) -> str | None:
        """Landing page of a Kiwix site (ZIM main page, or a bundle's book list). Cached per generation;
        in the server it is computed by the snapshot refresher, never by a request."""
        if current is None or spec.adapter != ZIM_ADAPTER or self.home_finder is None:
            return None
        key = (spec.id, current.generation_id)
        with self._homes_lock:
            if key in self._homes or self.background:
                return self._homes.get(key)
        native = self._home(spec.id, current.generation_id)
        if isinstance(native, _Failed):
            return None
        with self._homes_lock:
            self._homes[key] = native
        return native

    def _home(self, source_id: str, generation_id: str) -> str | _Failed | None:
        """The landing page's doc id, ``None`` when there is none, :data:`_FAILED` when it could not be
        read (not remembered: the next refresh tries again; a cold open of a 100 GB ZIM over SMB can
        outlast the reader's timeout, and the worker keeps the archive once it is open)."""
        assert self.home_finder is not None
        try:
            native = self.home_finder.home(source_id, generation_id)
        except Exception as exc:
            log.warning("cannot read the landing page of %s/%s yet: %s", source_id, generation_id, exc)
            return _FAILED
        return None if native is None else make_doc_id(source_id, native)

    def _find_homes(self, snap: LibrarySnapshot) -> None:
        """After a background snapshot: landing pages of current Kiwix generations not known yet.
        This also opens each current ZIM once, so the first page view does not wait for it."""
        if self.home_finder is None:
            return
        for spec in self._specs:
            cur = snap.source(spec.id).current
            if spec.adapter != ZIM_ADAPTER or cur is None:
                continue
            key = (spec.id, cur.generation_id)
            with self._homes_lock:
                if key in self._homes:
                    continue
            native = self._home(*key)
            warm = getattr(self.home_finder, "warm_soon", None)
            if callable(warm):
                warm(*key)  # every archive of the generation, in the background
            if isinstance(native, _Failed):
                continue
            with self._homes_lock:
                self._homes[key] = native


class SnapshotVault:
    """:class:`VaultView` answered from the latest snapshot: no NAS access on the request path."""

    def __init__(self, state: LibraryState, vault: VaultView) -> None:
        self._state = state
        self._vault = vault

    @property
    def root(self) -> object:
        return getattr(self._vault, "root", None)

    def _source(self, source_id: str) -> SourceState:
        return self._state.snapshot().source(source_id)

    def current(self, source_id: str) -> Generation | None:
        return self._source(source_id).current

    def current_set(self, source_id: str) -> Sequence[Generation]:
        st = self._source(source_id)
        return st.members or ([st.current] if st.current is not None else [])

    def generations(self, source_id: str) -> Sequence[Generation]:
        return self._source(source_id).generations

    def generation_path(self, source_id: str, generation_id: str) -> Path:
        found = self._source(source_id).paths.get(generation_id)
        return found if found is not None else self._vault.generation_path(source_id, generation_id)


# --- source -> domain memo (until FileVault caches it itself) ----------------------------------------


def memoize_domains(vault: object, *, ttl: float = 300.0) -> bool:
    """Cache ``vault.domain_of`` for ``ttl`` seconds per source.

    ``FileVault.domain_of`` lists every domain directory of the library and staging trees on each call
    (an SMB round trip per domain), and every path helper calls it: about 3 s per pass over 79 sources
    (audit #4). The domain of a source changes only when an operator moves it, so a few minutes of
    staleness is harmless. Returns whether a memo was installed. TODO(vault): cache in FileVault.
    """
    original = getattr(vault, "domain_of", None)
    if not isinstance(original, MethodType) or not hasattr(vault, "__dict__"):
        return False
    lookup: Callable[[str], str] = original
    memo: dict[str, tuple[float, str]] = {}
    lock = threading.Lock()

    def domain_of(source_id: str) -> str:
        now = time.monotonic()
        with lock:
            hit = memo.get(source_id)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        domain = lookup(source_id)
        with lock:
            memo[source_id] = (now, domain)
        return domain

    vault.__dict__["domain_of"] = domain_of
    return True


def proposals_with(items: Iterable[ProposalInfo], status: str | None) -> list[ProposalInfo]:
    return sorted(
        (p for p in items if status is None or p.status == status), key=lambda p: p.id, reverse=True
    )


def tail(events: Sequence[LedgerEvent], n: int) -> list[LedgerEvent]:
    return list(events[-n:]) if n > 0 else []
