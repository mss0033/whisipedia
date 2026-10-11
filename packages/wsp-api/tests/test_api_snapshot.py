"""The library snapshot (audit 2026-10-11 #4, #5, #11): metadata read off the request path."""

import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.testclient import TestClient

from wsp_api import ApiSettings, create_app
from wsp_api.errors import ApiError
from wsp_api.snapshot import LibraryState, Refreshed, StateSettings, build_snapshot, memoize_domains
from wsp_api.testing import local_client
from wsp_api.testing.fakes import CountingVault, InMemoryLedger, InMemoryVault, sample_services
from wsp_api.views import Library
from wsp_core.model import Generation, GenerationStatus
from wsp_core.ports import Ledger, Vault

BASE = "http://127.0.0.1:8737"


def _counting() -> tuple[Any, CountingVault]:
    services = sample_services()
    vault = CountingVault(cast(InMemoryVault, services.vault))
    return replace(services, vault=cast(Vault, vault)), vault


def test_in_process_reuses_the_snapshot_for_its_period() -> None:
    services, vault = _counting()
    with local_client(create_app(services, ApiSettings(snapshot_seconds=30))) as c:
        for _ in range(3):
            assert c.get("/v1/sources").status_code == 200
            assert c.get("/v1/system").status_code == 200
    reads = [name for name, _ in vault.calls if name == "current"]
    assert len(reads) == len(services.sources)  # one pass over the sources, not one per request


def test_tests_see_vault_changes_at_once() -> None:
    services = sample_services()
    vault = cast(InMemoryVault, services.vault)
    with local_client(create_app(services)) as c:  # snapshot_seconds=0: rebuilt per request
        before = c.get("/v1/sources/rfc").json()["current_generation"]
        vault.add(
            Generation("rfc", "2099-01-01", GenerationStatus.SEALED, sealed_at=datetime.now(UTC)),
            promote=True,
        )
        assert c.get("/v1/sources/rfc").json()["current_generation"] == "2099-01-01" != before


class _SlowLedger(InMemoryLedger):
    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def verify(self) -> bool:
        self.release.wait(10)
        return super().verify()


def test_server_never_blocks_on_the_ledger_replay() -> None:
    services, vault = _counting()
    ledger = _SlowLedger()
    for e in services.ledger.events():
        ledger.append(e.kind, e.subject, e.data)
    services = replace(services, ledger=cast(Ledger, ledger))
    app = create_app(services, ApiSettings(background=True, snapshot_seconds=3600, ops_seconds=3600))
    with TestClient(app, base_url=BASE) as c:
        t0 = time.monotonic()
        first = c.get("/v1/system").json()
        assert time.monotonic() - t0 < 5
        assert first["ledger_ok"] is None  # "unknown" while the replay runs
        vault.calls.clear()
        assert c.get("/v1/sources").status_code == 200
        assert c.get("/v1/sources/rfc/generations").status_code == 200
        assert vault.calls == []  # served from memory
        ledger.release.set()
        deadline = time.monotonic() + 10
        while c.get("/v1/system").json()["ledger_ok"] is None:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert c.get("/v1/system").json()["ledger_ok"] is True


def test_health_is_async_and_answers_while_the_nas_executor_is_saturated() -> None:
    services = sample_services()
    gate = threading.Event()
    calls = {"n": 0}

    class StuckDocs:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def get(self, doc_id: Any, *, at_generation: str | None = None) -> Any:
            calls["n"] += 1
            gate.wait(10)
            return self.inner.get(doc_id, at_generation=at_generation)

        def history(self, doc_id: Any) -> Any:
            return self.inner.history(doc_id)

    services = replace(services, documents=cast(Any, StuckDocs(services.documents)))
    settings = ApiSettings(nas_workers=2, nas_queue=1, nas_timeout_seconds=0.3)
    app = create_app(services, settings)
    with TestClient(app, base_url=BASE) as c:
        results: list[int] = []

        def view() -> None:
            results.append(c.get("/v1/documents/wsp%3Arfc%2F9110").status_code)

        threads = [threading.Thread(target=view) for _ in range(6)]
        for t in threads:
            t.start()
        time.sleep(0.1)
        t0 = time.monotonic()
        assert c.get("/v1/health").status_code == 200
        assert time.monotonic() - t0 < 1
        for t in threads:
            t.join(10)
        gate.set()
        assert sorted(results) == [503] * 6  # 3 admitted then timed out, 3 shed at once
        busy = c.get("/v1/documents/wsp%3Arfc%2F9110")  # slots free again once the stuck reads end
        deadline = time.monotonic() + 5
        while busy.status_code == 503 and time.monotonic() < deadline:
            time.sleep(0.05)
            busy = c.get("/v1/documents/wsp%3Arfc%2F9110")
        assert busy.status_code == 200
        assert busy.headers.get("retry-after") is None


def test_shed_and_timed_out_requests_carry_retry_after() -> None:
    services = sample_services()

    class Slow:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def get(self, doc_id: Any, *, at_generation: str | None = None) -> Any:
            time.sleep(0.5)
            return self.inner.get(doc_id, at_generation=at_generation)

        def history(self, doc_id: Any) -> Any:
            return self.inner.history(doc_id)

    services = replace(services, documents=cast(Any, Slow(services.documents)))
    with local_client(create_app(services, ApiSettings(nas_timeout_seconds=0.1))) as c:
        r = c.get("/v1/documents/wsp%3Arfc%2F9110")
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "timeout"
        assert r.headers["retry-after"] == "5"


def test_executor_threads_never_hold_up_process_exit() -> None:
    """A read stuck on a dead SMB mount must not keep the API (or a test run) from exiting."""
    before = {t.ident for t in threading.enumerate()}
    with local_client(create_app(sample_services())) as c:
        assert c.get("/v1/documents/wsp%3Arfc%2F9110").status_code == 200
        assert c.get("/v1/search", params={"q": "turing"}).status_code == 200
    new = [t for t in threading.enumerate() if t.ident not in before and t.is_alive()]
    assert new
    assert all(t.daemon for t in new)


def test_a_landing_page_that_cannot_be_read_yet_is_retried() -> None:
    """A cold open of a 100 GB ZIM over SMB outlasted the reader's timeout on the real library: the
    failure must not be remembered as "no landing page"."""
    from wsp_api.zimread import ZimUnavailable

    services = sample_services()
    lib = Library(services, ApiSettings(background=True))
    answers: list[str | Exception] = [ZimUnavailable("timed out"), "A/Main"]

    class Finder:
        def home(self, source_id: str, generation_id: str) -> str | None:
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    lib.state.home_finder = Finder()
    snap = lib.state.core.refresh()
    zim = next(s for s in services.sources if s.adapter == "kiwix-opds")
    current = snap.source(zim.id).current
    lib.state._find_homes(snap)  # pyright: ignore[reportPrivateUsage]
    assert lib.state.home_doc_id(zim, current) is None
    lib.state._find_homes(snap)  # pyright: ignore[reportPrivateUsage]
    assert lib.state.home_doc_id(zim, current) == f"wsp:{zim.id}/A/Main"


def test_warming_up_is_a_503_not_a_hang() -> None:
    services = sample_services()
    lib = Library(services, ApiSettings(background=True))
    state = LibraryState(
        vault=services.vault,
        specs=services.sources,
        catalog=lib.catalog,
        ledger=services.ledger,
        ops=None,
        clock=services.clock,
        settings=StateSettings(background=True, snapshot_seconds=60, wait_seconds=0.05),
    )
    with pytest.raises(ApiError) as err:  # never started: no snapshot to serve
        state.snapshot()
    assert (err.value.status, err.value.code) == (503, "warming_up")
    state.core.refresh()
    assert state.snapshot().source("rfc").current is not None


def test_a_failing_source_keeps_its_last_known_state() -> None:
    services = sample_services()
    lib = Library(services)
    good = build_snapshot(services.vault, services.sources, lib.catalog)

    class Flaky:
        def __getattr__(self, name: str) -> Any:
            return getattr(services.vault, name)

        def generations(self, source_id: str) -> Any:
            if source_id == "rfc":
                raise OSError("Host is down")
            return services.vault.generations(source_id)

    later = build_snapshot(cast(Any, Flaky()), services.sources, lib.catalog, good)
    assert later.source("rfc").current == good.source("rfc").current
    assert later.source("rfc").error == "Host is down"
    assert later.source("gutenberg").error is None


def test_refreshed_is_single_flight_in_process() -> None:
    builds: list[int] = []

    def build() -> int:
        builds.append(1)
        time.sleep(0.2)
        return len(builds)

    value = Refreshed("x", build, every=60, background=False)
    got: list[int | None] = []
    threads = [threading.Thread(target=lambda: got.append(value.get())) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert builds == [1]
    assert got == [1] * 16


def test_background_refresh_publishes_new_values_atomically() -> None:
    n = {"v": 0}

    def build() -> dict[str, int]:
        n["v"] += 1
        return {"a": n["v"], "b": n["v"]}  # a reader must never see a half-built value

    value = Refreshed("x", build, every=0.01, background=True)
    assert value.get() is None
    value.start()
    deadline = time.monotonic() + 5
    seen = 0
    while seen < 5 and time.monotonic() < deadline:
        cur = value.get()
        if cur is not None:
            assert cur["a"] == cur["b"]
            seen = cur["a"]
    value.stop()
    assert seen >= 5


def test_domain_lookups_are_memoized(tmp_path: Path) -> None:
    class Vault:
        def __init__(self) -> None:
            self.listings = 0

        def domain_of(self, source_id: str) -> str:
            self.listings += 1
            return "encyclopedia"

        def source_dir(self, source_id: str) -> Path:
            return tmp_path / self.domain_of(source_id) / source_id

    v = Vault()
    assert memoize_domains(v, ttl=60)
    for _ in range(10):
        v.source_dir("wiki")
        v.domain_of("wiki")
    assert v.listings == 1
    assert not memoize_domains(object())
