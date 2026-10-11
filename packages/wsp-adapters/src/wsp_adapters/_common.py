"""Helpers shared by the adapters: registry access, checksums, plan/fetch/verify, parallel map."""

from __future__ import annotations

import dataclasses
import hashlib
import multiprocessing
import os
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from pathlib import Path
from typing import cast

from wsp_core.model import (
    Checksum,
    DocRecord,
    FetchPlan,
    FetchResult,
    Generation,
    ReleaseRef,
    RemoteFile,
    SourceSpec,
    Verdict,
    VerdictStatus,
)
from wsp_core.ports import Ctx

# Strongest first. Verification uses the strongest digest upstream published.
HASH_PREFERENCE = ("sha512", "sha256", "sha1", "md5")
_HASH_ALIASES = {"sha-1": "sha1", "sha-256": "sha256", "sha-512": "sha512"}
_READ_CHUNK = 8 * 1024 * 1024


class IncompleteGenerationError(ValueError):
    """A generation's content set is missing files: refining it would publish a partial release, and
    the snapshot diff would delete every document it lacks. Raised instead (audit 2026-10-11 C1)."""


def access(spec: SourceSpec) -> Mapping[str, object]:
    """The ``access`` block of a registry entry (empty mapping if absent)."""
    value = spec.raw.get("access")
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def str_list(value: object) -> tuple[str, ...]:
    """Coerce a registry scalar-or-list into a tuple of strings."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(v) for v in cast(Sequence[object], value))
    raise ValueError(f"expected a string or a list, got {value!r}")


def int_option(spec: SourceSpec, key: str, default: int) -> int:
    value = access(spec).get(key, default)
    if not isinstance(value, int) or value < 1:
        raise ValueError(f"{spec.id}: access.{key} must be a positive integer")
    return value


def normalize_algorithm(name: str) -> str:
    name = name.strip().lower()
    return _HASH_ALIASES.get(name, name)


def parse_checksum_file(text: str) -> dict[str, str]:
    """Parse ``<hexdigest>  <filename>`` lines (coreutils ``*sum`` format) into ``{filename: digest}``."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            digest, name = parts
            out[name.lstrip("*").strip()] = digest.lower()
    return out


def strongest(checksums: Iterable[Checksum]) -> Checksum | None:
    by_alg = {normalize_algorithm(c.algorithm): c for c in checksums}
    for alg in HASH_PREFERENCE:
        if alg in by_alg:
            return by_alg[alg]
    return None


def hash_file(path: Path, algorithms: Iterable[str]) -> dict[str, str]:
    """Hash ``path`` with every algorithm in one read pass."""
    hashers = {alg: hashlib.new(alg) for alg in algorithms}
    with path.open("rb") as fh:
        while chunk := fh.read(_READ_CHUNK):
            for h in hashers.values():
                h.update(chunk)
    return {alg: h.hexdigest() for alg, h in hashers.items()}


def verify_file(remote: RemoteFile, local: Path) -> Verdict:
    """Check ``local`` against the size and the strongest checksum published for ``remote``."""
    if not local.is_file():
        return Verdict(VerdictStatus.MISSING, f"{local.name} not found")
    size = local.stat().st_size
    if remote.size is not None and size != remote.size:
        return Verdict(VerdictStatus.MISMATCH, f"size {size} != expected {remote.size}")
    best = strongest(remote.checksums)
    if best is None:
        return Verdict(VerdictStatus.UNVERIFIABLE, "upstream publishes no checksum")
    alg = normalize_algorithm(best.algorithm)
    actual = hash_file(local, [alg])[alg]
    if actual != best.hexdigest.lower():
        return Verdict(VerdictStatus.MISMATCH, f"{alg} {actual} != expected {best.hexdigest}")
    return Verdict(VerdictStatus.OK, f"{alg} matches")


def plan_missing(have: Generation | None, want: ReleaseRef) -> FetchPlan:
    """Plan every file of ``want`` that ``have`` does not already hold byte-identically.

    Kiwix bundles keep unchanged ZIMs (same dated file name and checksum) across months; those are
    carried over by the vault instead of being downloaded again.
    """
    held: dict[str, set[tuple[str, str]]] = {}
    if have is not None:
        for art in have.artifacts:
            digests = {(normalize_algorithm(c.algorithm), c.hexdigest.lower()) for c in art.upstream}
            digests.add(("sha256", art.sha256.lower()))
            held[art.relpath.name] = digests
    needed: list[RemoteFile] = []
    for remote in want.files:
        digests = held.get(remote.name)
        best = strongest(remote.checksums)
        if digests is None or best is None:
            needed.append(remote)
            continue
        if (normalize_algorithm(best.algorithm), best.hexdigest.lower()) not in digests:
            needed.append(remote)
    return FetchPlan(
        release=want,
        files=tuple(needed),
        previous_generation=have.generation_id if have is not None else None,
    )


def fetch_all(ctx: Ctx, plan: FetchPlan) -> Iterator[FetchResult]:
    """Download every planned file into ``ctx.workdir`` through the injected fetcher."""
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    for remote in plan.files:
        yield ctx.fetcher.download(remote, ctx.workdir / remote.name)


def upstream_dir(generation_dir: Path) -> Path:
    """Generations keep upstream bytes under ``upstream/`` (PLAN §7); fall back to the dir itself."""
    sub = generation_dir / "upstream"
    return sub if sub.is_dir() else generation_dir


def default_workers() -> int:
    """All cores but one, capped by ``WSP_MAX_WORKERS`` so background jobs leave the machine usable."""
    workers = max(1, (os.cpu_count() or 2) - 1)
    cap = os.environ.get("WSP_MAX_WORKERS", "")
    return min(workers, int(cap)) if cap.isdigit() and int(cap) > 0 else workers


DocRow = tuple[object, ...]
_DOC_FIELDS = tuple(f.name for f in dataclasses.fields(DocRecord))


def doc_row(doc: DocRecord) -> DocRow:
    """A ``DocRecord`` as a plain tuple for the trip from a worker to the parent (audit H5).

    Unpickling a frozen slotted dataclass costs 4.1 us per document; a tuple 0.7 us, and
    :func:`doc_from_row` 1.7 us more (measured on 20,000 1.2 KB records).
    """
    return tuple(getattr(doc, name) for name in _DOC_FIELDS)


def doc_from_row(row: DocRow) -> DocRecord:
    return DocRecord(*row)  # pyright: ignore[reportArgumentType]


class WorkerPool:
    """One process pool for a whole refine (spawned on first use, shut down on exit).

    Starting a spawn pool costs about half a second (a fresh interpreter per worker importing the
    adapters); a refine that maps several phases over many files (181 Stack Exchange ZIMs, three
    phases each) used to pay it every time. ``workers <= 1`` runs everything inline.
    """

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self._pool: ProcessPoolExecutor | None = None

    def __enter__(self) -> WorkerPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None

    def map[T, R](
        self, fn: Callable[[T], R], items: Iterable[T], *, window: int | None = None
    ) -> Iterator[R]:
        """Like ``map`` across processes, preserving order, with at most ``window`` tasks in flight.

        The bounded window keeps memory flat when the consumer is slower than the workers (the
        stdlib ``Executor.map`` submits every item up front); with tasks bounded by bytes it bounds
        the bytes in flight too.
        """
        if self.workers <= 1:
            yield from map(fn, items)
            return
        if self._pool is None:
            # spawn: safe with threads in the parent and identical on every platform.
            ctx = multiprocessing.get_context("spawn")
            self._pool = ProcessPoolExecutor(max_workers=self.workers, mp_context=ctx)
        pool = self._pool
        window = window or self.workers * 2
        pending: deque[Future[R]] = deque()
        try:
            for item in items:
                pending.append(pool.submit(fn, item))
                if len(pending) >= window:
                    yield pending.popleft().result()
            while pending:
                yield pending.popleft().result()
        finally:
            for f in pending:  # the consumer stopped early or a task failed: drop the rest
                f.cancel()


def ordered_parallel_map[T, R](
    fn: Callable[[T], R], items: Iterable[T], workers: int, *, window: int | None = None
) -> Iterator[R]:
    """:meth:`WorkerPool.map` on a pool of its own (for one-off maps)."""
    with WorkerPool(workers) as pool:
        yield from pool.map(fn, items, window=window)
