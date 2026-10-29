"""Regressions of the vault, orchestrator and lanes audit (docs/audits/2026-10-12/)."""

from __future__ import annotations

import argparse
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from test_doccollections import GOOD, OBJECTS, routes, spec

from wsp_cli import bootstrap, doccollections, ops, wiring
from wsp_cli.doccollections import (
    EXIT_ALERT,
    EXIT_COMPLETE,
    EXIT_LISTING_INCOMPLETE,
    EXIT_NOTHING_NEW,
    EXIT_PARTIAL,
    CollectionFetcher,
    FetchReport,
    fetch_exit_code,
)
from wsp_cli.ingest import Ingestor, VersionStatus
from wsp_cli.locks import HEAVY, LockHeldError, exclusive, lock_dir_for
from wsp_core.model import FetchResult, RemoteFile, SourceSpec
from wsp_core.testing.adapter_contract import FakeFetcher
from wsp_vault.ledger import JsonlLedger
from wsp_vault.signing import Ed25519Signer
from wsp_vault.vault import FileVault

SID = "test-pubs"


@pytest.fixture
def vault(tmp_path: Path) -> FileVault:
    signer = Ed25519Signer.generate()
    root = tmp_path / "vault"
    return FileVault(root, signer, domains={SID: "preparedness"}, ledger=JsonlLedger(root / "ledger", signer))


def fetch(
    vault: FileVault, tmp_path: Path, objects: list[tuple[str, bytes]], version: str, **kw: Any
) -> FetchReport:
    fetcher = FakeFetcher(kw.pop("routes", None) or routes(objects))
    runner = CollectionFetcher(
        vault.root,
        fetcher,
        keys={},
        user_agent="t",
        workdir=tmp_path / "work",
        out=io.StringIO(),
        throttle=False,
    )
    return runner.run(spec(objects), version=version, **kw)


def ingest(vault: FileVault, version: str | None = None) -> list[tuple[str, VersionStatus, bool]]:
    report = Ingestor(vault, [spec(OBJECTS)], out=io.StringIO()).run(source_id=SID, version=version)
    return [(v.generation_id, v.status, v.promoted) for v in report.versions]


# --- C1: one exit code per fetch outcome -----------------------------------------------------------------


def test_each_fetch_outcome_has_its_own_exit_code() -> None:
    def report(**kw: Any) -> FetchReport:
        out = FetchReport(SID, "2026-10-12")
        for k, v in kw.items():
            setattr(out, k, v)
        return out

    doc = object()
    assert fetch_exit_code(report(fetched=[doc])) == EXIT_COMPLETE == 0
    assert fetch_exit_code(report()) == EXIT_NOTHING_NEW == 8
    assert fetch_exit_code(report(fetched=[doc], alerts=["x"])) == EXIT_ALERT == 7
    assert fetch_exit_code(report(fetched=[doc], failed=[("k", "timeout")])) == EXIT_PARTIAL == 6
    assert fetch_exit_code(report(failed=[("k", "locked")], alerts=["x"])) == EXIT_PARTIAL  # worst wins
    assert fetch_exit_code(report(incomplete=["listing"])) == EXIT_LISTING_INCOMPLETE == 4


def test_a_fetch_whose_downloads_failed_is_partial_not_complete(vault: FileVault, tmp_path: Path) -> None:
    """lane-refetch2 r4 had "failed 5" and exit 0, and the lane marked it done for good."""
    from wsp_core.errors import FetchError

    objects = [("FM-5-0_2022.pdf", GOOD), ("FM-6-0_2022.pdf", GOOD)]

    class Flaky(FakeFetcher):  # this download fails the way a busy server fails (a 404 is "gone")
        def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
            if remote.url.endswith("FM-6-0_2022.pdf"):
                raise FetchError(f"{remote.url}: HTTP 503")
            return super().download(remote, dest)

    runner = CollectionFetcher(
        vault.root,
        Flaky(routes(objects)),
        keys={},
        user_agent="t",
        workdir=tmp_path / "work",
        out=io.StringIO(),
        throttle=False,
    )
    report = runner.run(spec(objects), version="2026-10-12")
    assert [d.cand.key for d in report.fetched] == ["test:FM-5-0_2022.pdf"]
    assert report.failed
    assert fetch_exit_code(report) == EXIT_PARTIAL
    # What did arrive is planned: the lane ingests it, and fetches again later for the rest.
    assert ingest(vault, "2026-10-12") == [("2026-10-12", VersionStatus.INGESTED, True)]


def _fetch_args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "config_dir": None,
        "source": SID,
        "version": "2026-10-12",
        "limit": None,
        "max_pages": None,
        "dry_run": False,
        "partition": None,
    }
    return argparse.Namespace(**{**base, **kw})


def test_cmd_fetch_returns_the_outcome_code(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def nothing(*a: object) -> None:
        return None

    def config(*a: object) -> dict[str, Any]:
        return {}

    def sources(*a: object) -> list[SourceSpec]:
        return [spec(OBJECTS)]

    def fetcher(*a: object, **k: object) -> FakeFetcher:
        return FakeFetcher(routes(OBJECTS))

    def paths(*a: object) -> _Paths:
        return _Paths(vault.root, tmp_path / "w")

    def state_dir() -> Path:
        return tmp_path / "state"

    monkeypatch.setattr(bootstrap, "layout", nothing)
    monkeypatch.setattr(bootstrap, "load", config)
    monkeypatch.setattr(bootstrap, "load_sources", sources)
    monkeypatch.setattr(bootstrap, "build_fetcher", fetcher)
    monkeypatch.setattr(bootstrap, "paths", paths)
    monkeypatch.setattr(doccollections, "write_review_proposal", nothing)
    monkeypatch.setattr(doccollections, "state_dir", state_dir)
    monkeypatch.setattr(
        doccollections.CollectionFetcher, "__init__", _unthrottled(CollectionFetcher.__init__)
    )
    out = io.StringIO()
    assert doccollections.cmd_fetch(_fetch_args(), out) == EXIT_ALERT  # restricted.pdf is listed
    assert "outcome: complete, with alerts (exit 7)" in out.getvalue()
    assert f"next: wsp ingest --source {SID} --version 2026-10-12" in out.getvalue()
    assert doccollections.cmd_fetch(_fetch_args(partition="ak"), io.StringIO()) == doccollections.EXIT_REFUSED
    # A paused source is not fetched (army-pubs-public's robots.txt disallows every crawler, 2026-10-21).
    import dataclasses

    from wsp_core.model import SourceStatus

    def paused(*a: object) -> list[SourceSpec]:
        return [dataclasses.replace(spec(OBJECTS), status=SourceStatus.PAUSED)]

    monkeypatch.setattr(bootstrap, "load_sources", paused)
    assert doccollections.cmd_fetch(_fetch_args(), io.StringIO()) == doccollections.EXIT_REFUSED


class _Paths:
    def __init__(self, vault_root: Path, workdir: Path) -> None:
        self.vault_root = vault_root
        self.workdir = workdir
        self.proposals = workdir / "proposals"
        self.state_dir = None  # no ingest slots: the fetch paces alone

    def require_vault(self) -> Path:
        return self.vault_root


def _unthrottled(init: Any) -> Any:
    def wrapper(self: CollectionFetcher, *args: Any, **kw: Any) -> None:
        init(self, *args, **{**kw, "throttle": False})

    return wrapper


# --- H3 and C1: `wsp ingest --version` answers for that version alone ------------------------------------


def _plan_missing_file(vault: FileVault, version: str) -> None:
    """A second version of the source whose download is still missing a file (INCOMPLETE)."""
    plans = vault.root / "_incoming" / "_plans" / "targets.d"
    plans.mkdir(parents=True, exist_ok=True)
    target: dict[str, object] = {
        "source_id": SID,
        "version": version,
        "name": "missing.pdf",
        "uris": ["https://bucket.example.gov/pubs/missing.pdf"],
        "lane": "collections",
        "checksum": ["sha-256", "0" * 64],
        "size": 10,
        "meta": {},
    }
    (plans / f"{SID}-{version}.json").write_text(json.dumps([target]))


def test_ingest_of_one_version_ignores_a_stuck_one(vault: FileVault, tmp_path: Path) -> None:
    # (A fetch waits while another version is pending, so the stuck plan comes after this cycle's fetch.)
    fetch(vault, tmp_path, OBJECTS, "2026-10-12")
    _plan_missing_file(vault, "2026-10-01")
    assert ingest(vault, "2026-10-12") == [("2026-10-12", VersionStatus.INGESTED, True)]
    assert ingest(vault, "2026-10-01") == [("2026-10-01", VersionStatus.INCOMPLETE, False)]
    assert ingest(vault, "2026-10-30") == []  # nothing planned for that cycle


def _ingest_args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "config_dir": None,
        "source": SID,
        "version": None,
        "partition": None,
        "dry_run": True,
        "offline": True,
        "allow_mirror": False,
        "settle_seconds": 0.0,
        "json": False,
    }
    return argparse.Namespace(**{**base, **kw})


def test_ingest_exit_codes_with_a_version(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch(vault, tmp_path, OBJECTS, "2026-10-12")
    _plan_missing_file(vault, "2026-10-01")

    def same_vault(*a: object, **kw: object) -> FileVault:
        return vault

    monkeypatch.setattr(ops, "_vault", same_vault)
    specs = (spec(OBJECTS),)

    def run(**kw: Any) -> int:
        return ops._ingest(_ingest_args(**kw), io.StringIO(), None, {}, specs)  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage]

    assert run() == ops.EXIT_INCOMPLETE  # the whole source: the stuck version decides
    assert run(version="2026-10-12") == 0  # this cycle alone
    assert run(version="2026-10-01") == ops.EXIT_INCOMPLETE
    assert run(version="2026-10-30") == ops.EXIT_NOT_PLANNED == 8
    assert run(source=None, version="2026-10-12") == 2
    assert run(partition="ak") == 2  # --partition needs --version
    assert run(version="2026-10-12", partition="ak") == 2  # not a partitioned source


def test_ingest_version_names_a_partition_like_the_fetch() -> None:
    import dataclasses

    raw = spec(OBJECTS).raw
    access = cast(dict[str, Any], raw["access"])
    part = dataclasses.replace(spec(OBJECTS), raw={**raw, "access": {**access, "partitioned": True}})
    assert part.partitioned
    resolve = ops._ingest_version  # pyright: ignore[reportPrivateUsage]
    assert resolve(_ingest_args(version="2026-10-12", partition="AK"), [part]) == "2026-10-12.ak"
    assert resolve(_ingest_args(version="2026-10-12.ak"), [part]) == "2026-10-12.ak"
    assert resolve(_ingest_args(version="2026-10-12"), [part]) == 2  # which partition?


# --- H2: a re-fetch after an interrupted ingest ------------------------------------------------------------


def test_a_refetch_after_an_interrupted_ingest_is_ingested(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch(vault, tmp_path, OBJECTS, "2026-10-12")
    real_seal = vault.seal

    def reboot(*a: Any, **kw: Any) -> Any:
        raise OSError("the machine rebooted before the seal")

    monkeypatch.setattr(vault, "seal", reboot)
    assert ingest(vault, "2026-10-12")[0][1] is VersionStatus.FAILED
    monkeypatch.setattr(vault, "seal", real_seal)
    later = datetime.now(UTC) + timedelta(hours=4)

    class _Later(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return later

    monkeypatch.setattr(doccollections, "datetime", _Later)
    again = fetch(vault, tmp_path, OBJECTS, "2026-10-12")  # the lane fetches the same cycle again
    assert again.fetched
    # Before: "meta/documents.jsonl is already staged with different content" on every run, forever.
    assert ingest(vault, "2026-10-12") == [("2026-10-12", VersionStatus.INGESTED, True)]


def test_an_ingest_stopped_while_hashing_a_file_finishes_it(vault: FileVault, tmp_path: Path) -> None:
    fetch(vault, tmp_path, OBJECTS, "2026-10-12")
    plan = Ingestor(vault, [spec(OBJECTS)], out=io.StringIO()).plan(SID, "2026-10-12")
    first = plan.versions[0].files[0]
    # Killed after the vault moved the file aside into staging, before it was hashed and published.
    moving = vault.stage(SID, "2026-10-12") / f"{first.target.relpath}.adopting"
    moving.parent.mkdir(parents=True, exist_ok=True)
    first.path.rename(moving)
    # Before: "1 of N file(s) not complete" on every run (ERA5 2025, 2026-10-21).
    assert ingest(vault, "2026-10-12") == [("2026-10-12", VersionStatus.INGESTED, True)]
    assert not moving.exists()


def test_a_host_that_takes_no_connection_is_left_for_the_next_run(vault: FileVault, tmp_path: Path) -> None:
    from wsp_cli.doccollections import HostBreaker
    from wsp_core.errors import FetchError

    objects = [(f"FM-{i}_2022.pdf", GOOD) for i in range(8)]

    class Refused(FakeFetcher):
        tries = 0

        def download(self, remote: Any, dest: Path) -> Any:
            Refused.tries += 1
            raise FetchError(f"probe {remote.url} failed after 6 attempt(s): ConnectTimeout: timed out")

    runner = CollectionFetcher(
        vault.root,
        Refused(routes(objects)),
        keys={},
        user_agent="t",
        workdir=tmp_path / "w",
        out=io.StringIO(),
        throttle=False,
    )
    report = runner.run(spec(objects), version="2026-10-21")
    # Before: every document waited out its own connect timeouts (Borden Institute: hours a run).
    assert Refused.tries == HostBreaker.AFTER
    assert len(report.failed) == len(objects)
    assert sum("HostUnreachable" in why for _, why in report.failed) == len(objects) - HostBreaker.AFTER
    assert fetch_exit_code(report) == EXIT_PARTIAL


def test_a_host_that_answered_is_never_skipped() -> None:
    from wsp_cli.doccollections import HostBreaker

    hosts = HostBreaker()
    for _ in range(HostBreaker.AFTER):
        hosts.failed("https://a.example/x", "FetchError: not found (HTTP 404)")  # it answers
        hosts.failed("https://b.example/x", "probe failed: ConnectError: refused")
    assert hosts.down("https://a.example/y") is None
    assert hosts.down("https://b.example/y") is not None
    hosts.answered("https://b.example/z")
    assert hosts.down("https://b.example/y") is None


def test_an_ingest_out_of_time_yields_and_the_next_run_goes_on(vault: FileVault, tmp_path: Path) -> None:
    fetch(vault, tmp_path, OBJECTS, "2026-10-12")
    out = io.StringIO()
    report = Ingestor(vault, [spec(OBJECTS)], out=out, max_seconds=0).run(source_id=SID, version="2026-10-12")
    # 22,000 CRS reports held the ingest lock for hours (2026-10-21): an ingest gives it up in time.
    assert [v.status for v in report.versions] == [VersionStatus.YIELDED]
    assert "yielded after 0 s" in out.getvalue()
    assert vault.current_set_ids(SID) == []
    assert ingest(vault, "2026-10-12") == [("2026-10-12", VersionStatus.INGESTED, True)]


# --- M3: natural order in `wsp ingest` --------------------------------------------------------------------


def test_ingest_promotes_r10_over_r4(vault: FileVault, tmp_path: Path) -> None:
    fetch(vault, tmp_path, OBJECTS, "2026-10-12-r4")
    assert ingest(vault, "2026-10-12-r4") == [("2026-10-12-r4", VersionStatus.INGESTED, True)]
    more = [*OBJECTS, ("FM-6-0_2022.pdf", GOOD)]
    fetch(vault, tmp_path, more, "2026-10-12-r10")
    assert ingest(vault, "2026-10-12-r10") == [("2026-10-12-r10", VersionStatus.INGESTED, True)]
    assert vault.current_set_ids(SID) == ["2026-10-12-r10"]


# --- M4: fetch locks shared by every process ----------------------------------------------------------------


def test_fetch_locks_live_in_the_machine_lock_directory(tmp_path: Path) -> None:
    cfg = {"index": {"root": str(tmp_path / "index")}}
    fetcher = bootstrap.build_fetcher(cfg)
    try:
        assert fetcher.config.lock_dir == str(lock_dir_for(tmp_path / "index") / "fetch")  # type: ignore[attr-defined]
    finally:
        fetcher.close()  # type: ignore[attr-defined]
    explicit = bootstrap.build_fetcher({**cfg, "acquirer": {"lock_dir": str(tmp_path / "mine")}})
    try:
        assert explicit.config.lock_dir == str(tmp_path / "mine")  # type: ignore[attr-defined]
    finally:
        explicit.close()  # type: ignore[attr-defined]


# --- L1: a busy heavy step costs no registry or NAS reads ---------------------------------------------------


@pytest.mark.parametrize("child", [wiring.refine_child, wiring.index_child])
def test_a_busy_step_does_not_build_its_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child: Any
) -> None:
    def no_context(payload: Any) -> Any:
        raise AssertionError("the registry and the vault were read before the lock")

    monkeypatch.setattr(wiring, "child_context", no_context)
    cfg = {"index": {"root": str(tmp_path / "index")}}
    with exclusive(lock_dir_for(tmp_path / "index"), HEAVY), pytest.raises(LockHeldError):
        child({"config": cfg, "source_id": SID, "generation_id": "2026-10-12"})


def test_the_cli_documents_the_exit_code_contract() -> None:
    from wsp_cli.main import build_parser

    parser = build_parser()
    args = parser.parse_args(["ingest", "--source", SID, "--version", "2026-10-12", "--partition", "ak"])
    assert (args.version, args.partition) == ("2026-10-12", "ak")
    sub = next(a for a in parser._actions if a.dest == "command")  # pyright: ignore[reportPrivateUsage]
    choices: dict[str, Any] = sub.choices  # type: ignore[assignment]
    assert "8 (--version only) no download plan" in choices["ingest"].description
    fetch_help = choices["collections"]._subparsers._group_actions[0].choices["fetch"].description  # pyright: ignore[reportPrivateUsage]
    for code in (
        "0 complete",
        "2 refused",
        "4 listing incomplete",
        "6 partial",
        "7 complete with alerts",
        "8 complete",
    ):
        assert code in fetch_help
