"""Helpers shared by the adapters: registry access, checksums, plan/fetch/verify, parallel map."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import multiprocessing
import os
import re
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from datetime import UTC, datetime
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
from wsp_core.ports import Ctx, SupportsHead

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


def parse_checksum_file(text: str, name: str | None = None) -> dict[str, str]:
    """Parse ``<hexdigest>  <filename>`` lines (coreutils ``*sum`` format) into ``{filename: digest}``.

    A sidecar that holds only a bare digest (``<file>.sha256`` containing just the hex) is the digest
    of ``name`` when one is given (audit LOW: such files used to yield nothing).
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            digest, fname = parts
            out[fname.lstrip("*").strip()] = digest.lower()
        elif len(parts) == 1 and name is not None and _HEX_RE.fullmatch(parts[0]):
            out.setdefault(name, parts[0].lower())
    return out


_HEX_RE = re.compile(r"[0-9a-fA-F]{32,128}")


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


# --- generations as a chain (audit 2026-10-11 C1, H3) ----------------------------------------------

_DIGEST_SUFFIX_RE = re.compile(r"^(?P<head>.+)[-+][0-9a-f]{8}$")
_DATE_ID_RE = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?[a-z]?$")


def generation_order_key(gen: Path) -> tuple[str, str, str]:
    """Generations of one source in release order: by the date their id starts with, then by when
    the vault sealed them (``MANIFEST.json`` ``sealed_at``), then by id.

    Ids that share a date (two bundle releases of one month, ``2026-08-<digest>``) differ only in a
    digest, which says nothing about order; the seal time does. A sealed bundle release is ordered by
    its seal time first: its id's date is its newest book's, which can go back between releases.
    """
    d = _DIGEST_SUFFIX_RE.match(gen.name)
    head = d.group("head") if d else gen.name
    date = head if _DATE_ID_RE.match(head) else gen.name
    sealed = ""
    try:
        doc: object = json.loads((gen / "MANIFEST.json").read_bytes())
        if isinstance(doc, dict):
            sealed = str(cast(dict[str, object], doc).get("sealed_at") or "")
    except (OSError, ValueError):
        pass
    if d is not None:
        # A bundle's id date is its newest book's, which goes back when that book leaves the selection:
        # a later release then sorted first. Its seal time is its release order; one not sealed yet is
        # the newest (2026-10-26 review).
        return (sealed or "\uffff", date, gen.name)
    return (date, sealed, gen.name)


def earlier_generations(generation_dir: Path) -> list[Path]:
    """The source's other generations (directories with ``upstream/``) before this one, oldest first."""
    me = generation_order_key(generation_dir)
    others = (p for p in generation_dir.parent.iterdir() if p != generation_dir and (p / "upstream").is_dir())
    return [p for p in sorted(others, key=generation_order_key) if generation_order_key(p) < me]


def holder_of(path: Path) -> str:
    """The generation a stored file belongs to (``<gen>/upstream/<file>`` or ``<gen>/<file>``)."""
    parent = path.parent
    return parent.parent.name if parent.name == "upstream" else parent.name


def files_across_generations(generation_dir: Path, names: Iterable[str]) -> dict[str, Path]:
    """Each named file from this generation, else from the newest earlier generation holding it.

    For sources whose release is a fixed set of file names and whose :func:`plan_by_head` skips
    files that did not change upstream. A name held nowhere raises
    :class:`IncompleteGenerationError` (never refine part of a release).
    """
    chain = [generation_dir, *reversed(earlier_generations(generation_dir))]  # newest first
    out: dict[str, Path] = {}
    missing: list[str] = []
    for name in names:
        found = next((p for gen in chain if (p := upstream_dir(gen) / name).is_file()), None)
        if found is None:
            missing.append(name)
        else:
            out[name] = found
    if missing:
        raise IncompleteGenerationError(
            f"{generation_dir.name}: {', '.join(missing)} not in it or any earlier generation"
        )
    return out


LAST_MODIFIED_META = "last_modified:"  # ReleaseRef.meta key prefix: ISO Last-Modified per file name
ETAG_META = "etag:"


def head_release(fetcher: object, urls: Sequence[tuple[str, str]]) -> tuple[list[RemoteFile], dict[str, str]]:
    """``RemoteFile``s for checksum-less ``(url, name)`` pairs plus their HEAD identity as meta.

    ``meta`` holds ``last_modified:<name>`` and ``etag:<name>`` where the server sends them;
    :func:`head_version` and :func:`plan_by_head` read it.
    """
    files: list[RemoteFile] = []
    meta: dict[str, str] = {}
    for url, name in urls:
        size: int | None = None
        if isinstance(fetcher, SupportsHead):
            head = fetcher.head(url)
            size = head.size
            if head.last_modified is not None:
                meta[LAST_MODIFIED_META + name] = head.last_modified.astimezone(UTC).isoformat()
            if head.etag:
                meta[ETAG_META + name] = head.etag
        files.append(RemoteFile(url=url, name=name, size=size))
    return files, meta


def head_version(files: Sequence[RemoteFile], meta: Mapping[str, str]) -> str | None:
    """``<newest Last-Modified date>+<digest of (name, size, ETag, Last-Modified)>``, or ``None``
    when HEAD told nothing (no size, ETag or Last-Modified for any file).

    The same upstream bytes give the same version, so an unchanged release is not fetched again;
    any change to any file gives a new one (the date alone missed same-day changes).
    """
    rows = [
        (
            f.name,
            str(f.size or ""),
            meta.get(ETAG_META + f.name, ""),
            meta.get(LAST_MODIFIED_META + f.name, ""),
        )
        for f in files
    ]
    if not any(r[1] or r[2] or r[3] for r in rows):
        return None
    newest = max((r[3][:10] for r in rows if r[3]), default="undated")
    digest = hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest()[:8]
    return f"{newest}+{digest}"


def plan_by_head(have: Generation | None, want: ReleaseRef) -> FetchPlan:
    """:func:`plan_missing` for files upstream publishes without checksums.

    A file is skipped when the previous generation holds it with the same size and upstream's
    ``Last-Modified`` is older than that generation's seal: it has not changed since it was
    fetched. Everything else (unknown size or date, no previous generation) is fetched. Adapters
    that use this read their files with :func:`files_across_generations`.
    """
    base = plan_missing(have, want)
    if have is None or have.sealed_at is None:
        return base
    held = {a.relpath.name: a.size for a in have.artifacts}
    sealed = have.sealed_at if have.sealed_at.tzinfo else have.sealed_at.replace(tzinfo=UTC)
    needed: list[RemoteFile] = []
    for remote in base.files:
        stamp = want.meta.get(LAST_MODIFIED_META + remote.name)
        unchanged = (
            remote.name in held
            and remote.size is not None
            and remote.size == held[remote.name]
            and stamp is not None
            and datetime.fromisoformat(stamp) < sealed
        )
        if not unchanged:
            needed.append(remote)
    return FetchPlan(release=want, files=tuple(needed), previous_generation=base.previous_generation)


def fetch_all(ctx: Ctx, plan: FetchPlan) -> Iterator[FetchResult]:
    """Download every planned file into ``ctx.workdir`` through the injected fetcher."""
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    for remote in plan.files:
        yield ctx.fetcher.download(remote, ctx.workdir / remote.name)


def upstream_dir(generation_dir: Path) -> Path:
    """Generations keep upstream bytes under ``upstream/`` (PLAN §7); fall back to the dir itself."""
    sub = generation_dir / "upstream"
    return sub if sub.is_dir() else generation_dir


def available_cpus(cgroup_root: Path = Path("/sys/fs/cgroup")) -> int:
    """CPUs this process may use: its affinity mask, lowered by a cgroup v2 ``cpu.max`` quota.

    ``os.cpu_count`` reports the host; a service in a slice capped at, say, 400% CPU would start
    one worker per host core and thrash (audit LOW).
    """
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    try:
        own = (Path("/proc/self/cgroup").read_text().strip().split("::", 1)[-1] or "/").lstrip("/")
    except OSError:
        own = ""
    here = cgroup_root / own
    while True:
        try:
            quota, period = (here / "cpu.max").read_text().split()[:2]
            if quota != "max" and int(period) > 0:
                cpus = min(cpus, max(1, -(-int(quota) // int(period))))
        except (OSError, ValueError):
            pass
        if here == cgroup_root or cgroup_root not in here.parents:
            break
        here = here.parent
    return max(1, cpus)


def worker_cap() -> int | None:
    """``WSP_MAX_WORKERS`` (a positive integer), else ``None``."""
    cap = os.environ.get("WSP_MAX_WORKERS", "")
    return int(cap) if cap.isdigit() and int(cap) > 0 else None


def default_workers() -> int:
    """All usable CPUs but one, capped by ``WSP_MAX_WORKERS`` so background jobs leave room."""
    workers = max(1, available_cpus() - 1)
    cap = worker_cap()
    return min(workers, cap) if cap is not None else workers


def workers_for(spec: SourceSpec, default: int | None = None) -> int:
    """``access.workers`` (or ``default``, else :func:`default_workers`), never above ``WSP_MAX_WORKERS``.

    The registry option used to bypass the cap (audit LOW).
    """
    workers = int_option(spec, "workers", default if default is not None else default_workers())
    cap = worker_cap()
    return min(workers, cap) if cap is not None else workers


Reject = tuple[str, str]  # (record id or position, error)


class TooManyRejectsError(RuntimeError):
    """More records failed to convert than :class:`Rejects` tolerates: the input is suspect."""


class Rejects:
    """Records an adapter could not convert, kept out of the refine instead of aborting it (audit M4).

    Each one is appended to ``<workdir>/_rejects-<generation>.jsonl`` (local disk) with its id and
    error. A pass fails with :class:`TooManyRejectsError` once more than ``floor`` records *and*
    more than ``ratio`` of those seen have failed: a few malformed records in 30 million are logged
    and skipped, a systematic failure stops the refine early.
    """

    def __init__(self, workdir: Path, generation: str, *, ratio: float = 0.001, floor: int = 100) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        self.path = workdir / f"_rejects-{generation}.jsonl"
        self.path.unlink(missing_ok=True)
        self.ratio = ratio
        self.floor = floor
        self.seen = 0
        self.count = 0

    def add(self, rejects: Sequence[Reject], ok: int) -> None:
        """Record one batch's outcome: ``ok`` converted records and ``rejects``."""
        self.seen += ok + len(rejects)
        if not rejects:
            return
        self.count += len(rejects)
        with self.path.open("a", encoding="utf-8") as fh:
            for record, error in rejects:
                fh.write(json.dumps({"record": record, "error": error[:500]}) + "\n")
        if self.count > self.floor and self.count > self.ratio * self.seen:
            raise TooManyRejectsError(
                f"{self.count} of {self.seen} records failed to convert (see {self.path}); "
                f"last: {rejects[-1]}"
            )


def describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


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
