"""``wsp collections`` (fetch + dry run) and ``wsp ingest`` of a document-collection generation."""

from __future__ import annotations

import dataclasses
import io
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from wsp_adapters.doccollection import DocCollectionAdapter
from wsp_adapters.doccollection.adapter import CATALOG, screen_text
from wsp_adapters.doccollection.catalog import read_catalog_file
from wsp_adapters.doccollection.model import Candidate
from wsp_adapters.doccollection.policy import Rules
from wsp_adapters.doccollection.testing import PUBLIC_A, RESTRICTED_C, make_pdf, s3_listing
from wsp_cli.doccollections import (
    CollectionFetcher,
    apply_review_decision,
    collection_spec,
    dry_run,
    github_repo,
    registry_repos,
    render_dry_run,
)
from wsp_cli.ingest import Ingestor, VersionStatus
from wsp_core.model import FetchResult, RemoteFile, SourceSpec, SourceStatus, Tier
from wsp_core.ports import Ctx
from wsp_core.testing.adapter_contract import FakeFetcher
from wsp_vault.ledger import JsonlLedger
from wsp_vault.signing import Ed25519Signer
from wsp_vault.vault import FileVault

BUCKET = "https://bucket.example.gov"
PREFIX = "pubs/"
GOOD = make_pdf([PUBLIC_A, ["Chapter 1"]])
RESTRICTED = make_pdf([RESTRICTED_C])
UNMARKED = make_pdf([["PLANNING HANDBOOK", "Contents"]])


def spec(objects: list[tuple[str, bytes]]) -> SourceSpec:
    del objects
    return SourceSpec(
        id="test-pubs",
        name="Test publications",
        tier=Tier.T1,
        status=SourceStatus.CANDIDATE,
        license="LicenseRef-US-Gov-PD",
        legal_basis="public-domain",
        domain=("preparedness",),
        adapter="doc-collection",
        raw={
            "id": "test-pubs",
            "access": {
                "adapter": "doc-collection",
                "issuer": "Test Agency",
                "key_prefix": "test",
                "origins": ["bucket.example.gov"],
                "discovery": [{"kind": "s3-listing", "bucket_url": BUCKET, "prefix": PREFIX}],
                "filter": {"allow_all": True, "distribution": {"require": ["A"]}},
                "currency": {"historical_before": "1990-01-01"},
            },
        },
    )


def routes(objects: list[tuple[str, bytes]]) -> dict[str, bytes]:
    out = {f"{BUCKET}/": s3_listing(PREFIX, objects)}
    out.update({f"{BUCKET}/{PREFIX}{k}": v for k, v in objects})
    return out


OBJECTS = [("FM-5-0_2022.pdf", GOOD), ("restricted.pdf", RESTRICTED), ("unmarked.pdf", UNMARKED)]


@pytest.fixture
def vault(tmp_path: Path) -> FileVault:
    signer = Ed25519Signer.generate()
    root = tmp_path / "vault"
    return FileVault(
        root, signer, domains={"test-pubs": "preparedness"}, ledger=JsonlLedger(root / "ledger", signer)
    )


def runner(vault: FileVault, objects: list[tuple[str, bytes]], tmp_path: Path) -> CollectionFetcher:
    return CollectionFetcher(
        vault.root,
        FakeFetcher(routes(objects)),
        keys={},
        user_agent="test",
        workdir=tmp_path / "work",
        out=io.StringIO(),
        throttle=False,
    )


def test_fetch_screens_quarantines_and_ingest_seals(vault: FileVault, tmp_path: Path) -> None:
    s = spec(OBJECTS)
    report = runner(vault, OBJECTS, tmp_path).run(s, version="2026-10-10")
    assert [d.cand.title for d in report.fetched] == ["FM 5 0 2022"]
    assert report.rejected == [("test:restricted.pdf", "deny:marking=distribution-C")]
    assert report.alerts
    assert "restricted.pdf" in report.alerts[0]
    assert len(report.held) == 1
    assert report.held[0]["rule"] == "review:marking=missing-A"
    incoming = vault.root / "_incoming"
    assert Path(report.held[0]["quarantined"]).is_file()
    assert not list((incoming / "test-pubs" / "2026-10-10").glob("*restricted*"))  # deleted, not kept
    plan = json.loads((incoming / "_plans" / "targets.d" / "test-pubs-2026-10-10.json").read_text())
    assert [t["meta"].get("relpath") for t in plan] == [None, CATALOG]
    catalog = read_catalog_file(incoming / "test-pubs" / "2026-10-10" / CATALOG)
    assert [m.distribution for m in catalog] == ["A"]
    assert "Approved for public release" in catalog[0].distribution_text

    # Default settle time: ingesting right after fetching must work (fetch renames only whole files;
    # a lane that ran ingest at once used to see "modified moments ago" and ingest nothing).
    ingest = Ingestor(vault, [s], out=io.StringIO())
    result = ingest.run(source_id="test-pubs")
    (vp,) = result.versions
    assert vp.status is VersionStatus.INGESTED
    assert vp.promoted
    gen_dir = vault.generation_path("test-pubs", "2026-10-10")
    assert (gen_dir / CATALOG).is_file()
    ctx = Ctx(source=s, fetcher=FakeFetcher(), workdir=tmp_path / "w2", user_agent="t")
    (doc,) = list(DocCollectionAdapter(throttle=False).documents(ctx, gen_dir))
    assert doc.meta["distribution"] == "A"
    assert doc.native_id == "test:FM-5-0_2022.pdf"

    again = runner(vault, OBJECTS, tmp_path).run(s, version="2026-11-10")
    assert again.fetched == []
    assert again.skipped == 1
    assert not (incoming / "_plans" / "targets.d" / "test-pubs-2026-11-10.json").exists()


def test_dry_run_counts_and_renders(tmp_path: Path) -> None:
    s = spec(OBJECTS)
    results = dry_run(
        [s],
        FakeFetcher(routes(OBJECTS)),
        keys={},
        workdir=tmp_path,
        user_agent="t",
        max_pages=1,
        resolve_sample=0,
        head_samples=0,
    )
    (r,) = results
    assert (r.discovered, r.allowed, r.denied, r.review) == (3, 3, 0, 0)
    assert r.known_bytes == len(GOOD) + len(RESTRICTED) + len(UNMARKED)
    out = io.StringIO()
    render_dry_run(results, out)
    assert "test-pubs" in out.getvalue()
    assert "TOTAL" in out.getvalue()


def test_git_mirror_sources_are_surveyed_as_bundles() -> None:
    s = SourceSpec(
        id="frus",
        name="FRUS",
        tier=Tier.T1,
        status=SourceStatus.CANDIDATE,
        license="LicenseRef-US-Gov-PD",
        legal_basis="public-domain",
        domain=("defense",),
        adapter="git-mirror",
        raw={
            "id": "frus",
            "access": {"adapter": "git-mirror", "endpoints": ["https://github.com/HistoryAtState/frus"]},
        },
    )
    coll = collection_spec(s)
    assert coll is not None
    access: Any = coll.raw["access"]
    assert access["discovery"] == [{"kind": "git", "repos": ["https://github.com/HistoryAtState/frus"]}]


def test_review_decisions_are_remembered_in_the_rules_file(tmp_path: Path) -> None:
    doc = {
        "doc_review": {
            "source_id": "army-pubs-public",
            "documents": [{"key": "army:ATP-9-99", "pub_number": "ATP 9-99"}],
        }
    }
    path = apply_review_decision(tmp_path, doc, accept=True)
    assert path is not None
    assert yaml.safe_load(path.read_text())["allow_documents"] == ["ATP 9-99"]
    apply_review_decision(
        tmp_path,
        {
            "doc_review": {
                "source_id": "army-pubs-public",
                "documents": [{"key": "k", "pub_number": "TC 9-1"}],
            }
        },
        accept=False,
    )
    data = yaml.safe_load(path.read_text())
    assert data["deny_documents"] == ["TC 9-1"]
    assert data["allow_documents"] == ["ATP 9-99"]


def test_api_keys_come_from_local_config_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from wsp_cli.doccollections import api_keys

    monkeypatch.setenv("WSP_API_KEY_CONGRESS", "env-key")
    keys = api_keys({"curator": {"api_keys": {"govinfo_key": "cfg-key", "empty_key": ""}}})
    assert keys == {"govinfo": "cfg-key", "congress": "env-key"}


# --- C3 (2026-10-11 audit): git bundles and redirect hops are screened -------------------------------


def _git_spec(endpoints: list[str]) -> SourceSpec:
    return SourceSpec(
        id="frus",
        name="FRUS",
        tier=Tier.T1,
        status=SourceStatus.CANDIDATE,
        license="LicenseRef-US-Gov-PD",
        legal_basis="public-domain",
        domain=("defense",),
        adapter="git-mirror",
        raw={"id": "frus", "access": {"adapter": "git-mirror", "endpoints": endpoints}},
    )


def test_git_allow_list_is_the_registry_endpoints() -> None:
    spec_ = _git_spec(["https://github.com/HistoryAtState/frus", "https://cwe.mitre.org/data/downloads.html"])
    assert registry_repos(spec_) == frozenset({"historyatstate/frus"})
    assert github_repo("https://github.com/HistoryAtState/frus.git") == "historyatstate/frus"
    assert github_repo("https://github.com/a/b/../../evil/x") is None
    assert github_repo("http://github.com/a/b") is None
    assert github_repo("https://github.com.evil.example/a/b") is None


def test_unlisted_repository_is_never_cloned(tmp_path: Path) -> None:
    fetcher = CollectionFetcher(
        tmp_path / "vault", FakeFetcher(), keys={}, user_agent="t", workdir=tmp_path / "w", git="/bin/false"
    )
    cand = Candidate(
        key="x:evil-repo", url="https://github.com/evil/repo.git", title="t", issuer="i", resolve="git"
    )
    with pytest.raises(ValueError, match="not a repository the registry lists"):
        fetcher._git_bundle(cand, tmp_path / "out", Rules(), frozenset({"historyatstate/frus"}))  # pyright: ignore[reportPrivateUsage]
    assert not (tmp_path / "w" / "git").exists()


def _local_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid", "GIT_COMMITTER_NAME": "t"}
    env |= {"GIT_COMMITTER_EMAIL": "t@invalid", "PATH": "/usr/bin:/bin", "HOME": str(root)}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=env)
    return root


@pytest.mark.parametrize(
    ("files", "verdict"),
    [
        ({"README.md": "Foreign Relations of the United States volumes. " * 4, "v/1.xml": "<x/>"}, "allow"),
        ({"README.md": "Tools for exploit development and malware development. " * 3}, "deny"),
        (
            {"README.md": "Collected notes and references for practitioners. " * 3, "weaponeering/a.md": "x"},
            "deny",
        ),
        ({"data.bin": "x"}, "review"),  # nothing to read: held
    ],
)
def test_git_repository_is_screened_by_readme_and_paths(
    tmp_path: Path, files: dict[str, str], verdict: str
) -> None:
    repo = _local_repo(tmp_path / "repo", files)
    fetcher = CollectionFetcher(tmp_path / "vault", FakeFetcher(), keys={}, user_agent="t", workdir=tmp_path)
    got = fetcher._repo_screen_text(repo)  # pyright: ignore[reportPrivateUsage]
    assert screen_text(got, Rules(allow_all=True)).decision.verdict.value == verdict


@dataclass(frozen=True, slots=True)
class _ProvenanceReport(FetchResult):
    provenance: dict[str, Any] = field(default_factory=dict[str, Any])


class _Redirecting(FakeFetcher):
    """Serves the bytes but reports a redirect to another host, as ``HttpFetcher`` would."""

    def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
        got = super().download(remote, dest)
        hop = {"url": remote.url, "status": 302, "location": "https://cdn.evil.example/x.pdf"}
        src = {"url": remote.url, "final_url": "https://cdn.evil.example/x.pdf", "redirects": [hop]}
        return _ProvenanceReport(got.path, got.size, got.sha256, got.blake3, provenance={"sources": [src]})


def test_fetch_refuses_a_document_redirected_off_origin(vault: FileVault, tmp_path: Path) -> None:
    objects = [("FM-5-0_2022.pdf", GOOD)]
    fetch = CollectionFetcher(
        vault.root,
        _Redirecting(routes(objects)),
        keys={},
        user_agent="t",
        workdir=tmp_path / "w",
        throttle=False,
    )
    report = fetch.run(spec(objects), version="2026-10-10")
    assert report.fetched == []
    assert report.failed
    assert "OriginError" in report.failed[0][1]
    assert not list((vault.root / "_incoming" / "test-pubs" / "2026-10-10").glob("*.pdf"))


def test_concurrent_downloads_give_the_same_report(vault: FileVault, tmp_path: Path) -> None:
    many = [*OBJECTS, *[(f"FM-6-{i}_2022.pdf", GOOD) for i in range(8)]]
    base = spec(many)
    access = {
        **cast(dict[str, object], base.raw["access"]),
        "rate": {"requests_per_second": 1, "concurrency": 4},
    }
    s = dataclasses.replace(base, raw={**base.raw, "access": access})
    report = runner(vault, many, tmp_path).run(s, version="2026-10-10")
    assert sorted(d.cand.title for d in report.fetched) == sorted(
        ["FM 5 0 2022", *[f"FM 6 {i} 2022" for i in range(8)]]
    )
    assert report.rejected == [("test:restricted.pdf", "deny:marking=distribution-C")]
    assert len(report.held) == 1
    assert not report.failed


def test_a_resumed_fetch_reuses_complete_files_with_a_published_md5(tmp_path: Path) -> None:
    import hashlib

    from wsp_adapters.doccollection.model import Candidate

    fetch = CollectionFetcher(
        tmp_path / "vault", FakeFetcher(), keys={}, user_agent="t", workdir=tmp_path, throttle=False
    )
    dest = tmp_path / "dest"
    dest.mkdir()
    cand = Candidate(
        key="t:a",
        url="https://bucket.example.gov/a.pdf",
        title="A",
        issuer="T",
        size=3,
        md5=hashlib.md5(b"pdf").hexdigest(),
    )
    (dest / cand.file_name).write_bytes(b"pdf")
    assert fetch._download(FakeFetcher(), cand, dest, ["bucket.example.gov"]) == dest / cand.file_name  # pyright: ignore[reportPrivateUsage]
    (dest / cand.file_name).write_bytes(b"bad")  # same size, wrong bytes: fetched again (and here: fails)
    with pytest.raises(Exception):  # noqa: B017, PT011 - the fake fetcher has no route for it
        fetch._download(FakeFetcher(), cand, dest, ["bucket.example.gov"])  # pyright: ignore[reportPrivateUsage]


def test_an_incomplete_listing_fetches_nothing_and_withdraws_nothing(
    vault: FileVault, tmp_path: Path
) -> None:
    from wsp_core.errors import FetchError

    class Flaky(FakeFetcher):
        def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
            if url.startswith(f"{BUCKET}/?"):  # the listing itself times out
                raise FetchError("GET listing failed after 6 attempt(s): ReadTimeout")
            return super().get_bytes(url, max_bytes=max_bytes)

    s = spec(OBJECTS)
    first = runner(vault, OBJECTS, tmp_path).run(s, version="2026-10-10")
    assert first.fetched
    flaky = CollectionFetcher(
        vault.root,
        Flaky(routes(OBJECTS)),
        keys={},
        user_agent="t",
        workdir=tmp_path / "w",
        out=io.StringIO(),
        throttle=False,
    )
    again = flaky.run(s, version="2026-11-10")
    assert again.incomplete
    assert again.fetched == []
    assert not (vault.root / "_incoming" / "test-pubs" / "2026-11-10").exists()  # no partial generation


def test_a_gone_index_page_is_not_an_incomplete_listing() -> None:
    from wsp_adapters.doccollection.discovery import DiscoverEnv, discover_all

    env = DiscoverEnv(fetcher=FakeFetcher({}), issuer="T", prefix="t", origins=("a.gov",))
    cfg = {"kind": "index-page", "urls": ["https://a.gov/gone"], "documents": r"a\.gov/.+\.pdf$"}
    assert list(discover_all([cfg], env)) == []
    assert env.notes  # noted
    assert env.incomplete == []  # but a 404 hides nothing


def test_an_unresolved_item_withdraws_nothing_but_does_not_stop_the_fetch(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wsp_adapters.doccollection.adapter import DocCollectionAdapter

    s = spec(OBJECTS)
    assert runner(vault, OBJECTS, tmp_path).run(s, version="2026-10-10").fetched
    real = DocCollectionAdapter.survey

    def one_unresolved(self: DocCollectionAdapter, *args: object, **kwargs: object) -> object:
        out = real(self, *args, **kwargs)  # type: ignore[arg-type]
        out.allowed = [c for c in out.allowed if "FM-5-0" not in c.key]  # its resolve failed this run
        out.unresolved = 1
        return out

    monkeypatch.setattr(DocCollectionAdapter, "survey", one_unresolved)
    more = [*OBJECTS, ("FM-6-0_2022.pdf", GOOD)]
    again = runner(vault, more, tmp_path).run(s, version="2026-11-10")
    assert not again.incomplete
    assert [d.cand.title for d in again.fetched] == ["FM 6 0 2022"]
    catalog = read_catalog_file(vault.root / "_incoming" / "test-pubs" / "2026-11-10" / CATALOG)
    assert all(m.currency != "withdrawn" for m in catalog)  # FM 5-0 was not listed, yet not withdrawn
