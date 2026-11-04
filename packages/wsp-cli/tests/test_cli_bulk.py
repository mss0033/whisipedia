"""``wsp bulk fetch``: a release larger than the local disk, fetched straight into _incoming."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from wsp_cli import bootstrap, bulk, ops
from wsp_cli.ingest import load_targets
from wsp_core import diag
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
        self.missing: set[str] = set()
        self.downloaded: list[str] = []

    def download(self, remote: RemoteFile, dest: Path) -> Any:
        if remote.name in self.fail:
            from wsp_core.errors import FetchError

            raise FetchError(f"{remote.url}: reset")
        if remote.name in self.missing:
            from wsp_core.errors import NotFoundError

            raise NotFoundError(remote.url, "HTTP 404")
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


class Two(Adapter):
    """A partition of both files."""

    def discover_partition(self, ctx: Ctx, partition: str) -> list[ReleaseRef]:
        return [ReleaseRef("big", "2026-10-18.p0001", None, FILES, format="tar", license="CC-BY-4.0")]


class PackedTwo(Two, Packed):
    pass


def test_a_file_the_issuer_no_longer_has_is_set_aside_and_the_partition_sealed(
    vault: tuple[Path, Downloads], monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-30: one withdrawn PMC article (HTTP 404) failed its 85,000-file partition on every pass;
    the lane went back over the same seven partitions for days. Gone files are set aside and named."""
    root, fetcher = vault

    def adapters(*_: object, **__: object) -> dict[str, object]:
        return {"big": Two()}

    monkeypatch.setattr(bootstrap, "load_adapters", adapters)
    fetcher.missing = {"b.tar.gz"}
    monkeypatch.setattr(bulk, "GONE_MAX_MIN", 0)  # one gone of two is past the share: fails as before
    rc, text = run(partition="p0001")
    assert rc == bulk.EXIT_FAILED, text
    assert "too many to be withdrawals" in text
    monkeypatch.setattr(bulk, "GONE_MAX_MIN", 10)
    rc, text = run(partition="p0001")
    assert rc == bulk.EXIT_OK, text
    assert "no longer has" in text
    plan = json.loads((root / "_incoming/_plans/targets.d/big-2026-10-18.p0001.json").read_text())
    assert [t["name"] for t in plan] == ["a.tar.gz"]  # the plan names only what was had
    fetcher.missing = {"a.tar.gz", "b.tar.gz"}  # everything gone: nothing to seal, a failure
    (root / "_incoming" / "big" / "2026-10-18.p0001" / "a.tar.gz").unlink()
    rc, text = run(partition="p0001")
    assert rc == bulk.EXIT_FAILED, text


def test_a_packed_partition_without_a_gone_file_names_it_in_its_zip(
    vault: tuple[Path, Downloads], monkeypatch: pytest.MonkeyPatch
) -> None:
    import zipfile

    root, fetcher = vault

    def adapters(*_: object, **__: object) -> dict[str, object]:
        return {"big": PackedTwo()}

    monkeypatch.setattr(bootstrap, "load_adapters", adapters)
    fetcher.missing = {"b.tar.gz"}
    rc, text = run(partition="p0001")
    assert rc == bulk.EXIT_OK, text
    zipped = root / "_incoming" / "big" / "2026-10-18.p0001" / "big-2026-10-18.p0001.zip"
    with zipfile.ZipFile(zipped) as z:
        assert sorted(z.namelist()) == ["GONE.txt", "a.tar.gz"]
        assert z.read("GONE.txt").decode().startswith("b.tar.gz\t")


def test_files_an_older_version_left_staged_are_kept_not_fetched_again(
    vault: tuple[Path, Downloads], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-01: PMC's listing day changed every day or two, each change began an 85,000-file
    partition again from nothing, and the old staging stayed (seven copies of p0272, 60 GB)."""
    root, fetcher = vault

    def adapters(*_: object, **__: object) -> dict[str, object]:
        return {"big": PackedTwo()}

    monkeypatch.setattr(bootstrap, "load_adapters", adapters)
    pack = tmp_path / "state" / "bulk" / "big" / "pack"
    older = pack / "2026-10-17.p0001"
    older.mkdir(parents=True)
    (older / "a.tar.gz").write_bytes(A)  # whole: kept
    (older / "b.tar.gz").write_bytes(b"cut")  # short of its size: fetched again
    (older / "dropped.txt").write_text("not in the newer listing")
    newer, other = pack / "2026-10-19.p0001", pack / "2026-10-17.p0002"
    for folder in (newer, other):
        folder.mkdir()
        (folder / "a.tar.gz").write_bytes(A)
    rc, text = run(partition="p0001")
    assert rc == bulk.EXIT_OK, text
    assert "1 file(s) an older version left staged kept" in text
    assert fetcher.downloaded == ["b.tar.gz"]
    assert not older.exists()  # removed with what the newer listing dropped
    assert (newer / "a.tar.gz").is_file()  # not this one's to touch
    assert (other / "a.tar.gz").is_file()
    with zipfile.ZipFile(root / "_incoming/big/2026-10-18.p0001/big-2026-10-18.p0001.zip") as z:
        assert z.read("a.tar.gz") == A
        assert z.read("b.tar.gz") == B


def test_a_zip_copy_the_share_refuses_fails_the_run_cleanly_and_a_rerun_writes_it(
    vault: tuple[Path, Downloads], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-02: the NAS refused past the copy's retries and the fetch died with a traceback (exit 1)."""
    root, fetcher = vault

    def adapters(*_: object, **__: object) -> dict[str, object]:
        return {"big": Packed()}

    from wsp_cli import nasretry

    refusing = [True]

    def refuse(src: Path, dest: Path, pace: object) -> str:
        if refusing[0]:
            raise BlockingIOError(11, "Resource temporarily unavailable")
        return nasretry.copy_hashed(src, dest, None)

    monkeypatch.setattr(bootstrap, "load_adapters", adapters)
    monkeypatch.setattr(bulk, "copy_hashed", refuse)
    rc, text = run(partition="p0001")
    assert rc == bulk.EXIT_FAILED, text
    assert "not written" in text
    assert (tmp_path / "state/bulk/big/pack/2026-10-18.p0001.zip").is_file()  # kept for the rerun
    refusing[0] = False
    rc, text = run(partition="p0001")
    assert rc == bulk.EXIT_OK, text
    assert fetcher.downloaded == ["a.tar.gz"]  # fetched once
    assert (root / "_incoming/big/2026-10-18.p0001/big-2026-10-18.p0001.zip").is_file()


def test_a_loose_file_that_cannot_be_moved_is_fetched_instead(
    vault: tuple[Path, Downloads], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving a loose file off the NAS failed with EAGAIN (2026-10-19): the run fetches it instead."""
    import shutil

    root, fetcher = vault

    def adapters(*_: object, **__: object) -> dict[str, object]:
        return {"big": Packed()}

    def refuse(*_: object, **__: object) -> None:
        raise BlockingIOError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(bootstrap, "load_adapters", adapters)
    monkeypatch.setattr(shutil, "copyfile", refuse)
    loose = root / "_incoming" / "big" / "2026-10-17.p0001"
    loose.mkdir(parents=True)
    (loose / "a.tar.gz").write_bytes(A)
    rc, text = run(partition="p0001")
    assert rc == bulk.EXIT_OK, text
    assert fetcher.downloaded == ["a.tar.gz"]


def test_an_ingest_run_log_times_each_version_and_its_seal(
    vault: tuple[Path, Downloads], tmp_path: Path
) -> None:
    assert run()[0] == bulk.EXIT_OK
    args = argparse.Namespace(
        config_dir=None,
        source="big",
        version=None,
        partition=None,
        dry_run=False,
        offline=True,
        json=False,
        settle_seconds=0,
        allow_mirror=False,
        max_seconds=None,
    )
    log = diag.start_run(tmp_path / "state", "ingest", ["ingest"])
    rc = ops.cmd_ingest(args, io.StringIO())
    diag.end_run(log, rc)
    assert rc == 0
    rows = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    ends = {r["phase"]: r for r in rows if r.get("event") == "phase-end"}
    assert set(ends) == {"ingest-version", "seal"}
    assert (ends["ingest-version"]["source"], ends["ingest-version"]["status"]) == ("big", "ingested")
    assert ends["ingest-version"]["files"] == 2
    assert ends["ingest-version"]["bytes"] == len(A) + len(B)
    assert ends["seal"]["seconds"] <= ends["ingest-version"]["seconds"]
    # The progress lines are in the log too.
    assert any(r.get("logger") == "wsp.ingest.progress" and "adopting 2 file(s)" in r["msg"] for r in rows)


def test_a_file_without_size_or_checksum_must_read_to_count_as_whole(tmp_path: Path) -> None:
    """2026-10-26 review: a PMC article file cut short (no size or checksum published) counted as whole
    because it was there, and was packed and sealed under its own checksum."""
    from wsp_core.model import RemoteFile

    def remote(name: str) -> RemoteFile:
        return RemoteFile(f"https://x/{name}", name, None)

    good, cut, empty, text, big = (tmp_path / n for n in ("a.json", "b.json", "c.txt", "d.txt", "e.tar"))
    good.write_text('{"title": "An article"}')
    cut.write_text('{"title": "An art')
    empty.write_bytes(b"")
    text.write_text("Body text.", encoding="utf-8")
    big.write_bytes(b"x")
    whole = bulk._whole  # pyright: ignore[reportPrivateUsage]
    assert whole(good, remote("a.json"))
    assert not whole(cut, remote("b.json"))
    assert not whole(empty, remote("c.txt"))
    assert whole(text, remote("d.txt"))
    assert whole(big, remote("e.tar"))  # other kinds: not read whole, only not empty


def test_files_an_interrupted_ingest_staged_are_not_fetched_again(vault: tuple[Path, Downloads]) -> None:
    """2026-11-04 trawl: a fetch looked only in _incoming, so every file an ingest that stopped before
    its seal had adopted was downloaded again (NARA's 401 files, 2026-10-28), then read back twice
    over the NAS and dropped as a duplicate."""
    from dataclasses import replace

    from wsp_cli.ingest import Ingestor, VersionStatus
    from wsp_vault.signing import Ed25519Signer
    from wsp_vault.vault import FileVault

    root, fetcher = vault
    assert run()[0] == bulk.EXIT_OK
    incoming = root / "_incoming/big/2025-03-26"
    lib = FileVault(root, Ed25519Signer.generate(), domains={"big": SPEC.vault_domain})
    staging = lib.stage("big", "2025-03-26")
    # a.tar.gz adopted and recorded; b.tar.gz moved aside by an ingest stopped while hashing it.
    lib.adopt("big", "2025-03-26", incoming / "a.tar.gz", "upstream/a.tar.gz", upstream=FILES[0].checksums)
    (incoming / "b.tar.gz").rename(staging / "upstream/b.tar.gz.adopting")
    fetcher.downloaded.clear()
    rc, text = run()
    assert rc == bulk.EXIT_OK, text
    assert fetcher.downloaded == []
    assert "2 file(s) already here or staged" in text
    # Not the planned size, or a checksum it was never checked against: fetched again.
    staged = bulk._staged(staging)  # pyright: ignore[reportPrivateUsage]
    assert staged(FILES[0])
    assert not staged(replace(FILES[0], size=len(A) + 1))
    assert not staged(replace(FILES[0], checksums=(Checksum("sha256", "0" * 64),)))
    # The ingest goes on from what is staged.
    report = Ingestor(lib, [SPEC], out=io.StringIO()).run(source_id="big")
    assert [v.status for v in report.versions] == [VersionStatus.INGESTED]
