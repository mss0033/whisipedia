"""In-memory fakes of the wsp-core ports and a crash injector, for orchestrator tests and integration demos.

These are deliberately strict: e.g. :class:`FakeVault` refuses to ingest into or re-seal a sealed
generation, so a non-idempotent orchestrator step shows up as a test failure.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, cast

from wsp_core.errors import NotFoundError
from wsp_core.ids import make_doc_id
from wsp_core.model import (
    Artifact,
    Checksum,
    DocRecord,
    FetchPlan,
    FetchResult,
    Generation,
    GenerationStatus,
    LedgerEvent,
    ReleaseRef,
    RemoteFile,
    SourceSpec,
    SourceStatus,
    Tier,
    UpdateStrategy,
    Verdict,
    VerdictStatus,
)
from wsp_core.ports import Ctx
from wsp_orchestrator.alerts import Alert, Alerter
from wsp_orchestrator.clock import Clock, ManualClock, SystemClock
from wsp_orchestrator.orchestrator import Wiring
from wsp_orchestrator.pipeline import RefineResult, ReleasePipeline, RetryPolicy, adapter_resolver
from wsp_orchestrator.settings import OrchestratorSettings
from wsp_orchestrator.store import StateStore


class SimulatedCrash(BaseException):
    """Raised by :class:`CrashInjector`. A BaseException so that ``except Exception`` cannot swallow it."""


class CrashInjector:
    """Callable checkpoint hook that raises :class:`SimulatedCrash` at the ``crash_at``-th call."""

    def __init__(self, crash_at: int | None = None) -> None:
        self.crash_at = crash_at
        self.points: list[str] = []

    def __call__(self, point: str) -> None:
        self.points.append(point)
        if self.crash_at is not None and len(self.points) == self.crash_at:
            raise SimulatedCrash(point)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_result(path: Path, verified: Sequence[Checksum] = ()) -> FetchResult:
    """A FetchResult for a file on disk (sha256 + a stand-in for blake3, like the fake vault)."""
    data = path.read_bytes()
    return FetchResult(path, len(data), sha256_hex(data), hashlib.blake2b(data).hexdigest(), tuple(verified))


def make_release(
    source_id: str,
    version: str,
    files: Mapping[str, bytes],
    *,
    license: str | None = None,
    format: str | None = None,
    **meta: str,
) -> ReleaseRef:
    """A ReleaseRef whose files carry sha256 checksums of ``files`` contents."""
    published = None
    with contextlib.suppress(ValueError):
        published = datetime.fromisoformat(version).replace(tzinfo=UTC)
    return ReleaseRef(
        source_id=source_id,
        version=version,
        published=published,
        files=tuple(
            RemoteFile(
                url=f"https://upstream.example/{source_id}/{version}/{name}",
                name=name,
                size=len(data),
                checksums=(Checksum("sha256", sha256_hex(data)),),
            )
            for name, data in files.items()
        ),
        meta=dict(meta),
        license=license,
        format=format,
    )


@dataclass
class FakeAdapter:
    """Serves configured releases. ``contents[(version, name)]`` is what fetch() writes."""

    id: str
    source_id: str = ""  # source the releases belong to; defaults to the adapter id
    releases: list[ReleaseRef] = field(default_factory=list[ReleaseRef])
    contents: dict[tuple[str, str], bytes] = field(default_factory=dict[tuple[str, str], bytes])
    corrupt: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    failures: dict[str, list[BaseException]] = field(default_factory=dict[str, list[BaseException]])
    calls: Counter[str] = field(default_factory=Counter[str])

    def add_release(
        self,
        version: str,
        files: Mapping[str, bytes],
        *,
        license: str | None = None,
        format: str | None = None,
        **meta: str,
    ) -> ReleaseRef:
        ref = make_release(self.source_id or self.id, version, files, license=license, format=format, **meta)
        self.releases.append(ref)
        for name, data in files.items():
            self.contents[(version, name)] = data
        return ref

    def _maybe_fail(self, method: str) -> None:
        self.calls[method] += 1
        queue = self.failures.get(method)
        if queue:
            raise queue.pop(0)

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        self._maybe_fail("discover")
        return sorted(self.releases, key=lambda r: r.version, reverse=True)

    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan:
        self._maybe_fail("plan")
        return FetchPlan(
            release=want, files=want.files, previous_generation=have.generation_id if have else None
        )

    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[FetchResult]:
        """Unverified results (like a fetcher without upstream digests), so ``verify`` still runs."""
        self._maybe_fail("fetch")
        out: list[FetchResult] = []
        for remote in plan.files:
            dest = ctx.workdir / remote.name
            data = self.contents[(plan.release.version, remote.name)]
            if (plan.release.version, remote.name) in self.corrupt:
                data = b"corrupted:" + data
            dest.write_bytes(data)
            out.append(fetch_result(dest))
        return out

    def verify(self, ctx: Ctx, remote: RemoteFile, local: Path) -> Verdict:
        self._maybe_fail("verify")
        if not local.is_file():
            return Verdict(VerdictStatus.MISSING, str(local))
        want = {c.algorithm: c.hexdigest for c in remote.checksums}
        if "sha256" not in want:
            return Verdict(VerdictStatus.UNVERIFIABLE)
        got = sha256_hex(local.read_bytes())
        return Verdict(VerdictStatus.OK) if got == want["sha256"] else Verdict(VerdictStatus.MISMATCH, got)

    def documents(self, ctx: Ctx, generation_dir: Path) -> Iterator[DocRecord]:
        for i, path in enumerate(sorted((generation_dir / "upstream").glob("*"))):
            text = path.read_text(encoding="utf-8", errors="replace")
            yield DocRecord(
                doc_id=make_doc_id(self.id, f"doc{i}"),
                source_id=self.id,
                native_id=f"doc{i}",
                title=path.name,
                lang="en",
                license="CC0-1.0",
                text=text,
            )


@dataclass
class FakeFetcher:
    documents: dict[str, bytes] = field(default_factory=dict[str, bytes])

    def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        if url not in self.documents:
            raise NotFoundError(url, "HTTP 404")
        return self.documents[url][:max_bytes]

    def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
        dest.write_bytes(self.documents[remote.url])
        return fetch_result(dest)


class VaultError(RuntimeError):
    pass


class FakeVault:
    """Filesystem-backed vault under a temporary root, with strict write-once semantics."""

    def __init__(self, root: Path, clock: Clock | None = None) -> None:
        self.root = root
        self.clock = clock or SystemClock()
        self._gens: dict[tuple[str, str], Generation] = {}
        self._current: dict[str, str] = {}
        self.calls: Counter[str] = Counter()
        self.pruned: list[tuple[str, str]] = []

    def stage(self, source_id: str, generation_id: str) -> Path:
        self.calls["stage"] += 1
        gen = self._gens.get((source_id, generation_id))
        if gen is not None and gen.status is not GenerationStatus.STAGING:
            raise VaultError(f"{source_id}/{generation_id} is {gen.status.value}, cannot stage")
        path = self.generation_path(source_id, generation_id)
        (path / "upstream").mkdir(parents=True, exist_ok=True)
        if gen is None:
            self._gens[(source_id, generation_id)] = Generation(
                source_id, generation_id, GenerationStatus.STAGING
            )
        return path

    def ingest(
        self,
        source_id: str,
        generation_id: str,
        local: Path,
        relpath: str,
        *,
        upstream: Sequence[Checksum] = (),
        expected: FetchResult | None = None,
    ) -> Artifact:
        self.calls["ingest"] += 1
        gen = self._gens.get((source_id, generation_id))
        if gen is None or gen.status is not GenerationStatus.STAGING:
            raise VaultError(f"{source_id}/{generation_id} is not staging")
        dest = self.generation_path(source_id, generation_id) / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, dest)
        data = dest.read_bytes()
        art = Artifact(
            source_id=source_id,
            version=generation_id,
            relpath=PurePosixPath(relpath),
            size=len(data),
            sha256=sha256_hex(data),
            blake3=hashlib.blake2b(data).hexdigest(),
            upstream=tuple(upstream),
        )
        if expected is not None and (expected.size, expected.sha256) != (art.size, art.sha256):
            raise VaultError(f"{relpath}: stored bytes differ from the fetch result")
        others = tuple(a for a in gen.artifacts if a.relpath != art.relpath)
        self._gens[(source_id, generation_id)] = replace(gen, artifacts=(*others, art))
        return art

    def adopt(
        self,
        source_id: str,
        generation_id: str,
        path: Path,
        relpath: str,
        *,
        upstream: Sequence[Checksum] = (),
    ) -> Artifact:
        self.calls["adopt"] += 1
        art = self.ingest(source_id, generation_id, path, relpath, upstream=upstream)
        path.unlink()
        return art

    def seal(self, source_id: str, generation_id: str) -> Generation:
        self.calls["seal"] += 1
        gen = self._gens.get((source_id, generation_id))
        if gen is None or gen.status is not GenerationStatus.STAGING:
            raise VaultError(f"{source_id}/{generation_id} is not staging")
        sealed = replace(gen, status=GenerationStatus.SEALED, sealed_at=self.clock.now())
        self._gens[(source_id, generation_id)] = sealed
        manifest = {"artifacts": [str(a.relpath) for a in sealed.artifacts]}
        (self.generation_path(source_id, generation_id) / "MANIFEST.json").write_text(json.dumps(manifest))
        return sealed

    def promote(self, source_id: str, generation_id: str) -> None:
        self.calls["promote"] += 1
        gen = self._gens.get((source_id, generation_id))
        if gen is None or gen.status is not GenerationStatus.SEALED:
            raise VaultError(f"{source_id}/{generation_id} is not sealed")
        self._current[source_id] = generation_id

    def current(self, source_id: str) -> Generation | None:
        gid = self._current.get(source_id)
        return self._gens[(source_id, gid)] if gid else None

    def generations(self, source_id: str) -> Sequence[Generation]:
        return sorted(
            (g for (s, _), g in self._gens.items() if s == source_id), key=lambda g: g.generation_id
        )

    def generation_path(self, source_id: str, generation_id: str) -> Path:
        return self.root / source_id / generation_id

    def prune(self, source_id: str, generation_id: str) -> Path:
        if self._current.get(source_id) == generation_id:
            raise VaultError("refusing to prune the current generation")
        gen = self._gens[(source_id, generation_id)]
        shutil.rmtree(self.generation_path(source_id, generation_id), ignore_errors=True)
        self._gens[(source_id, generation_id)] = replace(gen, status=GenerationStatus.PRUNED, artifacts=())
        self.pruned.append((source_id, generation_id))
        return self.generation_path(source_id, generation_id)

    def add_sealed(self, source_id: str, generation_id: str, *, current: bool = False) -> Generation:
        """Test helper: inject an already sealed generation."""
        gen = Generation(source_id, generation_id, GenerationStatus.SEALED, sealed_at=self.clock.now())
        self._gens[(source_id, generation_id)] = gen
        if current:
            self._current[source_id] = generation_id
        return gen


class FakeLedger:
    """Hash-chained, in-memory ledger."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or SystemClock()
        self._events: list[LedgerEvent] = []

    def append(
        self, kind: str, subject: str, data: Mapping[str, object], *, idempotency_key: str | None = None
    ) -> LedgerEvent:
        if idempotency_key is not None:
            for e in self._events:
                if e.data.get("idempotency_key") == idempotency_key:
                    return e
            data = {**data, "idempotency_key": idempotency_key}
        prev = self._events[-1].hash if self._events else "0" * 64
        seq = len(self._events) + 1
        at = self.clock.now()
        body = json.dumps(
            {
                "seq": seq,
                "at": at.isoformat(),
                "kind": kind,
                "subject": subject,
                "data": dict(data),
                "prev": prev,
            },
            sort_keys=True,
            default=str,
        )
        ev = LedgerEvent(seq, at, kind, subject, dict(data), prev, hashlib.sha256(body.encode()).hexdigest())
        self._events.append(ev)
        return ev

    def events(self, since_seq: int = 0) -> Iterator[LedgerEvent]:
        return iter([e for e in self._events if e.seq > since_seq])

    def tail(self, n: int) -> Sequence[LedgerEvent]:
        return list(self._events[-n:]) if n > 0 else []

    def verify(self) -> bool:
        prev = "0" * 64
        for e in self._events:
            if e.prev_hash != prev:
                return False
            prev = e.hash
        return True

    def of_kind(self, kind: str, subject: str | None = None) -> list[LedgerEvent]:
        return [e for e in self._events if e.kind == kind and (subject is None or e.subject == subject)]


@dataclass
class RecordingSink:
    """AlertSink that remembers what it delivered."""

    delivered: list[Alert] = field(default_factory=list[Alert])

    def deliver(self, alert: Alert) -> None:
        self.delivered.append(alert)

    def alert_keys(self) -> list[str]:
        return [a.key for a in self.delivered]


def make_spec(
    source_id: str,
    *,
    adapter: str | None = None,
    cadence: str | None = None,
    status: SourceStatus = SourceStatus.ACTIVE,
    license: str = "CC0-1.0",
    strategy: UpdateStrategy | None = UpdateStrategy.SNAPSHOT_RETENTION,
    raw: Mapping[str, object] | None = None,
) -> SourceSpec:
    return SourceSpec(
        id=source_id,
        name=source_id.replace("-", " ").title(),
        tier=Tier.T1,
        status=status,
        license=license,
        legal_basis="open-license",
        domain=("encyclopedia",),
        adapter=adapter or source_id,
        cadence=cadence,
        strategy=strategy,
        raw=dict(raw or {}),
    )


@dataclass
class FakeRefinery:
    """Refine/index hooks. ``doc_counts[generation_id]`` is reported as the document count."""

    doc_counts: dict[str, int] = field(default_factory=dict[str, int])
    default_count: int = 1000
    refined: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    indexed: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    failures: list[BaseException] = field(default_factory=list[BaseException])

    def refine(self, source: SourceSpec, generation: Generation, generation_dir: Path) -> RefineResult:
        if self.failures:
            raise self.failures.pop(0)
        self.refined.append((source.id, generation.generation_id))
        return RefineResult(self.doc_counts.get(generation.generation_id, self.default_count))

    def index(self, source: SourceSpec, generation: Generation) -> None:
        self.indexed.append((source.id, generation.generation_id))


class PipelineHarness:
    """Wires a :class:`ReleasePipeline` to fakes. :meth:`restart` simulates a daemon restart.

    The vault, ledger, adapters and refinery are "external" and survive a restart; the pipeline and
    its SQLite connection are rebuilt from the same database file.
    """

    def __init__(
        self,
        root: Path,
        *,
        sources: Sequence[SourceSpec] = (),
        clock: ManualClock | None = None,
        retry: RetryPolicy | None = None,
        crash_at: int | None = None,
    ) -> None:
        self.root = root
        self.clock = clock or ManualClock(datetime(2026, 10, 15, tzinfo=UTC))
        self.sources = list(sources) or [make_spec("src")]
        self.adapters = {s.adapter or s.id: FakeAdapter(s.adapter or s.id, s.id) for s in self.sources}
        self.vault = FakeVault(root / "vault", self.clock)
        self.ledger = FakeLedger(self.clock)
        self.refinery = FakeRefinery()
        self.sink = RecordingSink()
        self.retry = retry or RetryPolicy(max_attempts=3)
        self.injector = CrashInjector(crash_at)
        self.store: StateStore
        self.alerter: Alerter
        self.pipeline: ReleasePipeline
        self._open()

    @property
    def adapter(self) -> FakeAdapter:
        return next(iter(self.adapters.values()))

    @property
    def db_path(self) -> Path:
        return self.root / "state" / "orchestrator.sqlite3"

    def _open(self) -> None:
        self.store = StateStore(self.db_path, check_local=False)
        self.alerter = Alerter(self.store, self.clock, [self.sink])
        self.pipeline = ReleasePipeline(
            store=self.store,
            sources=self.sources,
            adapters=adapter_resolver(self.adapters),
            fetcher=FakeFetcher(),
            vault=self.vault,
            ledger=self.ledger,
            refine=self.refinery.refine,
            index=self.refinery.index,
            clock=self.clock,
            workdir=self.root / "work",
            user_agent="Whisipedia-test/0",
            alerter=self.alerter,
            retry=self.retry,
            checkpoint=self.injector,
        )

    def restart(self, *, crash_at: int | None = None) -> None:
        self.store.close()
        self.injector = CrashInjector(crash_at)
        self._open()


def make_settings(root: Path, **overrides: Any) -> OrchestratorSettings:
    """Settings rooted in a temporary directory (state, work and metrics under ``root``)."""
    base: dict[str, Any] = {
        "state_db": root / "state" / "orchestrator.sqlite3",
        "workdir": root / "work",
        "metrics_path": root / "metrics" / "orchestrator.prom",
        "vault_root": root / "vault",
        "retry": RetryPolicy(max_attempts=3, base_delay=timedelta(minutes=10)),
        "poll_interval_seconds": 0.05,
        "shutdown_grace_seconds": 5.0,
    }
    base.update(overrides)
    return OrchestratorSettings(**base)


@dataclass
class FakeWorld:
    """Fakes behind a :class:`Wiring`; survives orchestrator restarts like real external systems."""

    sources: list[SourceSpec]
    adapters: dict[str, FakeAdapter]
    vault: FakeVault
    ledger: FakeLedger
    refinery: FakeRefinery
    sink: RecordingSink
    scrubbed: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])

    @classmethod
    def build(cls, root: Path, clock: Clock, sources: Sequence[SourceSpec]) -> FakeWorld:
        return cls(
            sources=list(sources),
            adapters={s.adapter or s.id: FakeAdapter(s.adapter or s.id, s.id) for s in sources},
            vault=FakeVault(root / "vault", clock),
            ledger=FakeLedger(clock),
            refinery=FakeRefinery(),
            sink=RecordingSink(),
        )

    def scrub(self, index: int, count: int) -> None:
        self.scrubbed.append((index, count))

    def wiring(self, **extra: Any) -> Wiring:
        return Wiring(
            sources=self.sources,
            adapters=dict(self.adapters),
            fetcher=FakeFetcher(),
            vault=self.vault,
            ledger=self.ledger,
            refine=self.refinery.refine,
            index=self.refinery.index,
            alert_sinks=[self.sink],
            scrub=self.scrub,
            **extra,
        )


def demo_wiring(config: Mapping[str, Any]) -> Wiring:
    """``orchestrator.wiring = "wsp_orchestrator.testing:demo_wiring"``: a self-contained fake world.

    One active source with one release; the fake vault lives next to ``orchestrator.workdir``.
    Useful for smoke-testing the daemon and deployment without network or NAS.
    """
    orch = cast(Mapping[str, Any], config.get("orchestrator", {}))
    root = Path(str(orch["workdir"])).parent
    world = FakeWorld.build(root, SystemClock(), [make_spec("demo")])
    world.adapters["demo"].add_release("2026-10-01", {"demo.txt": b"hello offline world\n"})
    return world.wiring()
