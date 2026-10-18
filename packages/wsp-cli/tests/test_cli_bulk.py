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
        self.downloaded: list[str] = []

    def download(self, remote: RemoteFile, dest: Path) -> Any:
        if remote.name in self.fail:
            from wsp_core.errors import FetchError

            raise FetchError(f"{remote.url}: reset")
        self.downloaded.append(remote.name)
        dest.write_bytes(self.routes[remote.url])


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Downloads]:
    root = tmp_path / "vault"
    root.mkdir()
    fetcher = Downloads({f.url: d for f, d in zip(FILES, (A, B), strict=True)})
    cfg = {"vault": {"root": str(root), "ingest_bytes_per_sec": 0}, "index": {"root": str(tmp_path / "i")}}

    def returning(value: object) -> Any:
        def stub(*args: object, **kwargs: object) -> object:
            return value

        return stub

    monkeypatch.setattr(bootstrap, "layout", returning(None))
    monkeypatch.setattr(bootstrap, "load", returning(cfg))
    monkeypatch.setattr(bootstrap, "load_sources", returning([SPEC]))
    monkeypatch.setattr(bootstrap, "load_adapters", returning({"big": Adapter()}))
    monkeypatch.setattr(bootstrap, "build_fetcher", returning(fetcher))
    return root, fetcher


def run(only: list[str] | None = None) -> tuple[int, str]:
    out = io.StringIO()
    rc = bulk.cmd_fetch(argparse.Namespace(config_dir=None, source="big", only=only or []), out)
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
