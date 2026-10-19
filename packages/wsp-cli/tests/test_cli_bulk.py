"""``wsp bulk fetch``: a release larger than the local disk, fetched straight into _incoming."""

from __future__ import annotations

import argparse
import hashlib
import io
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from wsp_cli import bootstrap, bulk
from wsp_cli.ingest import load_targets
from wsp_core.model import Checksum, ReleaseRef, RemoteFile, SourceSpec, SourceStatus, Tier
from wsp_core.ports import Ctx
from wsp_core.testing.adapter_contract import FakeFetcher

A, B = b"year one", b"year two!"
FILES = (
    RemoteFile(
        "https://d.example/a.tar.gz", "a.tar.gz", len(A), (Checksum("sha256", hashlib.sha256(A).hexdigest()),)
    ),
    RemoteFile(
        "https://d.example/b.tar.gz", "b.tar.gz", len(B), (Checksum("md5", hashlib.md5(B).hexdigest()),)
    ),
)
RELEASE = ReleaseRef("big", "2025-03-26", None, FILES, format="tar", license="CC-BY-4.0")


class Adapter:
    id = "big"

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]:
        return [RELEASE]

    def discover_partition(self, ctx: Ctx, partition: str) -> list[ReleaseRef]:
        self.workdir = ctx.workdir
        if partition != "p0001":
            return []
        return [ReleaseRef("big", "2026-10-18.p0001", None, FILES[:1], format="tar", license="CC-BY-4.0")]


ADAPTER = Adapter()

SPEC = SourceSpec(
    id="big",
    name="big",
    tier=Tier.T2,
    status=SourceStatus.CANDIDATE,
    license="CC-BY-4.0",
    legal_basis="open-license",
    domain=("news",),
    adapter="big",
    raw={"id": "big", "access": {"adapter": "big"}},
)


class Downloads(FakeFetcher):
    def __init__(self, routes: dict[str, bytes], fail: set[str] | None = None) -> None:
        super().__init__(routes)
        self.fail = fail or set()
        self.write_fail: set[str] = set()
        self.downloaded: list[str] = []

    def download(self, remote: RemoteFile, dest: Path) -> Any:
        if remote.name in self.fail:
            from wsp_core.errors import FetchError

            raise FetchError(f"{remote.url}: reset")
        if remote.name in self.write_fail:
            raise OSError(14, "Bad address")
        self.downloaded.append(remote.name)
        dest.write_bytes(self.routes[remote.url])


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Downloads]:
    root = tmp_path / "vault"
    root.mkdir()
    fetcher = Downloads({f.url: d for f, d in zip(FILES, (A, B), strict=True)})
    cfg = {
        "vault": {"root": str(root), "ingest_bytes_per_sec": 0},
        "index": {"root": str(tmp_path / "i")},
        "orchestrator": {"state_db": str(tmp_path / "state" / "orchestrator.sqlite3")},
    }

    def returning(value: object) -> Any:
        def stub(*args: object, **kwargs: object) -> object:
            return value

        return stub

    monkeypatch.setattr(bootstrap, "layout", returning(None))
    monkeypatch.setattr(bootstrap, "load", returning(cfg))
    monkeypatch.setattr(bootstrap, "load_sources", returning([SPEC]))
    monkeypatch.setattr(bootstrap, "load_adapters", returning({"big": ADAPTER}))
    monkeypatch.setattr(bootstrap, "build_fetcher", returning(fetcher))
    return root, fetcher


def run(only: list[str] | None = None, partition: str | None = None, parallel: int = 1) -> tuple[int, str]:
    out = io.StringIO()
    args = argparse.Namespace(
        config_dir=None, source="big", only=only or [], partition=partition, parallel=parallel
    )
    rc = bulk.cmd_fetch(args, out)
    return rc, out.getvalue()


def test_the_release_lands_in_incoming_with_a_plan_ingest_reads(vault: tuple[Path, Downloads]) -> None:
    root, fetcher = vault
    rc, text = run()
    assert rc == bulk.EXIT_OK, text
    assert (root / "_incoming/big/2025-03-26/a.tar.gz").read_bytes() == A
    assert "next: wsp ingest --source big --version 2025-03-26" in text
    targets = load_targets(root / "_incoming/_plans/targets.d/big-2025-03-26.json")
    assert [(t.name, t.checksum, t.size) for t in targets] == [
        ("a.tar.gz", Checksum("sha256", hashlib.sha256(A).hexdigest()), len(A)),
        ("b.tar.gz", Checksum("md5", hashlib.md5(B).hexdigest()), len(B)),
    ]
    # A rerun fetches nothing that is already whole.
    fetcher.downloaded.clear()
    assert run()[0] == bulk.EXIT_OK
    assert fetcher.downloaded == []


def test_a_failed_file_fails_the_run_and_a_rerun_continues(vault: tuple[Path, Downloads]) -> None:
    _, fetcher = vault
    fetcher.fail = {"a.tar.gz"}
    rc, text = run()
    assert rc == bulk.EXIT_FAILED
    assert "a.tar.gz: FAILED" in text
    assert fetcher.downloaded == ["b.tar.gz"]
    fetcher.fail = set()
    fetcher.downloaded.clear()
    assert run()[0] == bulk.EXIT_OK
    assert fetcher.downloaded == ["a.tar.gz"]


def test_a_failed_write_fails_that_file_not_the_run(vault: tuple[Path, Downloads]) -> None:
    _, fetcher = vault
    fetcher.write_fail = {"a.tar.gz"}
    rc, text = run()
    assert rc == bulk.EXIT_FAILED
    assert "a.tar.gz: FAILED" in text
    assert fetcher.downloaded == ["b.tar.gz"]


def test_only_fetches_the_matching_files(vault: tuple[Path, Downloads]) -> None:
    root, fetcher = vault
    assert run(["b.*"])[0] == bulk.EXIT_OK
    assert fetcher.downloaded == ["b.tar.gz"]
    # The plan still names every file: ingest waits until all of them are there.
    assert len(load_targets(root / "_incoming/_plans/targets.d/big-2025-03-26.json")) == 2


def test_a_sealed_release_is_not_fetched_again(vault: tuple[Path, Downloads]) -> None:
    root, fetcher = vault
    sealed = root / "library" / SPEC.vault_domain / "big" / "2025-03-26"
    sealed.mkdir(parents=True)
    (sealed / "MANIFEST.json").write_text("{}")
    assert run()[0] == bulk.EXIT_NOTHING_NEW
    assert fetcher.downloaded == []


def test_one_partition_in_parallel_with_a_kept_workdir(vault: tuple[Path, Downloads], tmp_path: Path) -> None:
    root, fetcher = vault
    rc, text = run(partition="p0001", parallel=4)
    assert rc == bulk.EXIT_OK, text
    assert (root / "_incoming/big/2026-10-18.p0001/a.tar.gz").read_bytes() == A
    assert fetcher.downloaded == ["a.tar.gz"]
    assert "next: wsp ingest --source big --version 2026-10-18.p0001" in text
    assert ADAPTER.workdir == tmp_path / "state" / "bulk" / "big"  # kept between runs (PMC's inventory)
    assert run(partition="p0099")[0] == bulk.EXIT_NOTHING_NEW  # an empty partition


class Packed(Adapter):
    """A partition of many small files stored as one zip (PubMed Central, 2026-10-19)."""

    def pack_name(self, version: str) -> str:
        return f"big-{version}.zip"


def test_a_packed_partition_lands_in_incoming_as_one_zip(
    vault: tuple[Path, Downloads], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """45,000 files a partition written one by one into the NAS starved every read of the library;
    they are fetched into the local disk and written into _incoming as one zip."""
    import zipfile

    root, fetcher = vault

    def adapters(*_: object, **__: object) -> dict[str, object]:
        return {"big": Packed()}

    monkeypatch.setattr(bootstrap, "load_adapters", adapters)
    # An earlier, unpacked run left one file loose under the partition's older version.
    loose = root / "_incoming" / "big" / "2026-10-17.p0001"
    loose.mkdir(parents=True)
    (loose / "a.tar.gz").write_bytes(A)
    old_plan = root / "_incoming/_plans/targets.d/big-2026-10-17.p0001.json"
    old_plan.parent.mkdir(parents=True)
    old_plan.write_text("[]")
    rc, text = run(partition="p0001", parallel=4)
    assert rc == bulk.EXIT_OK, text
    assert fetcher.downloaded == []  # the loose file was staged, not fetched again
    assert not loose.exists()  # and its emptied folder removed, with its plan
    assert not old_plan.exists()
    version = root / "_incoming" / "big" / "2026-10-18.p0001"
    assert [p.name for p in version.iterdir()] == ["big-2026-10-18.p0001.zip"]
    with zipfile.ZipFile(version / "big-2026-10-18.p0001.zip") as z:
        assert z.read("a.tar.gz") == A
    (target,) = load_targets(root / "_incoming/_plans/targets.d/big-2026-10-18.p0001.json")
    data = (version / target.name).read_bytes()
    assert target.checksum == Checksum("sha256", hashlib.sha256(data).hexdigest())
    assert target.size == len(data)
    assert not (tmp_path / "state" / "bulk" / "big" / "pack" / "2026-10-18.p0001").exists()  # cleaned
    rc, text = run(partition="p0001")  # a rerun before the ingest: nothing fetched or written again
    assert rc == bulk.EXIT_OK
    assert "already written" in text
    assert fetcher.downloaded == []
