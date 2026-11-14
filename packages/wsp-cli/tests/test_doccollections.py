"""``wsp collections`` (fetch + dry run) and ``wsp ingest`` of a document-collection generation."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import subprocess
import zipfile
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
import yaml

from wsp_adapters.doccollection import DocCollectionAdapter
from wsp_adapters.doccollection.adapter import CATALOG, Screen, WrongBodyError, screen_text
from wsp_adapters.doccollection.catalog import read_catalog_file
from wsp_adapters.doccollection.model import Candidate
from wsp_adapters.doccollection.policy import Decision, Marking, Rules, Verdict, load_rules
from wsp_adapters.doccollection.testing import PUBLIC_A, RESTRICTED_C, make_pdf, s3_listing
from wsp_cli import doccollections as dc
from wsp_cli.doccollections import (
    CollectionFetcher,
    FetchReport,
    ReviewDecisionError,
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


def ingest(vault: FileVault, s: SourceSpec) -> None:
    result = Ingestor(vault, [s], out=io.StringIO()).run(source_id=s.id)
    statuses = {vp.status for vp in result.versions}
    assert VersionStatus.INGESTED in statuses, result.versions
    assert VersionStatus.FAILED not in statuses, result.versions


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

    # --refetch: the key is downloaded again whatever the listing says; identical bytes stay skipped.
    import wsp_cli.doccollections as dc

    seen: list[str] = []
    real = dc.unchanged

    def spy(cand: Candidate, held: object, **kw: object) -> bool:
        seen.append(cand.key)
        return real(cand, held, **kw)  # type: ignore[arg-type]

    dc.unchanged = spy  # type: ignore[assignment]
    try:
        forced = runner(vault, OBJECTS, tmp_path).run(
            s, version="2026-12-10", refetch=frozenset({"test:FM-5-0_2022.pdf"})
        )
    finally:
        dc.unchanged = real
    assert "test:FM-5-0_2022.pdf" not in seen
    assert forced.fetched == []  # downloaded, byte-identical, not kept
    assert forced.skipped == 1


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


RULES_TEXT = """\
# Hand-written rules: this comment must survive every review decision (CA-1).
# The ISC Design-Basis Threat appendices are restricted and must never be fetched.
deny_keywords:
  - design-basis threat  # inline note
allow_all: true
"""


def _review_spec(rules: str | None) -> SourceSpec:
    filt: dict[str, Any] = {"allow_all": True}
    if rules is not None:
        filt["rules"] = rules
    return dataclasses.replace(
        spec([]), raw={"id": "test-pubs", "access": {"adapter": "doc-collection", "filter": filt}}
    )


def _review(*rows: tuple[str, str]) -> dict[str, Any]:
    return {
        "doc_review": {
            "source_id": "test-pubs",
            "documents": [{"key": k, "pub_number": "", "title": k, "rule": r} for k, r in rows],
        }
    }


def test_a_rejected_review_denies_only_the_named_rows_and_never_rewrites_the_rules_file(
    tmp_path: Path,
) -> None:
    rules = tmp_path / "registry" / "filters" / "test-pubs.yaml"
    rules.parent.mkdir(parents=True)
    rules.write_text(RULES_TEXT, encoding="utf-8")
    specs = [_review_spec("registry/filters/test-pubs.yaml")]
    doc = _review(("t:a", "review:marking=missing-public-release"), ("t:b", "review:text=no-text-layer"))
    with pytest.raises(ReviewDecisionError, match="--deny"):
        apply_review_decision(tmp_path, specs, doc, allow=False)  # a rejection alone denies nothing
    with pytest.raises(ReviewDecisionError, match="not rows"):
        apply_review_decision(tmp_path, specs, doc, allow=False, keys=["t:zzz"])
    denied = apply_review_decision(tmp_path, specs, doc, allow=False, keys=["t:b"], proposal=7)
    assert denied.keys == ("t:b",)
    assert rules.read_text(encoding="utf-8") == RULES_TEXT  # comments and all: untouched
    assert denied.path == tmp_path / "registry" / "filters" / "decisions" / "test-pubs.yaml"
    data = yaml.safe_load(denied.path.read_text(encoding="utf-8"))
    assert data["deny_documents"] == ["t:b"]
    assert data["allow_documents"] == []
    assert data["log"][0]["proposal"] == 7
    loaded = load_rules({"rules": "registry/filters/test-pubs.yaml"}, repo_root=tmp_path)
    assert loaded.deny_documents == {"T:B"}  # keys are compared normalized (policy.normalize_pub_number)
    assert loaded.deny_keywords == ("design-basis threat",)  # the hand-written rules still load


def test_an_accepted_review_allows_rows_by_document_key_and_releases_content_holds(tmp_path: Path) -> None:
    rules = tmp_path / "rules.yaml"
    rules.write_text(RULES_TEXT, encoding="utf-8")
    specs = [_review_spec(str(rules))]
    doc = _review(("t:a", "review:safety=booby traps"), ("t:b", "review:marking=missing-A"))
    first = apply_review_decision(tmp_path, specs, doc, allow=True, keys=["t:a"])
    data = yaml.safe_load(first.path.read_text(encoding="utf-8"))
    assert data["allow_documents"] == ["t:a"]
    assert data["allow_content_review"] == ["t:a"]  # a content hold needs the explicit release
    apply_review_decision(tmp_path, specs, doc, allow=False, keys=["t:a"])  # changed mind: replaces it
    data = yaml.safe_load(first.path.read_text(encoding="utf-8"))
    assert data["allow_documents"] == []
    assert data["deny_documents"] == ["t:a"]
    assert rules.read_text(encoding="utf-8") == RULES_TEXT


def test_a_review_decision_without_a_rules_file_is_refused(tmp_path: Path) -> None:
    doc = _review(("t:a", "review:marking=missing-A"))
    with pytest.raises(ReviewDecisionError, match=r"no filter\.rules"):
        apply_review_decision(tmp_path, [_review_spec(None)], doc, allow=True)
    assert not any(tmp_path.rglob("*.yaml"))  # nothing written where nothing would load it


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


def test_a_repository_a_git_discovery_names_is_allowed() -> None:
    """2026-10-26 review: Ben-Yehuda's dump, named in its git discovery's repos, was refused."""
    spec_ = replace(
        _git_spec([]),
        raw={
            "id": "by",
            "access": {
                "adapter": "doc-collection",
                "discovery": [
                    {"kind": "git", "repos": ["https://github.com/projectbenyehuda/public_domain_dump"]}
                ],
            },
        },
    )
    assert registry_repos(spec_) == frozenset({"projectbenyehuda/public_domain_dump"})


def test_a_git_clone_honours_github_robots_txt_and_sends_the_library_user_agent(tmp_path: Path) -> None:
    """2026-10-26 review: clones went around robots.txt and sent git's own User-Agent."""
    from wsp_core.errors import RobotsDisallowedError

    class Refusing(FakeFetcher):
        def robots_allows(self, url: str) -> bool:
            self.requests.append(url)
            return False

    fetcher = Refusing()
    cf = CollectionFetcher(
        tmp_path / "vault",
        fetcher,
        keys={},
        user_agent="Whisipedia/0.1 (test)",
        workdir=tmp_path / "w",
        git="/bin/false",
    )
    cand = Candidate(key="x:y", url="https://github.com/owner/repo.git", title="t", issuer="i", resolve="git")
    with pytest.raises(RobotsDisallowedError):
        cf._git_bundle(cand, tmp_path / "out", Rules(), frozenset({"owner/repo"}))  # pyright: ignore[reportPrivateUsage]
    assert fetcher.requests == ["https://github.com/owner/repo.git/info/refs?service=git-upload-pack"]
    seen: list[list[str]] = []

    def run(cmd: list[str], **_: Any) -> Any:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"")

    with patch.object(subprocess, "run", run):
        cf._git("status")  # pyright: ignore[reportPrivateUsage]
    assert "http.userAgent=Whisipedia/0.1 (test)" in seen[0]


def test_a_stalled_git_transfer_is_given_up_not_waited_on_forever(tmp_path: Path) -> None:
    """2026-11-04 trawl: git has no timeout of its own; a clone whose socket stayed open while nothing
    came held the whole collection pass. Every git command carries curl's low-speed limit."""
    cf = CollectionFetcher(
        tmp_path / "vault", FakeFetcher(), keys={}, user_agent="t", workdir=tmp_path / "w", git="/bin/false"
    )
    seen: list[list[str]] = []

    def run(cmd: list[str], **_: Any) -> Any:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"")

    with patch.object(subprocess, "run", run):
        cf._git("clone", "--mirror", "https://github.com/owner/repo.git", str(tmp_path / "m"))  # pyright: ignore[reportPrivateUsage]
    pairs = {seen[0][i + 1] for i, a in enumerate(seen[0][:-1]) if a == "-c"}
    assert {"http.lowSpeedLimit=1000", "http.lowSpeedTime=300"} <= pairs
    assert seen[0].index("http.lowSpeedTime=300") < seen[0].index("clone")  # options before the command


def test_an_organisation_discovery_allows_that_organisations_repositories(tmp_path: Path) -> None:
    """Standard Ebooks names no repository one by one: discovering its organisation allows them, and
    only them (all 1,533 books were refused, 2026-10-19)."""
    spec_ = replace(
        _git_spec([]),
        raw={
            "id": "se",
            "access": {"adapter": "doc-collection", "discovery": [{"kind": "git", "org": "StandardEbooks"}]},
        },
    )
    repos = registry_repos(spec_)
    assert repos == frozenset({"standardebooks/*"})
    fetcher = CollectionFetcher(
        tmp_path / "vault", FakeFetcher(), keys={}, user_agent="t", workdir=tmp_path / "w", git="/bin/false"
    )
    elsewhere = Candidate(
        key="x:y", url="https://github.com/evil/repo.git", title="t", issuer="i", resolve="git"
    )
    with pytest.raises(ValueError, match="not a repository the registry lists"):
        fetcher._git_bundle(elsewhere, tmp_path / "out", Rules(), repos)  # pyright: ignore[reportPrivateUsage]
    book = Candidate(
        key="se:x", url="https://github.com/standardebooks/joseph-conrad_heart-of-darkness.git", title="t",
        issuer="i", resolve="git",
    )  # fmt: skip
    with pytest.raises(subprocess.CalledProcessError):  # allowed: it reaches git (here /bin/false)
        fetcher._git_bundle(book, tmp_path / "out", Rules(), repos)  # pyright: ignore[reportPrivateUsage]


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
            {
                "README.md": "Collected notes and references for practitioners. " * 3,
                "shaped_charges/a.md": "x",
            },
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


def test_a_snapshot_is_the_default_branch_files_as_one_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FRUS's history is 14.5 GB and its refiner reads the files only (2026-10-17): ``snapshot: true``
    keeps a shallow clone and stores ``git archive`` of the default branch."""
    readme = "Foreign Relations of the United States volumes. " * 4
    repo = _local_repo(tmp_path / "repo", {"README.md": readme, "volumes/frus1.xml": "<TEI/>"})
    branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"], check=True, capture_output=True, text=True
    ).stdout.strip()
    fetcher = CollectionFetcher(
        tmp_path / "vault", FakeFetcher(), keys={}, user_agent="t", workdir=tmp_path / "w"
    )
    cand = Candidate(
        key="frus:HistoryAtState-frus",
        url="https://github.com/HistoryAtState/frus.git",
        title="FRUS",
        issuer="i",
        resolve="git",
        extra={"default_branch": branch, "snapshot": "1"},
    )
    clone = tmp_path / "w" / "git" / "frus_HistoryAtState-frus.shallow"  # kept from an earlier run
    subprocess.run(["git", "clone", "-q", "--bare", f"file://{repo}", str(clone)], check=True)
    real = fetcher._git  # pyright: ignore[reportPrivateUsage]

    def local_fetch(
        *args: str, cwd: Path | None = None, capture: bool = False
    ) -> str:  # https only in real runs
        if args[0] == "fetch":
            subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)
            return ""
        return real(*args, cwd=cwd, capture=capture)

    monkeypatch.setattr(fetcher, "_git", local_fetch)
    bundle = fetcher._git_bundle  # pyright: ignore[reportPrivateUsage]
    path, screen = bundle(cand, tmp_path / "out", Rules(allow_all=True), frozenset({"historyatstate/frus"}))
    assert path.suffix == ".zip"
    with zipfile.ZipFile(path) as z:
        assert sorted(z.namelist()) == ["README.md", "volumes/", "volumes/frus1.xml"]
    assert screen.decision.verdict.value == "allow"


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


def test_a_download_is_verified_against_the_listings_checksum_not_fetched_twice(tmp_path: Path) -> None:
    """2026-11-04 trawl: the listing's MD5 was dropped, so every such file was corroborated by a second
    full download and never compared with the checksum its issuer published."""
    import hashlib

    from wsp_core.model import Checksum

    class Recording(FakeFetcher):
        def __init__(self, routes: dict[str, bytes]) -> None:
            super().__init__(routes)
            self.remotes: list[RemoteFile] = []

        def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
            self.remotes.append(remote)
            return super().download(remote, dest)

    url = "https://bucket.example.gov/a.pdf"
    fetcher = Recording({url: GOOD})
    fetch = CollectionFetcher(
        tmp_path / "vault", fetcher, keys={}, user_agent="t", workdir=tmp_path, throttle=False
    )
    md5 = hashlib.md5(GOOD, usedforsecurity=False).hexdigest()
    sha = hashlib.sha256(GOOD).hexdigest()
    cand = Candidate(key="t:a", url=url, title="A", issuer="T", size=len(GOOD), md5=md5, sha256=sha)
    fetch._download(fetcher, cand, tmp_path / "dest", ["bucket.example.gov"])  # pyright: ignore[reportPrivateUsage]
    assert [r.checksums for r in fetcher.remotes] == [(Checksum("sha256", sha), Checksum("md5", md5))]


class _Verifying(FakeFetcher):
    """Checks the listing's checksums as ``HttpFetcher`` does: other bytes are deleted, never kept."""

    def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
        import hashlib

        from wsp_core.errors import ChecksumMismatchError

        got = super().download(remote, dest)
        data = Path(got.path).read_bytes()
        for c in remote.checksums:
            actual = hashlib.new(c.algorithm, data).hexdigest()
            if actual != c.hexdigest.lower():
                Path(got.path).unlink()
                raise ChecksumMismatchError(remote.url, c.algorithm, c.hexdigest, actual)
        return got


def test_a_listing_checksum_that_never_matches_is_set_aside_with_an_alert(
    vault: FileVault, tmp_path: Path
) -> None:
    """2026-11-04 trawl review: a stale published checksum (or an S3 ETag that is no MD5) failed the
    same bytes on every run, downloaded whole each time, and the source never finished. The same
    bytes on three tries over two days set it aside as gone, with an alert; other bytes start over."""
    from datetime import UTC, datetime, timedelta

    from wsp_cli import doccollections as dc

    listed = [("FM-5-0_2022.pdf", GOOD)]
    served = {**routes(listed), f"{BUCKET}/{PREFIX}FM-5-0_2022.pdf": UNMARKED}  # not the listed bytes
    work = tmp_path / "w"
    fetch = CollectionFetcher(
        vault.root, _Verifying(served), keys={}, user_agent="t", workdir=work, throttle=False
    )
    report = fetch.run(spec(listed), version="2026-10-10")
    assert [k for k, _ in report.failed] == ["test:FM-5-0_2022.pdf"]  # one mismatch: tried again
    assert "md5 mismatch" in report.failed[0][1]
    assert report.gone == []
    assert report.alerts == []
    ledger = work / "test-pubs" / "stale-checksum.json"
    entry = json.loads(ledger.read_text())["test:FM-5-0_2022.pdf"]
    assert entry["tries"] == 1
    entry["first"] = (datetime.now(UTC) - timedelta(days=3)).isoformat(timespec="seconds")
    ledger.write_text(json.dumps({"test:FM-5-0_2022.pdf": {**entry, "tries": 2}}))
    report = fetch.run(spec(listed), version="2026-10-10")
    assert report.failed == []
    assert [k for k, _ in report.gone] == ["test:FM-5-0_2022.pdf"]
    assert len(report.alerts) == 1
    assert "checksum never matches" in report.alerts[0]
    assert dc.fetch_exit_code(report) != dc.EXIT_PARTIAL  # the source finishes
    assert report.fetched == []  # the bytes are never taken

    now = datetime(2026, 11, 4, tzinfo=UTC)
    for day, sig in enumerate(["md5 a -> b", "md5 a -> c", "md5 a -> c"]):  # a transfer error, then
        r = dc.FetchReport("s", "v", failed=[("k", "mismatch")], mismatched={"k": sig})
        dc.settle_stale_checksums(r, tmp_path / "l.json", now + timedelta(days=day))
        assert r.gone == []  # other bytes started the count over: two tries of "c" so far
    r = dc.FetchReport("s", "v", failed=[("k", "mismatch")], mismatched={"k": "md5 a -> c"})
    dc.settle_stale_checksums(r, tmp_path / "l.json", now + timedelta(days=3))
    assert [k for k, _ in r.gone] == ["k"]


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
    ingest(vault, s)
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
    assert env.incomplete == []  # but a 404 hides nothing: an incomplete listing would stop the fetch,
    # and one dead page among many roots would block the source for good. A moved site that empties the
    # listing is caught when the catalog is built instead (_mass_withdrawal, 2026-10-26 review #21).


def test_an_unresolved_item_withdraws_nothing_but_does_not_stop_the_fetch(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wsp_adapters.doccollection.adapter import DocCollectionAdapter

    s = spec(OBJECTS)
    assert runner(vault, OBJECTS, tmp_path).run(s, version="2026-10-10").fetched
    ingest(vault, s)
    real = DocCollectionAdapter.survey

    def one_unresolved(self: DocCollectionAdapter, *args: object, **kwargs: object) -> object:
        out = real(self, *args, **kwargs)  # type: ignore[arg-type]
        gone = [c for c in out.allowed if "FM-5-0" in c.key]
        out.allowed = [c for c in out.allowed if "FM-5-0" not in c.key]  # its resolve failed this run
        out.decisions = [(c, d) for c, d in out.decisions if "FM-5-0" not in c.key]
        out.unresolved = 1
        out.unresolved_keys = [c.key for c in gone]
        return out

    monkeypatch.setattr(DocCollectionAdapter, "survey", one_unresolved)
    more = [*OBJECTS, ("FM-6-0_2022.pdf", GOOD)]
    again = runner(vault, more, tmp_path).run(s, version="2026-11-10")
    assert not again.incomplete
    assert [d.cand.title for d in again.fetched] == ["FM 6 0 2022"]
    catalog = read_catalog_file(vault.root / "_incoming" / "test-pubs" / "2026-11-10" / CATALOG)
    assert all(m.currency != "withdrawn" for m in catalog)  # FM 5-0 was not listed, yet not withdrawn


# --- 2026-10-12 audit (collections-adapters): CA-12, CA-13, CA-14, CA-15, CA-16 ---------------------------


def test_a_second_fetch_waits_for_ingest_then_takes_a_new_version(vault: FileVault, tmp_path: Path) -> None:
    from wsp_cli.doccollections import EXIT_INGEST_PENDING

    s = spec(OBJECTS)
    first = runner(vault, OBJECTS, tmp_path).run(s, version="2026-10-10")
    assert first.planned
    pending = runner(vault, OBJECTS, tmp_path).run(s, version="2026-10-11")
    assert pending.pending == ["2026-10-10"]  # another cycle waits: its catalog would miss 10-10's documents
    assert not pending.fetched
    # The same cycle again is allowed: it repairs an interrupted ingest (vault audit H2).
    assert runner(vault, OBJECTS, tmp_path).run(s, version="2026-10-10").pending == []
    assert EXIT_INGEST_PENDING == 5
    ingest(vault, s)
    more = [*OBJECTS, ("FM-6-0_2022.pdf", GOOD)]
    again = runner(vault, more, tmp_path).run(s, version="2026-10-10")
    assert again.version == "2026-10-10r2"  # the sealed version is never fetched into again
    assert [d.cand.key for d in again.fetched] == ["test:FM-6-0_2022.pdf"]
    assert again.skipped == 1  # unchanged by listing MD5: not downloaded
    ingest(vault, s)
    assert runner(vault, more, tmp_path).free_version(s, "2026-10-10", None) == "2026-10-10r3"


def test_a_changed_catalog_without_new_documents_is_still_recorded(vault: FileVault, tmp_path: Path) -> None:
    s = spec(OBJECTS)
    runner(vault, OBJECTS, tmp_path).run(s, version="2026-10-10")
    ingest(vault, s)
    gone = [o for o in OBJECTS if not o[0].startswith("FM-5-0")]
    report = runner(vault, gone, tmp_path).run(s, version="2026-11-10")
    assert not report.fetched
    assert report.planned  # a catalog-only generation (CA-14)
    catalog = read_catalog_file(vault.root / "_incoming" / "test-pubs" / "2026-11-10" / CATALOG)
    assert [m.currency for m in catalog] == ["withdrawn-from-index"]
    ingest(vault, s)
    quiet = runner(vault, gone, tmp_path).run(s, version="2026-12-10")
    assert not quiet.planned  # nothing changed: nothing to ingest


def test_unchanged_compares_the_listing_with_the_stored_bytes() -> None:
    from datetime import UTC, datetime, timedelta

    from wsp_adapters.doccollection.model import DocumentMeta
    from wsp_cli.doccollections import REFETCH_AFTER, unchanged

    now = datetime(2026, 10, 12, tzinfo=UTC)
    held = DocumentMeta(
        doc_key="k",
        file="f",
        issuer="i",
        title="t",
        sha256="s",
        bytes=10,
        md5="aa",
        fetched_at=(now - timedelta(days=10)).isoformat(),
    )
    cand = Candidate(key="k", url="https://x/f.pdf", title="t", issuer="i")
    assert unchanged(dataclasses.replace(cand, md5="AA", size=10), held, now=now)
    assert not unchanged(dataclasses.replace(cand, md5="bb", size=10), held, now=now)
    assert not unchanged(dataclasses.replace(cand, size=11), held, now=now)
    assert unchanged(cand, held, now=now)  # no size, no checksum: trusted for a while
    assert not unchanged(cand, dataclasses.replace(held, bytes=0, md5=None), now=now)  # an empty copy
    assert not unchanged(cand, held, now=now + REFETCH_AFTER)  # then re-checked
    assert not unchanged(cand, None, now=now)
    # The key now lists another file: fetched again, whatever the listing says (an http->https move
    # of the same file is not a move).
    placed = dataclasses.replace(held, origin_url="https://x/addendum.pdf")
    assert not unchanged(dataclasses.replace(cand, md5="AA", size=10), placed, now=now)
    assert unchanged(dataclasses.replace(cand, url="https://X/addendum.pdf"), placed, now=now)
    assert unchanged(
        dataclasses.replace(cand, url="https://x/addendum.pdf"),
        dataclasses.replace(placed, origin_url="http://x/addendum.pdf"),
        now=now,
    )


def test_held_documents_are_proposed_once(tmp_path: Path) -> None:
    from wsp_cli.doccollections import unproposed_rows

    root = tmp_path / "proposals"
    root.mkdir()
    doc = {"id": 1, "status": "open", "doc_review": {"source_id": "test-pubs", "documents": [{"key": "t:a"}]}}
    (root / "00001-doc-review-test-pubs.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    cfg = {"curator": {"proposals_dir": str(root)}}
    rows = [
        {"key": "t:a", "title": "A", "rule": "r"},
        {"key": "t:b", "title": "B", "rule": "r"},
        {"key": "t:b", "title": "B", "rule": "r"},
    ]
    assert [r["key"] for r in unproposed_rows(cfg, "test-pubs", rows)] == ["t:b"]


def test_collection_sources_are_never_indexed_without_a_refine_adapter() -> None:
    from wsp_adapters import available_adapters
    from wsp_cli.doccollections import COLLECTION_ADAPTERS

    repo = Path(__file__).resolve().parents[3]
    data: Any = yaml.safe_load((repo / "registry" / "sources.yaml").read_text(encoding="utf-8"))
    installed = set(available_adapters())
    for raw in data["sources"]:
        access: dict[str, Any] = raw.get("access") or {}
        adapter = access.get("adapter")
        if adapter in COLLECTION_ADAPTERS and raw.get("index", True) is not False:
            assert adapter in installed, f"{raw['id']}: indexed but no {adapter!r} refine adapter"


def test_files_fetched_again_for_a_sealed_version_are_a_failure_not_sealed(
    vault: FileVault, tmp_path: Path
) -> None:
    s = spec(OBJECTS)
    first = runner(vault, OBJECTS, tmp_path).run(s, version="2026-10-10")
    ingest(vault, s)
    name = first.fetched[0].file.removeprefix("upstream/")
    leftover = vault.root / "_incoming" / "test-pubs" / "2026-10-10" / name
    leftover.write_bytes(GOOD)  # what a same-version re-fetch used to leave behind
    result = Ingestor(vault, [s], out=io.StringIO()).run(source_id="test-pubs")
    (vp,) = [v for v in result.versions if v.generation_id == "2026-10-10"]
    assert vp.status is VersionStatus.FAILED
    assert "not ingested" in vp.reason


class _NoNetwork(FakeFetcher):
    def download(self, remote: RemoteFile, dest: Path) -> Any:
        raise AssertionError(f"downloaded {remote.url}")


def test_a_kept_download_the_owner_takes_is_used_as_it_is_and_nothing_is_fetched(tmp_path: Path) -> None:
    """GEBCO (2026-10-23): kept as .uncorroborated (no checksum upstream, no second download), and
    the owner keeps it. Its zip must read whole; a paused source fetches nothing else."""
    import zipfile

    from wsp_adapters.doccollection.model import Candidate
    from wsp_core.errors import FetchError

    fetch = CollectionFetcher(
        tmp_path / "vault",
        _NoNetwork(),
        keys={},
        user_agent="t",
        workdir=tmp_path,
        throttle=False,
        adopt_kept={"g:grid"},
        offline=True,
    )
    dest = tmp_path / "dest"
    dest.mkdir()
    grid = Candidate(
        key="g:grid", url="https://bodc.example/grid/", title="Grid", issuer="G", extra={"suffix": ".zip"}
    )
    other = Candidate(key="g:other", url="https://bodc.example/other.zip", title="Other", issuer="G")
    kept = dest / f"{grid.file_name}.uncorroborated"
    with zipfile.ZipFile(kept, "w") as z:
        z.writestr("GEBCO_2024.nc", b"elevation " * 1000)
    got = fetch._download(_NoNetwork(), grid, dest, ["bodc.example"])  # pyright: ignore[reportPrivateUsage]
    assert got == dest / grid.file_name
    assert zipfile.ZipFile(got).read("GEBCO_2024.nc").startswith(b"elevation")
    assert not kept.exists()
    assert fetch._download(_NoNetwork(), grid, dest, ["bodc.example"]) == got  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(FetchError, match="paused"):
        fetch._download(_NoNetwork(), other, dest, ["bodc.example"])  # pyright: ignore[reportPrivateUsage]

    # A kept file that does not read whole is refused, and stays where it is.
    got.unlink()
    kept.write_bytes(b"<html>Service unavailable</html>")  # not a zip at all
    with pytest.raises(FetchError, match="no offline check"):
        fetch._download(_NoNetwork(), grid, dest, ["bodc.example"])  # pyright: ignore[reportPrivateUsage]
    with zipfile.ZipFile(kept, "w") as z:
        z.writestr("GEBCO_2024.nc", b"elevation " * 1000)
    raw = bytearray(kept.read_bytes())
    raw[40] ^= 0xFF  # a byte of the member's data
    kept.write_bytes(bytes(raw))
    with pytest.raises(FetchError, match=r"CRC|does not read whole"):
        fetch._download(_NoNetwork(), grid, dest, ["bodc.example"])  # pyright: ignore[reportPrivateUsage]
    assert kept.exists()


def _manual_spec() -> SourceSpec:
    return SourceSpec(
        id="test-gazetteer",
        name="Test gazetteer",
        tier=Tier.T1,
        status=SourceStatus.PAUSED,  # its host's robots.txt disallows crawlers
        license="CC-BY-4.0",
        legal_basis="open-license",
        domain=("geography",),
        adapter="doc-collection",
        raw={
            "id": "test-gazetteer",
            "access": {
                "adapter": "doc-collection",
                "issuer": "Test",
                "key_prefix": "gaz",
                "origins": ["dl.example.org"],
                "discovery": [
                    {
                        "kind": "list",
                        "items": [
                            {"pub_number": "all", "url": "https://dl.example.org/dump/allCountries.zip"},
                            {"pub_number": "codes", "url": "https://dl.example.org/dump/countryInfo.txt"},
                            {"pub_number": "readme", "url": "https://dl.example.org/dump/readme.txt"},
                        ],
                    }
                ],
                "filter": {"allow_all": True, "public_issuer": True, "data_files": True},
            },
        },
    )


def test_files_downloaded_by_hand_are_taken_in_and_nothing_is_fetched(
    vault: FileVault, tmp_path: Path
) -> None:
    """GeoNames and GEBCO (2026-10-23): hosts whose robots.txt disallows crawlers; the owner downloads
    the files and the library takes them from a folder, making no request at all."""
    import zipfile

    from wsp_cli.doccollections import _Offline  # pyright: ignore[reportPrivateUsage]

    drop = vault.root / "_incoming" / "_manual" / "test-gazetteer"
    drop.mkdir(parents=True)
    with zipfile.ZipFile(drop / "allCountries.zip", "w") as z:
        z.writestr("allCountries.txt", "2988507\tParis\t48.85\t2.35\n" * 200)
    (drop / "countryInfo.txt").write_text("#ISO\tCountry\nFR\tFrance\n" * 50)
    fetch = CollectionFetcher(
        vault.root,
        _Offline(),
        keys={},
        user_agent="t",
        workdir=tmp_path / "work",
        out=io.StringIO(),
        throttle=False,
        offline=True,
        manual_dir=drop,
    )
    report = fetch.run(_manual_spec(), version="2026-10-23")
    assert sorted(d.cand.key for d in report.fetched) == ["gaz:ALL", "gaz:CODES"]
    assert [k for k, _ in report.failed] == ["gaz:README"]  # not downloaded yet: named, with its link
    assert "readme.txt is not in" in report.failed[0][1]
    assert "https://dl.example.org/dump/readme.txt" in report.failed[0][1]
    assert not (drop / "allCountries.zip").exists()  # moved into the cycle
    assert report.planned

    # A copy an earlier run held for review is taken from the quarantine (screened again today).
    from wsp_adapters.doccollection.model import Candidate
    from wsp_core.errors import FetchError

    held_cand = Candidate(
        key="gaz:CODES", url="https://dl.example.org/dump/countryInfo.txt", title="C", issuer="T"
    )
    quarantine = vault.root / "_incoming" / "_quarantine" / "test-gazetteer"
    quarantine.mkdir(parents=True)
    (quarantine / held_cand.file_name).write_text("#ISO\tCountry\n")
    cycle = vault.root / "_incoming" / "test-gazetteer" / "2026-10-24"
    got = fetch._download(_Offline(), held_cand, cycle, ["dl.example.org"])  # pyright: ignore[reportPrivateUsage]
    assert got == cycle / held_cand.file_name
    assert not (quarantine / held_cand.file_name).exists()

    # A zip that does not read whole is refused and left for the owner to download again.
    bad = vault.root / "_incoming" / "_manual" / "bad"
    bad.mkdir()
    (bad / "allCountries.zip").write_bytes(b"<html>503 Service Unavailable</html>")
    fetch.manual_dir = bad
    cand = Candidate(
        key="gaz:all", url="https://dl.example.org/dump/allCountries.zip", title="All", issuer="T"
    )
    with pytest.raises(FetchError, match="does not read whole"):
        fetch._download(_Offline(), cand, tmp_path / "cycle", ["dl.example.org"])  # pyright: ignore[reportPrivateUsage]
    assert (bad / "allCountries.zip").exists()


def _manual_import(
    monkeypatch: pytest.MonkeyPatch,
    vault: FileVault,
    fetch: Any,
    ingest: Any,
) -> argparse.Namespace:
    """``wsp collections manual test-gazetteer --import`` with its fetch and ingest replaced."""
    import sys

    import wsp_cli
    from wsp_cli import bootstrap
    from wsp_cli import doccollections as dc

    class Paths:
        @staticmethod
        def require_vault() -> Path:
            return vault.root

    def layout(_dir: object) -> None:
        return None

    def load(_lay: object) -> dict[str, Any]:
        return {}

    def load_sources(_cfg: object, _lay: object) -> tuple[SourceSpec, ...]:
        return (_manual_spec(),)

    def paths(_cfg: object) -> type[Paths]:
        return Paths

    class FakeOps:
        cmd_ingest = staticmethod(ingest)

    monkeypatch.setattr(bootstrap, "layout", layout)
    monkeypatch.setattr(bootstrap, "load", load)
    monkeypatch.setattr(bootstrap, "load_sources", load_sources)
    monkeypatch.setattr(bootstrap, "paths", paths)
    monkeypatch.setattr(dc, "cmd_fetch", fetch)
    monkeypatch.setattr(wsp_cli, "ops", FakeOps, raising=False)
    monkeypatch.setitem(sys.modules, "wsp_cli.ops", FakeOps)
    return argparse.Namespace(
        config_dir=None, source="test-gazetteer", do_import=True, version=None, sync=False, status=False
    )


def test_a_manual_import_queues_for_the_ingest_lock_ahead_of_the_lanes(
    vault: FileVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-04 trawl: the import tried the ingest lock once a minute without queueing, lost every
    release to the lanes waiting in the queue, and gave up after four hours with the files unsealed.
    It now waits in the queue, ahead of the lanes, as ``--sync`` does: one call, no polling."""
    from wsp_cli import doccollections as dc
    from wsp_cli import manual

    calls: list[argparse.Namespace] = []
    answer = [0]

    def fetch(_args: argparse.Namespace, _out: Any) -> int:
        return dc.EXIT_COMPLETE  # the owner's files moved into the cycle and planned

    def ingest(args: argparse.Namespace, _out: Any) -> int:
        calls.append(args)
        return answer[0]

    args = _manual_import(monkeypatch, vault, fetch, ingest)
    assert dc.cmd_manual(args, io.StringIO()) == 0
    assert len(calls) == 1
    assert calls[0].wait == manual.INGEST_WAIT
    assert calls[0].priority is True
    assert calls[0].source == "test-gazetteer"

    answer[0] = dc.BUSY  # the queue wait ran out: reported busy at once, not polled for hours
    assert dc.cmd_manual(args, io.StringIO()) == dc.BUSY
    assert len(calls) == 2


def test_a_manual_import_rerun_ingests_the_earlier_unsealed_import_first(
    vault: FileVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-04 trawl review: after an import's queue wait ran out, a rerun of --import was refused
    by the fetch (exit 5, "run `wsp ingest`", which does not queue) before any ingest was tried, so
    the files never sealed. The earlier import's ingest now goes first, in the queue, as --sync's."""
    from wsp_cli import doccollections as dc
    from wsp_cli import manual

    spec = _manual_spec()
    plans = vault.root / "_incoming" / "_plans" / dc.PLANS_SUBDIR
    plans.mkdir(parents=True)
    (plans / "test-gazetteer-2026-11-03.json").write_text(
        json.dumps([{"source_id": spec.id, "version": "2026-11-03"}])
    )
    sealed = vault.root / "library" / spec.vault_domain / spec.id / "2026-11-03" / "MANIFEST.json"
    folder = vault.root / "_incoming" / dc.MANUAL_DIR / spec.id
    fetched: list[argparse.Namespace] = []
    calls: list[argparse.Namespace] = []
    answer = [dc.BUSY]

    def fetch(args: argparse.Namespace, _out: Any) -> int:
        fetched.append(args)
        return dc.EXIT_COMPLETE if sealed.is_file() else dc.EXIT_INGEST_PENDING

    def ingest(args: argparse.Namespace, _out: Any) -> int:
        calls.append(args)
        if answer[0] == 0:
            sealed.parent.mkdir(parents=True, exist_ok=True)
            sealed.write_text("{}")
        return answer[0]

    args = _manual_import(monkeypatch, vault, fetch, ingest)
    out = io.StringIO()
    assert dc.cmd_manual(args, out) == dc.BUSY  # the queue is still held: the next rerun tries again
    assert fetched == []
    assert len(calls) == 1
    assert (calls[0].wait, calls[0].priority) == (manual.INGEST_WAIT, True)
    assert "not ingested yet" in out.getvalue()

    answer[0] = 0
    (folder / "countryInfo.txt").write_text("#ISO\tCountry\n")  # a new file waits
    assert dc.cmd_manual(args, io.StringIO()) == 0
    assert len(fetched) == 1  # sealed in the queue, then the new file fetched and ingested
    assert len(calls) == 3
    assert all(c.wait == manual.INGEST_WAIT and c.priority for c in calls)

    out = io.StringIO()
    sealed.unlink()  # unsealed again, and no new file: sealed, nothing fetched
    (folder / "countryInfo.txt").unlink()
    assert dc.cmd_manual(args, out) == 0
    assert len(fetched) == 1
    assert "no new file waits" in out.getvalue()


def test_a_share_mounted_path_is_named_as_windows_names_it(tmp_path: Path) -> None:
    from wsp_cli.doccollections import windows_path

    mounts = tmp_path / "mounts"
    mounts.write_text(
        "C:\\134 /mnt/c 9p rw 0 0\n"
        "//192.168.1.169/Bellerophon /mnt/z cifs rw,vers=3.1.1 0 0\n"
        "tmpfs /tmp tmpfs rw 0 0\n"
    )
    got = windows_path(Path("/mnt/z/whisipedia/_incoming/_manual/geonames"), mounts)
    assert got == "\\\\192.168.1.169\\Bellerophon\\whisipedia\\_incoming\\_manual\\geonames"
    assert windows_path(Path("/home/x"), mounts) is None


def _hdf5(length: int) -> bytes:
    """An HDF5 file's start (superblock version 2) saying it is ``length`` bytes long."""
    head = b"\x89HDF\r\n\x1a\n" + bytes([2, 8, 8, 0]) + (0).to_bytes(8, "little") * 2
    head += length.to_bytes(8, "little") + (48).to_bytes(8, "little") + b"\0" * 4
    return head + b"\0" * (length - len(head))


def test_a_kept_netcdf_grid_is_taken_when_it_is_as_long_as_it_says(tmp_path: Path) -> None:
    """GEBCO (2026-10-23): BODC's link came to serve netCDF-4 (HDF5), kept under the registry's old
    ".zip" name. Its own header gives its length: a cut-off download is refused."""
    from wsp_adapters.doccollection.model import Candidate
    from wsp_cli.doccollections import whole
    from wsp_core.errors import FetchError

    dest = tmp_path / "cycle"
    dest.mkdir()
    grid = Candidate(
        key="g:grid", url="https://bodc.example/grid/", title="Grid", issuer="G", extra={"suffix": ".nc"}
    )
    old_name = grid.file_name.removesuffix(".nc") + ".zip.uncorroborated"
    (dest / old_name).write_bytes(_hdf5(4096))
    assert whole(dest / old_name) == ("hdf5", None)
    fetch = CollectionFetcher(
        tmp_path / "vault",
        _NoNetwork(),
        keys={},
        user_agent="t",
        workdir=tmp_path,
        throttle=False,
        adopt_kept={"g:grid"},
    )
    got = fetch._download(_NoNetwork(), grid, dest, ["bodc.example"])  # pyright: ignore[reportPrivateUsage]
    assert got == dest / grid.file_name
    assert got.stat().st_size == 4096

    got.unlink()
    (dest / old_name).write_bytes(_hdf5(4096)[:3000])  # cut off
    kind, problem = whole(dest / old_name)
    assert kind == "hdf5"
    assert problem is not None
    assert "3,000 bytes where its header says 4,096" in problem
    with pytest.raises(FetchError, match="header says"):
        fetch._download(_NoNetwork(), grid, dest, ["bodc.example"])  # pyright: ignore[reportPrivateUsage]


def test_files_received_from_the_daily_list_stand_for_the_listing_and_withdraw_nothing(
    vault: FileVault, tmp_path: Path
) -> None:
    """wsp collections manual --sync (2026-10-24): the queue's received items are the listing, a
    sample: what they lack (the hundreds not downloaded yet) is never inferred withdrawn."""
    import dataclasses

    from wsp_adapters.doccollection import DocCollectionAdapter
    from wsp_adapters.doccollection.model import Candidate
    from wsp_cli.doccollections import _Offline  # pyright: ignore[reportPrivateUsage]
    from wsp_core.ports import Ctx

    drop = vault.root / "_incoming" / "_manual" / "test-gazetteer"
    drop.mkdir(parents=True)
    (drop / "countryInfo.txt").write_text("#ISO\tCountry\nFR\tFrance\n" * 50)
    got = Candidate(
        key="gaz:CODES", url="https://dl.example.org/dump/countryInfo.txt", title="Codes", issuer="Test"
    )
    spec = _manual_spec()
    access = {**dict(spec.raw["access"]), "discovery": [{"kind": "candidates", "items": [got.to_json()]}]}  # type: ignore[arg-type]
    spec = dataclasses.replace(spec, raw={**spec.raw, "access": access})
    adapter = DocCollectionAdapter(throttle=False)
    ctx = Ctx(source=spec, fetcher=_Offline(), workdir=tmp_path / "w", user_agent="t")
    survey = adapter.survey(ctx, env=adapter.env(ctx))
    assert not survey.listing_complete  # a sample
    fetch = CollectionFetcher(
        vault.root,
        _Offline(),
        keys={},
        user_agent="t",
        workdir=tmp_path / "work",
        out=io.StringIO(),
        throttle=False,
        offline=True,
        manual_dir=drop,
    )
    report = fetch.run(spec, version="2026-10-24")
    assert [d.cand.key for d in report.fetched] == ["gaz:CODES"]
    assert report.failed == []  # nothing else was offered as a document of this run


@contextlib.contextmanager
def _no_share_waits() -> Generator[list[float]]:
    """Retries on the share without their pauses, which are collected instead. ``retrying`` binds
    ``time.sleep`` as a default when it is defined, so patching ``time.sleep`` later changes nothing
    (the test below really waited 30 s)."""
    from wsp_core import nasretry

    pauses: list[float] = []
    defaults = {"sleep": pauses.append, "transient": nasretry.is_transient}
    with patch.object(nasretry.retrying, "__kwdefaults__", defaults):
        yield pauses


def test_share_trouble_while_screening_fails_one_document_not_the_fetch(
    vault: FileVault, tmp_path: Path
) -> None:
    """EAGAIN opening a downloaded file on the share (2026-10-28, an Aozora fetch died on one file after
    28 hours): tried again; if it lasts, that document fails (retried next run) and the rest go on."""
    import wsp_cli.doccollections as dc

    real = dc.screen_document  # pyright: ignore[reportPrivateImportUsage]
    calls: list[str] = []

    def flaky(path: Path, rules: Rules, **kw: Any) -> Any:
        calls.append(path.name)
        if "restricted" in path.name:
            raise BlockingIOError(11, "Resource temporarily unavailable", str(path))
        if path.name.startswith("FM") and calls.count(path.name) == 1:
            raise BlockingIOError(11, "Resource temporarily unavailable", str(path))  # once only
        return real(path, rules, **kw)

    with patch.object(dc, "screen_document", flaky), _no_share_waits():
        report = runner(vault, OBJECTS, tmp_path).run(spec(OBJECTS), version="2026-10-10")
    assert [d.cand.title for d in report.fetched] == ["FM 5 0 2022"]  # second try went through
    (failed,) = report.failed
    assert failed[0] == "test:restricted.pdf"
    assert "the share did not answer" in failed[1]
    assert len(report.held) == 1  # the third document was screened as usual


def test_screen_fails_one_document_on_host_down_not_the_run(vault: FileVault, tmp_path: Path) -> None:
    """Trawl 2026-11-10: the CIFS mount reports the NAS away as a plain OSError (EHOSTDOWN, EIO), which
    escaped ``_screen`` and ended the whole fetch."""
    import errno

    import wsp_cli.doccollections as dc

    real = dc.screen_document  # pyright: ignore[reportPrivateImportUsage]
    calls: list[str] = []

    def flaky(path: Path, rules: Rules, **kw: Any) -> Any:
        calls.append(path.name)
        if "restricted" in path.name:
            raise OSError(errno.EHOSTDOWN, "Host is down", str(path))
        if "FM-5" in path.name and calls.count(path.name) == 1:
            raise OSError(errno.EIO, "Input/output error", str(path))  # once only
        return real(path, rules, **kw)

    with patch.object(dc, "screen_document", flaky), _no_share_waits() as pauses:
        report = runner(vault, OBJECTS, tmp_path).run(spec(OBJECTS), version="2026-10-10")
    assert [d.cand.title for d in report.fetched] == ["FM 5 0 2022"]  # second try went through
    (failed,) = report.failed
    assert failed[0] == "test:restricted.pdf"
    assert "the share did not answer" in failed[1]
    assert "Host is down" in failed[1]
    assert len(report.held) == 1
    assert (
        sum("restricted" in name for name in calls) == 8
    )  # an outage gets the longer wait, then the document fails
    assert sorted(pauses) == sorted([2.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0])  # one EIO, one outage


def test_a_rename_the_share_did_and_then_refused_still_files_the_document(
    vault: FileVault, tmp_path: Path
) -> None:
    """A CIFS reconnect: the server moved the copy into the quarantine, the client got ESTALE. The retry
    found the file gone (FileNotFoundError, not transient) and the whole fetch ended."""
    import errno

    real = Path.replace
    refused: list[str] = []

    def done_then_refused(self: Path, target: Any) -> Any:
        if "unmarked" in self.name and not refused:
            refused.append("unmarked")
            real(self, target)
            raise OSError(errno.ESTALE, "Stale file handle", str(self))
        return real(self, target)

    with patch.object(Path, "replace", done_then_refused), _no_share_waits():
        report = runner(vault, OBJECTS, tmp_path).run(spec(OBJECTS), version="2026-10-10")
    assert refused == ["unmarked"]
    assert report.failed == []
    (held,) = report.held
    assert Path(held["quarantined"]).is_file()
    assert [d.cand.title for d in report.fetched] == ["FM 5 0 2022"]  # the run went on


def test_an_unlink_the_share_did_and_then_refused_skips_the_document_once(
    vault: FileVault, tmp_path: Path
) -> None:
    """The byte-identical branch: the copy is deleted, the share answers ESTALE, the retry finds it gone.
    It is counted as skipped once and not hashed again."""
    import errno

    import wsp_cli.doccollections as dc

    first = runner(vault, OBJECTS, tmp_path).run(spec(OBJECTS), version="2026-10-10")
    held_sha = {d.cand.key: d.sha256 for d in first.fetched}
    (kept,) = first.fetched
    path = tmp_path / "again.pdf"
    path.write_bytes(GOOD)
    real_unlink, real_hash = Path.unlink, dc.fetched_doc  # pyright: ignore[reportPrivateImportUsage]
    unlinks: list[str] = []
    hashes: list[str] = []

    def done_then_refused(self: Path, missing_ok: bool = False) -> None:
        real_unlink(self, missing_ok=missing_ok)
        if self == path:
            unlinks.append(self.name)
            if len(unlinks) == 1:
                raise OSError(errno.ESTALE, "Stale file handle", str(self))

    def counted(*a: Any, **k: Any) -> Any:
        hashes.append("hash")
        return real_hash(*a, **k)

    report = dc.FetchReport("test-pubs", "2026-10-11")
    fetch = runner(vault, OBJECTS, tmp_path)
    with (
        patch.object(Path, "unlink", done_then_refused),
        patch.object(dc, "fetched_doc", counted),
        _no_share_waits(),
    ):
        fetch._screen(kept.cand, path, load_rules(None), report, held_sha)  # pyright: ignore[reportPrivateUsage]
    assert (report.skipped, report.failed, report.fetched) == (1, [], [])
    assert len(unlinks) == 2
    assert hashes == ["hash"]
    assert not path.exists()


def test_a_listing_that_drops_most_held_documents_is_not_believed() -> None:
    """2026-10-26 review #21: one broken listing must not label a whole source withdrawn."""
    from wsp_adapters.doccollection.model import DocumentMeta
    from wsp_cli.doccollections import _mass_withdrawal  # pyright: ignore[reportPrivateUsage]

    held = [DocumentMeta(f"k{i}", f"upstream/k{i}.pdf", "X", f"T{i}") for i in range(20)]
    assert _mass_withdrawal(held, set(), ()) == "the listing leaves out 20 of 20 held documents"
    assert _mass_withdrawal(held, {f"k{i}" for i in range(12)}, ()) is None  # 8 withdrawn: believed
    assert _mass_withdrawal(held[:5], set(), ()) is None  # too few to judge
    assert _mass_withdrawal(held, {f"k{i}" for i in range(9)}, {f"k{i}" for i in range(9, 20)}) is None


def test_a_lagging_cycle_label_is_moved_past_the_newest_sealed_generation(
    vault: FileVault, tmp_path: Path
) -> None:
    """2026-10-26 review #30: a lane's own cycle record (2026-10-12r6) sorted before a generation a
    second lane had sealed (2026-10-20r3), so its new documents were never current."""
    s = spec(OBJECTS)
    fetcher = runner(vault, OBJECTS, tmp_path)
    lib = vault.root / "library" / s.vault_domain / s.id
    for gen in ("2026-10-20r3.b0001", "2026-10-25.b0002", "2026-10-20r3"):
        (lib / gen).mkdir(parents=True)
        (lib / gen / "MANIFEST.json").write_text("{}")
    assert fetcher.free_version(s, "2026-10-12r6.b0001", "b0001") == "2026-10-20r4.b0001"
    assert fetcher.free_version(s, "2026-10-12r6", None) == "2026-10-20r4"
    assert fetcher.free_version(s, "2026-10-28.b0001", "b0001") == "2026-10-28.b0001"  # newer: as asked
    assert (
        fetcher.free_version(s, "2026-10-12.b0003", "b0003") == "2026-10-12.b0003"
    )  # its own partition only


def test_a_listed_file_the_issuer_no_longer_has_does_not_make_the_fetch_partial(
    vault: FileVault, tmp_path: Path
) -> None:
    """2026-10-26 review #29: documents that 404 for good kept a CIA partition 'partial' forever; each
    pass sealed another cycle and the lane behind it never ran."""
    from wsp_cli.doccollections import EXIT_COMPLETE, fetch_exit_code

    s = spec(OBJECTS)
    routes_ = routes(OBJECTS)
    del routes_[f"{BUCKET}/{PREFIX}restricted.pdf"]  # listed, but 404
    fetcher = CollectionFetcher(
        vault.root,
        FakeFetcher(routes_),
        keys={},
        user_agent="test",
        workdir=tmp_path / "work",
        out=io.StringIO(),
        throttle=False,
    )
    report = fetcher.run(s, version="2026-10-10")
    assert report.failed == []
    assert [k for k, _ in report.gone] == ["test:restricted.pdf"]
    assert fetch_exit_code(report) == EXIT_COMPLETE


@pytest.mark.parametrize("unreachable", [False, True])
def test_only_a_robots_txt_that_answers_makes_a_document_gone(
    vault: FileVault, tmp_path: Path, unreachable: bool
) -> None:
    """2026-11-05 deep dive: a robots.txt that could not be read (a 5xx, a timeout) disallows only until
    it is read again, so the document failed (tried next pass); a real Disallow is as good as gone."""
    from wsp_core.errors import RobotsDisallowedError, RobotsUnreachableError

    refused = f"{BUCKET}/{PREFIX}restricted.pdf"

    class Refusing(FakeFetcher):
        def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
            if remote.url == refused:
                cls = RobotsUnreachableError if unreachable else RobotsDisallowedError
                raise cls(f"{refused}: disallowed by robots.txt")
            return super().download(remote, dest)

    fetcher = CollectionFetcher(
        vault.root,
        Refusing(routes(OBJECTS)),
        keys={},
        user_agent="test",
        workdir=tmp_path / "work",
        out=io.StringIO(),
        throttle=False,
    )
    report = fetcher.run(spec(OBJECTS), version="2026-10-10")
    failed, gone = [k for k, _ in report.failed], [k for k, _ in report.gone]
    assert (failed, gone) == ((["test:restricted.pdf"], []) if unreachable else ([], ["test:restricted.pdf"]))


def test_a_document_that_is_always_a_web_page_is_gone_after_a_week_of_tries(tmp_path: Path) -> None:
    """2026-10-29: one CIA document was an HTML page on 19 tries over 8 days and kept its partition
    from ever finishing. Five tries over seven days in a row make it gone; anything else starts over."""
    from datetime import UTC, datetime, timedelta

    from wsp_cli import doccollections as dc

    ledger = tmp_path / "wrong-body-b0003.json"
    start = datetime(2026, 10, 21, tzinfo=UTC)

    def run(day: int, wrong: bool, *, other: bool = False) -> dc.FetchReport:
        report = dc.FetchReport("cia", "v")
        if wrong:
            report.failed.append(("doc", "expected a PDF, got html"))
            report.wrong_body.add("doc")
        if other:
            report.failed.append(("other", "ConnectTimeout"))
        dc.settle_wrong_bodies(report, ledger, start + timedelta(days=day))
        return report

    for day in range(4):
        assert run(day, wrong=True).gone == []  # four tries: not yet
    assert run(5, wrong=True).gone == []  # five tries, but only five days
    report = run(7, wrong=True, other=True)
    assert [k for k, _ in report.gone] == ["doc"]
    assert "6 tries since 2026-10-21" in report.gone[0][1]
    assert report.failed == [("other", "ConnectTimeout")]  # other failures stay failures
    assert dc.fetch_exit_code(report) == dc.EXIT_PARTIAL
    run(8, wrong=False)  # it downloaded (or failed otherwise): the count starts over
    assert run(20, wrong=True).gone == []


def test_failures_another_download_answers_the_same_way_are_permanent_the_rest_transient() -> None:
    from wsp_adapters.doccollection.adapter import WrongBodyError
    from wsp_cli.doccollections import permanent_class, permanent_kind
    from wsp_core.errors import FetchError, NotFoundError
    from wsp_fetch.errors import MirrorsExhaustedError, UnverifiableError

    url = "https://archive.org/download/mcrp0002unse/mcrp0002unse.pdf"

    def exhausted(why: str) -> MirrorsExhaustedError:
        return MirrorsExhaustedError(f"no usable source for {url}: {url}: probe {url} failed after {why}")

    assert permanent_kind(exhausted("1 attempt(s): HTTP 401")) == "refused: HTTP 401"
    assert permanent_kind(exhausted("1 attempt(s): HTTP 403")) == "refused: HTTP 403"
    # Counted by class: 401, 403 and 451 are one refusal; every wrong body is one "not the file".
    assert {permanent_class(f"refused: HTTP {c}") for c in (401, 403, 451)} == {"refused"}
    assert {
        permanent_class(permanent_kind(WrongBodyError(why)) or "")
        for why in ("expected a PDF, got html", "not a document: a login or error page ('Sign in')")
    } == {"not the file"}
    assert permanent_kind(WrongBodyError("not a PDF (missing %PDF- header)")) == (
        "not the file: not a PDF (missing %PDF- header)"
    )
    unverifiable = UnverifiableError(
        f"{url}: upstream publishes no checksum and no second download succeeded ({url}: corroborate "
        f"{url} failed after 6 attempt(s): more than the 11510 bytes expected); first copy kept as x"
    )
    assert permanent_kind(unverifiable) is not None
    # A challenge or maintenance page served with 200 is a wrong body too: counted, but over more tries.
    assert (
        permanent_kind(WrongBodyError("expected a PDF, got html")) == "not the file: expected a PDF, got html"
    )
    # A real HTTP 500-599 and nothing else is a server error: counted, over many more tries and days.
    assert permanent_kind(exhausted("6 attempt(s): HTTP 500")) == "server error, persistent: HTTP 500"
    assert permanent_kind(exhausted("6 attempt(s): HTTP 503")) == "server error, persistent: HTTP 503"
    assert {
        permanent_class(permanent_kind(exhausted(f"6 attempt(s): HTTP {c}")) or "") for c in (500, 599)
    } == {"server error, persistent"}
    direct = FetchError(f"GET {url} failed after 6 attempt(s): HTTP 502")
    direct.__cause__ = FetchError("HTTP 502")
    direct.__cause__.status = 502  # type: ignore[attr-defined]  # as wsp_fetch's retry loop raises it
    assert permanent_kind(direct) == "server error, persistent: HTTP 502"
    throttled = FetchError(f"GET {url} failed after 6 attempt(s): HTTP 502")
    throttled.__cause__ = FetchError("HTTP 429")
    throttled.__cause__.status = 429  # type: ignore[attr-defined]  # the status it carries decides
    for transient in (
        throttled,
        exhausted("6 attempt(s): HTTP 408"),
        exhausted("6 attempt(s): HTTP 425"),
        exhausted("6 attempt(s): HTTP 600"),
        exhausted("6 attempt(s): HTTP 500; https://mirror/x: failed after 6 attempt(s): ConnectError"),
        exhausted("6 attempt(s): HTTP 500; https://mirror/x: failed after 1 attempt(s): HTTP 403"),
        exhausted("6 attempt(s): HTTP 503; https://mirror/x: failed after 2 attempt(s): HTTP 429"),
        FetchError(f"GET {url}: Retry-After 86400s too long"),
        FetchError(f"GET {url}: HTTP 503, Retry-After 86400s too long"),
        OSError("HTTP 500"),  # not a download's answer
        exhausted("6 attempt(s): HTTP 429"),
        exhausted("6 attempt(s): ConnectTimeout: timed out"),
        exhausted("6 attempt(s): ReadTimeout: The read operation timed out"),
        exhausted("1 attempt(s): HTTP 403; https://mirror/x: failed after 6 attempt(s): ConnectError"),
        UnverifiableError(
            f"{url}: no second download succeeded ({url}: failed after 6 attempt(s): HTTP 502)"
        ),
        UnverifiableError(f"{url}: no second download succeeded ({url}: body ended after 5 of 9 bytes)"),
        UnverifiableError(f"{url}: no second download succeeded ({url}: server ignored Range)"),
        UnverifiableError(f"{url}: no second download succeeded ({url}: HTTP 404)"),
        FetchError("HostUnreachable: x took no connection 5 times in a row"),
        OSError(11, "Resource temporarily unavailable"),
        NotFoundError(url),  # gone already: never counted here
    ):
        assert permanent_kind(transient) is None, transient


def test_a_failure_seen_the_same_way_on_runs_in_a_row_no_longer_makes_the_run_partial(
    tmp_path: Path,
) -> None:
    """2026-11-09: NTRS materials (one not-a-PDF), USMC MCPEL (archive.org 401 and 403) and CRS reports
    (three 403s) sealed cycle after cycle and their lanes never finished. A refusal on two runs in a
    row, a wrong body on three, the first at least 2 hours before, from a host that serves good files,
    no longer keeps the run partial; anything else does."""
    from datetime import UTC, datetime, timedelta

    from wsp_cli import doccollections as dc

    ledger = tmp_path / "failed-for-good.json"
    now = datetime(2026, 11, 9, tzinfo=UTC)

    def run(
        hour: float,
        failures: Mapping[str, str | None],
        *,
        tried: int = 10,
        good: int = 1,
    ) -> dc.FetchReport:
        at = now + timedelta(hours=hour)
        report = dc.FetchReport("ntrs", "v", planned=True)
        for key, kind in failures.items():
            report.failed.append((key, f"why {key}"))
            if kind is not None:
                report.permanent[key] = kind
        dc.settle_permanent(
            report,
            ledger,
            at,
            tried=tried,
            host_of=dict.fromkeys(failures, "h"),
            fail_at={key: (at, i) for i, key in enumerate(failures)},
            clean_at={"h": [(at, 1000)] * good},  # a clean take after every failure of the run
        )
        return report

    both = {"a": "refused: HTTP 403", "b": "not the file: x"}
    first = run(0, {"a": "refused: HTTP 403"})
    assert first.given_up == []
    assert dc.fetch_exit_code(first) == dc.EXIT_PARTIAL
    assert run(0.25, both).given_up == []  # two passes of a lane minutes apart: a passing outage?
    second = run(2, {**both, "t": None})
    assert [k for k, _ in second.given_up] == ["a"]  # a refusal: here three tries over 2 hours
    assert "on every one of 3 tries since 2026-11-09T00:00:00+00:00" in second.given_up[0][1]
    assert second.failed == [("b", "why b"), ("t", "why t")]  # b: two tries; t is transient
    assert dc.fetch_exit_code(second) == dc.EXIT_PARTIAL
    third = run(2.5, both)
    assert sorted(k for k, _ in third.given_up) == ["a", "b"]
    assert third.failed == []
    assert dc.fetch_exit_code(third) == dc.EXIT_COMPLETE
    assert third.given_up_ledger == str(ledger)
    seen = json.loads(ledger.read_text())
    assert seen["a"]["tries"] == 4  # every document, with its count
    assert seen["a"]["given_up"] == "2026-11-09T02:00:00+00:00"  # kept from the first time
    other = run(3, {"a": "refused: HTTP 403", "b": "refused: HTTP 401"})  # b fails otherwise now
    assert sorted(k for k, _ in other.given_up) == ["a", "b"]  # given up before: stays given up
    run(4, {})  # a run without them (fetched, or not tried): the counts start over
    assert run(6.5, {"a": "refused: HTTP 403"}).given_up == []

    # Two refusals 2 hours apart are enough; a wrong body takes a third.
    run(10, {"r": "refused: HTTP 403", "w": "not the file: x"})
    assert [k for k, _ in run(12, {"r": "refused: HTTP 403", "w": "not the file: x"}).given_up] == ["r"]

    # A site-wide refusal is not single documents: more than 10, or 2% of the downloads tried, stay
    # failures, whatever the size of the listing.
    many = {f"k{i}": "refused: HTTP 403" for i in range(11)}
    run(20, many, tried=11)
    over = run(23, many, tried=11)
    assert over.given_up == []
    assert len(over.failed) == 11
    assert over.permanent_over == 11
    assert run(24, many, tried=1_000).given_up == []  # blocked runs were not counted: from 1 again
    assert sorted(k for k, _ in run(26, many, tried=1_000).given_up) == sorted(many)


PAGE = b"<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>Checking</body></html>"
SIBLINGS = [
    ("a-1990.pdf", make_pdf([PUBLIC_A, ["Alloys"]])),
    ("b-1991.pdf", make_pdf([PUBLIC_A, ["Fatigue"]])),
    ("c-1992.pdf", make_pdf([PUBLIC_A, ["Fracture"]])),
]
NEW = [(f"n{i}-2026.pdf", make_pdf([PUBLIC_A, [f"Report {i}"]])) for i in range(1, 6)]


class _Timeline:
    """A source over a lane's passes (tools/lanes/lane-run.sh: retries 15 minutes apart, rounds an
    hour apart), on the clock each pass gives: a plain list of URLs with no size and no MD5, like
    NTRS and CRS (held documents are not downloaded again for REFETCH_AFTER). A document whose bytes
    are None is always an HTML page; ``blocked`` serves a challenge page for every request (probes
    too), and ``block_after`` starts the block in the middle of a pass, after that many downloads."""

    def __init__(self, vault: FileVault, tmp_path: Path, docs: list[tuple[str, bytes | None]]) -> None:
        from datetime import UTC, datetime

        self.vault, self.tmp = vault, tmp_path
        self.docs = docs
        self.blocked = False
        self.block_after: int | None = None
        self.status: dict[str, int] = {}  # file name -> the HTTP status its every download answers
        self.outage: int | None = None  # every request (probes too) answers this HTTP status
        self.t0 = datetime(2026, 11, 9, 6, tzinfo=UTC)

    def spec(self) -> SourceSpec:
        s = spec(OBJECTS)
        items = [{"url": f"{BUCKET}/{PREFIX}{n}"} for n, _ in self.docs]
        access: dict[str, Any] = {
            **cast(dict[str, Any], s.raw["access"]),
            "discovery": [{"kind": "list", "items": items}],
        }
        return replace(s, raw={**s.raw, "access": access})

    def fetch(self, hour: float) -> Any:
        from datetime import timedelta

        from wsp_fetch.errors import MirrorsExhaustedError

        timeline = self
        served = [0]
        body = {f"{BUCKET}/{PREFIX}{n}": b for n, b in self.docs}

        def page() -> bool:
            return timeline.blocked or (
                timeline.block_after is not None and served[0] >= timeline.block_after
            )

        def answer(url: str) -> None:
            code = timeline.outage or timeline.status.get(url.rsplit("/", 1)[-1])
            if code is not None:
                raise MirrorsExhaustedError(
                    f"no usable source for {url}: {url}: probe {url} failed after 6 attempt(s): HTTP {code}"
                )

        class Site(FakeFetcher):
            def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
                answer(remote.url)
                good = body.get(remote.url)
                if page() or good is None:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(PAGE)
                    return FetchResult(dest, len(PAGE), "0" * 64, "0" * 64)
                served[0] += 1
                return super().download(remote, dest)

            def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
                answer(url)
                return PAGE if page() else super().get_bytes(url, max_bytes=max_bytes)

        fetcher = CollectionFetcher(
            self.vault.root,
            Site({u: b for u, b in body.items() if b is not None}),
            keys={},
            user_agent="test",
            workdir=self.tmp / "work",
            out=io.StringIO(),
            throttle=False,
        )
        fetcher.clock = lambda: self.t0 + timedelta(hours=hour)
        s = self.spec()
        report = fetcher.run(s, version="2026-11-09")
        if report.planned:
            ingest(self.vault, s)
        return report


def test_a_web_page_last_on_a_listing_without_sizes_is_given_up_through_a_probe(
    vault: FileVault, tmp_path: Path
) -> None:
    """Review of 2026-11-09, rounds 3 and 4: NTRS lists no size or MD5, so a later pass downloads
    only the failing document, last on the list. Once it is due, one held document of its host is
    downloaded again and found the same, after the failure: the host is well, the document is given
    up (lane-run.sh's passes: 0, 15 and 30 minutes, then the next round)."""
    from wsp_cli.doccollections import EXIT_PARTIAL, fetch_exit_code

    site = _Timeline(vault, tmp_path, [*SIBLINGS, ("html-1989.pdf", None)])
    first = site.fetch(0)  # the siblings download, then the page fails
    assert len(first.fetched) == 3
    [(bad, _)] = first.failed
    assert first.permanent_hosts == {bad: "bucket.example.gov"}
    for hour in (0.25, 0.5, 1.5):  # passes that download only the page
        report = site.fetch(hour)
        assert report.fetched == []
        assert [k for k, _ in report.failed] == [bad]
        assert fetch_exit_code(report) == EXIT_PARTIAL
    given = site.fetch(2.5)  # 5 tries over 2.5 h: due; a sibling downloaded again is the same
    assert [k for k, _ in given.given_up] == [bad]
    assert given.failed == []
    assert fetch_exit_code(given) != EXIT_PARTIAL
    later = site.fetch(30)  # still failing: stays given up, its record kept, retried stamped
    assert [k for k, _ in later.given_up] == [bad]
    seen = json.loads((tmp_path / "work" / "test-pubs" / "failed-for-good.json").read_text())
    assert seen[bad]["given_up"] == "2026-11-09T08:30:00+00:00"
    assert seen[bad]["retried"] == "2026-11-10T12:00:00+00:00"


def test_a_block_that_starts_mid_pass_after_clean_takes_gives_up_nothing(
    vault: FileVault, tmp_path: Path
) -> None:
    """Review round 4: a CRS refresh took two new reports, then Cloudflare blocked for three hours;
    the clean takes before the block proved the three reports after it, given up two hours later. A
    take before a failure proves nothing, and the probe after it meets the block: nothing is given
    up, and once the block lifts every report is fetched."""
    site = _Timeline(vault, tmp_path, list(SIBLINGS))
    assert len(site.fetch(0).fetched) == 3  # held from an earlier week
    more: list[tuple[str, bytes | None]] = [*SIBLINGS, *NEW]
    site.docs = more
    site.block_after = 2
    first = site.fetch(10)
    assert len(first.fetched) == 2
    assert len(first.failed) == 3
    site.block_after = 0  # the block goes on
    for hour in (10.25, 10.5, 11.5, 12.5, 12.75, 13):
        report = site.fetch(hour)
        assert report.given_up == [], hour
        assert len(report.failed) == 3
    site.block_after = None
    lifted = site.fetch(13.5)
    assert len(lifted.fetched) == 3
    assert lifted.failed == []
    assert lifted.given_up == []


def test_a_block_from_before_the_first_failure_gives_up_nothing_until_the_host_serves_again(
    vault: FileVault, tmp_path: Path
) -> None:
    """Every request a challenge page from the first pass on: every document fails the same way on
    every pass, counted, none given up (nothing held to probe). Once the block lifts the siblings
    download (before the page on the list: no proof yet); the next pass's probe proves the host."""
    from wsp_cli.doccollections import EXIT_PARTIAL, fetch_exit_code

    site = _Timeline(vault, tmp_path, [*SIBLINGS, ("html-1989.pdf", None)])
    site.blocked = True
    for hour in (0, 0.25, 0.5, 1.5, 2.5, 3.5, 6, 12, 24):
        report = site.fetch(hour)
        assert report.given_up == [], hour
        assert len(report.failed) == 4
        assert fetch_exit_code(report) == EXIT_PARTIAL
    seen = json.loads((tmp_path / "work" / "test-pubs" / "failed-for-good.json").read_text())
    [bad] = [k for k in seen if "html-1989" in k]
    assert seen[bad]["tries"] == 9  # counted all along; only not given up
    site.blocked = False
    cleared = site.fetch(25)
    assert len(cleared.fetched) == 3
    assert cleared.given_up == []  # the siblings came before the page: no take after it yet
    proven = site.fetch(25.25)
    assert [k for k, _ in proven.given_up] == [bad]


def test_given_up_documents_are_due_for_another_try_once_a_month(tmp_path: Path) -> None:
    """A fetch that tries them again stamps ``retried``; a refresh that never fetched them (the
    source's lock held, a crash) stamps nothing, so they stay due."""
    from datetime import UTC, datetime, timedelta

    from wsp_cli import doccollections as dc

    now = datetime(2026, 12, 20, tzinfo=UTC)
    old = (now - timedelta(days=40)).isoformat(timespec="seconds")
    new = (now - timedelta(days=3)).isoformat(timespec="seconds")
    ledgers = {
        "usmc-mcpel/failed-for-good.json": {"a": {"given_up": old, "tries": 9, "sig": "s", "first": old}},
        "cia-reading-room/failed-for-good-b0003.json": {
            "b": {"given_up": old, "retried": old, "tries": 5, "sig": "s", "first": old}
        },
        "nist-csrc/failed-for-good.json": {
            "e": {"given_up": old, "retried": new, "tries": 7, "sig": "s", "first": old}
        },
        "crs-reports/failed-for-good.json": {"c": {"given_up": new, "tries": 3, "sig": "s", "first": new}},
        "ntrs/failed-for-good.json": {"d": {"tries": 1, "sig": "s", "first": old}},  # never given up
    }
    for name, entries in ledgers.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(entries))
    assert dc.due_for_retry(tmp_path, now) == [("cia-reading-room", "b0003"), ("usmc-mcpel", "")]
    assert dc.due_for_retry(tmp_path, now) == [("cia-reading-room", "b0003"), ("usmc-mcpel", "")]  # unchanged
    assert dc.due_for_retry(tmp_path, now + timedelta(days=31)) == [
        ("cia-reading-room", "b0003"),
        ("crs-reports", ""),
        ("nist-csrc", ""),
        ("usmc-mcpel", ""),
    ]


def _http(code: int | str) -> Exception:
    """A download's failure as wsp_fetch words it (itu-recommendations, 2026-11-13)."""
    from wsp_fetch.errors import MirrorsExhaustedError

    url = "https://www.itu.int/rec/dologin_pub.asp?lang=e&id=T-REC-G.718-200806-I!!PDF-E&type=items"
    why = f"HTTP {code}" if isinstance(code, int) else code
    return MirrorsExhaustedError(
        f"no usable source for {url}: {url}: probe {url} failed after 6 attempt(s): {why}"
    )


class _Passes:
    """Fetch runs of one source settled one after another (``settle_permanent``), each at a day since
    2026-11-09: what failed as the download raised it (classified by ``permanent_kind``, as a run
    does), all from host ``h``, which made ``good`` clean takes after every failure of the run."""

    def __init__(self, ledger: Path) -> None:
        self.ledger = ledger

    def run(
        self,
        day: float,
        failures: Mapping[str, BaseException],
        *,
        tried: int = 10,
        listed: int = 0,
        good: int = 1,
        probe: Callable[[str], tuple[int, tuple[datetime, int] | None]] | None = None,
    ) -> dc.FetchReport:
        at = datetime(2026, 11, 9, tzinfo=UTC) + timedelta(days=day)
        report = dc.FetchReport("itu-recommendations", "v", planned=True)
        for key, exc in failures.items():
            report.failed.append((key, f"why {key}"))
            if (kind := dc.permanent_kind(exc)) is not None:
                report.permanent[key] = kind
        dc.settle_permanent(
            report,
            self.ledger,
            at,
            tried=tried,
            listed=listed,
            host_of=dict.fromkeys(failures, "h"),
            fail_at={key: (at, i) for i, key in enumerate(failures)},
            clean_at={"h": [(at, 1000)] * good},
            probe=probe,
        )
        return report

    def seen(self) -> dict[str, dict[str, Any]]:
        return json.loads(self.ledger.read_text()) if self.ledger.exists() else {}


HALF_DAYS = [d / 2 for d in range(13)]  # 13 runs over 6 days: the 10th at 4.5 days, the 11th at 5


def test_a_server_error_after_the_hosts_clean_take_does_not_unprove_a_refusal_before_it(
    tmp_path: Path,
) -> None:
    """2026-11-13 review: proof was looked for once per host, after the latest failure of all its due
    documents, so a 500 that came after the host's last clean take kept a refused document, which the
    code before gave up, failing. Each crowd is proven by its own latest failure.

    Fails on the code before this rule: it gave ``s`` no record at all (asserted below)."""
    at = datetime(2026, 11, 20, tzinfo=UTC)
    long_ago = (at - timedelta(days=9)).isoformat(timespec="seconds")
    ledger = tmp_path / "failed-for-good.json"
    ledger.write_text(
        json.dumps(
            {
                "r": {"first": long_ago, "tries": 9, "sig": "refused: HTTP 403"},
                "s": {"first": long_ago, "tries": 12, "sig": f"{dc.SERVER_ERROR}: HTTP 500"},
            }
        )
    )
    report = dc.FetchReport("itu-recommendations", "v", planned=True)
    report.failed = [("r", "why r"), ("s", "why s")]
    report.permanent = {"r": "refused: HTTP 403", "s": f"{dc.SERVER_ERROR}: HTTP 500"}
    dc.settle_permanent(
        report,
        ledger,
        at,
        tried=10,
        host_of={"r": "h", "s": "h"},
        fail_at={"r": (at, 0), "s": (at, 2)},
        clean_at={"h": [(at, 1)]},  # after the refusal, before the server error
    )
    assert [key for key, _ in report.given_up] == ["r"]
    assert [key for key, _ in report.failed] == ["s"]  # unproven: its count is kept
    assert json.loads(ledger.read_text())["s"]["tries"] == 13


def test_a_503_whose_server_sent_retry_after_is_not_a_persistent_server_error() -> None:
    """wsp_fetch says so in the text (``test_a_failure_whose_server_sent_retry_after_says_so``); the
    same failure without it is one. The first half is a guard: it holds on the code before too."""
    assert dc.permanent_kind(_http("HTTP 503 (Retry-After sent)")) is None
    assert dc.permanent_kind(_http(503)) == f"{dc.SERVER_ERROR}: HTTP 503"


def test_a_host_whose_every_document_answers_5xx_gives_up_none_however_long(tmp_path: Path) -> None:
    """A site-wide outage is never single documents: with nothing good from the host after the
    failures (no clean take; held documents downloaded again fail too, or none is held), or with more
    such documents than the cap, HTTP 500 on 13 runs over 6 days gives up nothing.

    Fails on the code before: it counted no 5xx at all, so ``permanent_over`` was 0 and the ledger
    held no count."""
    down = {f"k{i}": _http(500) for i in range(5)}
    probed = _Passes(tmp_path / "probed.json")  # held documents of the host are probed: they fail too
    alone = _Passes(tmp_path / "alone.json")  # nothing held to probe: unproven
    capped = _Passes(tmp_path / "capped.json")  # the host serves, but too many documents answer 5xx
    many = {f"k{i}": _http(503) for i in range(11)}
    itu = {f"k{i}": _http(500) for i in range(284)}  # the real run: 284 failing of some 320 tried
    itu_reports: list[dc.FetchReport] = []
    for day in HALF_DAYS:
        itu_reports.append(_Passes(tmp_path / "itu.json").run(day, itu, tried=320))
        for report in (
            probed.run(day, down, good=0, probe=lambda _host: (2, None)),
            alone.run(day, down, good=0),
            capped.run(day, many, tried=11),
            itu_reports[-1],
        ):
            assert report.given_up == [], day
            assert dc.fetch_exit_code(report) == dc.EXIT_PARTIAL
    report = itu_reports[-1]
    assert len(report.failed) == 284
    assert report.permanent_over == 284
    last = alone.run(6.5, down, good=0)
    assert last.given_up == []
    assert last.permanent_over == 5  # due, and said so: only unproven
    assert {(e["tries"], e["first"]) for e in alone.seen().values()} == {(14, "2026-11-09T00:00:00+00:00")}
    # A probe that found nothing good is a block: the counts started over the day they were due.
    assert {(e["tries"], e["first"]) for e in probed.seen().values()} == {(2, "2026-11-14T12:00:00+00:00")}
    assert capped.seen() == {}
    assert capped.run(6.5, many, tried=11).permanent_over == 11


def test_the_itu_crowd_of_persistent_5xx_is_given_up_when_the_host_served_on_three_days(
    tmp_path: Path,
) -> None:
    """2026-11-13: the 284 ITU documents are 284 of some 320 downloads tried on a later pass (cap 10),
    so their counts started over on every run and the rule above never released them. Counted against
    the 8,500 documents the run listed (3.3 %, within SERVER_ERROR_SHARE) they keep their counts, and
    are given up after five days and ten runs when the host proved itself after their failures on
    every run, on three separate days at least.

    Fails on the code before: ``listed`` was not looked at, the crowd was reset on every run."""
    itu = {f"k{i}": _http(500) for i in range(284)}
    runs = _Passes(tmp_path / "itu.json")
    for day in HALF_DAYS[:10]:
        report = runs.run(day, itu, tried=320, listed=8500)
        assert report.given_up == [], day
        assert len(report.failed) == 284
    assert {e["tries"] for e in runs.seen().values()} == {10}
    assert runs.seen()["k0"]["proved"] == ["2026-11-11", "2026-11-12", "2026-11-13"]
    given = runs.run(5, itu, tried=320, listed=8500)
    assert len(given.given_up) == 284
    assert given.failed == []
    assert given.permanent_over == 0
    assert dc.fetch_exit_code(given) == dc.EXIT_COMPLETE
    assert runs.run(5.5, itu, tried=284, listed=8500).failed == []  # still given up, still a crowd

    # As the real passes are: nothing fetched after the failures, the host proven by held documents
    # downloaded again and found the same.
    probed = _Passes(tmp_path / "probed.json")
    for n, day in enumerate(HALF_DAYS[:11]):
        at = datetime(2026, 11, 9, tzinfo=UTC) + timedelta(days=day)
        report = probed.run(
            day, itu, tried=284, listed=8500, good=0, probe=lambda _host, at=at: (1, (at, 2000))
        )
        assert len(report.given_up) == (284 if n == 10 else 0), day

    for name, listed in (("unknown.json", 0), ("small.json", 2000)):
        small = _Passes(tmp_path / name)
        for day in HALF_DAYS:
            assert small.run(day, itu, tried=320, listed=listed).given_up == [], (name, day)
        assert small.seen() == {}


def test_a_crowd_of_5xx_that_is_fresh_or_most_of_the_source_or_unproven_gives_up_none(
    tmp_path: Path,
) -> None:
    """The wider bound gives up nothing in an outage: failures younger than five days or ten runs;
    most of the source failing (6,000 of 8,500: the host, not documents; counts start over); no proof
    that the host serves (nothing good after the failures, nothing held to download again: counts kept,
    nothing given up); held documents that fail too (a block: counts start over); proof on fewer than
    three separate days.

    Fails on the code before for the kept counts (it reset them all); the "nothing given up" lines are
    guards of the wider bound."""
    itu = {f"k{i}": _http(500) for i in range(284)}
    fresh = _Passes(tmp_path / "fresh.json")
    assert fresh.run(0, itu, tried=320, listed=8500).given_up == []
    for day in [d / 6 for d in range(1, 15)]:  # 15 runs in under three days
        assert fresh.run(day, itu, tried=320, listed=8500).given_up == [], day
    assert {e["tries"] for e in fresh.seen().values()} == {15}

    most = _Passes(tmp_path / "most.json")
    outage = {f"k{i}": _http(500) for i in range(6000)}
    for day in HALF_DAYS:
        report = most.run(day, outage, tried=6100, listed=8500)
        assert report.given_up == [], day
        assert report.permanent_over == 6000
    assert most.seen() == {}
    over = _Passes(tmp_path / "over.json")  # just above the share (850 of 8,500)
    for day in HALF_DAYS:
        assert over.run(day, {f"k{i}": _http(500) for i in range(851)}, tried=900, listed=8500).given_up == []
    assert over.seen() == {}

    unproven = _Passes(tmp_path / "unproven.json")
    for day in HALF_DAYS:
        assert unproven.run(day, itu, tried=284, listed=8500, good=0).given_up == [], day
    last = unproven.run(6.5, itu, tried=284, listed=8500, good=0)
    assert last.given_up == []
    assert last.permanent_over == 284
    assert {e["tries"] for e in unproven.seen().values()} == {14}
    assert unproven.seen()["k0"]["proved"] == []
    # ... and such a crowd is not given up on one day's proof by a full pass, whose cap (2 % of 8,500
    # downloads) it is under: its counts were kept as a crowd's, and need the three days.
    full = unproven.run(7, itu, tried=8500, listed=8500)
    assert full.given_up == []
    assert full.permanent_over == 284
    assert unproven.seen()["k0"]["proved"] == ["2026-11-16"]

    blocked = _Passes(tmp_path / "blocked.json")  # held documents downloaded again fail too
    for day in HALF_DAYS:
        report = blocked.run(day, itu, tried=284, listed=8500, good=0, probe=lambda _host: (2, None))
        assert report.given_up == [], day
    assert blocked.seen() == {}
    # ... also on the day the documents were due, after ten proven runs: the block starts them over.
    late = _Passes(tmp_path / "late.json")
    for day in HALF_DAYS[:10]:
        late.run(day, itu, tried=284, listed=8500)
    assert late.run(5, itu, tried=284, listed=8500, good=0, probe=lambda _host: (2, None)).given_up == []
    assert late.seen() == {}

    two = _Passes(tmp_path / "two.json")  # the host shown to serve on two days only
    for day in HALF_DAYS[:11]:
        report = two.run(day, itu, tried=284, listed=8500, good=1 if day in (0, 5) else 0)
        assert report.given_up == [], day
    assert two.seen()["k0"]["proved"] == ["2026-11-09", "2026-11-14"]
    assert two.run(5.5, itu, tried=284, listed=8500, good=0).permanent_over == 284
    assert len(two.run(6, itu, tried=284, listed=8500).given_up) == 284  # a third day

    # Documents that were single ones (under the cap, no days recorded) joined by a crowd on the day
    # they are due: not given up that day.
    joined = _Passes(tmp_path / "joined.json")
    few = {f"k{i}": _http(500) for i in range(5)}
    for day in HALF_DAYS[:10]:
        joined.run(day, few, tried=320, listed=8500)
    assert joined.run(5, itu, tried=320, listed=8500).given_up == []


def test_single_documents_answering_5xx_for_five_days_and_ten_tries_are_given_up(tmp_path: Path) -> None:
    """2026-11-13: some 284 ITU recommendations answer HTTP 500 on every pass since 2026-11-09 while
    8,220 from the same host are held, and the lane never finishes. A document whose every download
    answers a real HTTP 500-599 on ten runs in a row, the first at least five days before, from a host
    that made a clean take after its latest failure, is failed for good; fewer tries or days are not.

    Fails on the code before: a 5xx was never counted, so nothing was given up."""
    runs = _Passes(tmp_path / "failed-for-good.json")
    failing = {"p": _http(500), "q": _http(502), "t": _http(429)}
    for day in HALF_DAYS[:10]:
        report = runs.run(day, failing)
        assert report.given_up == [], day  # the 10th try is at 4.5 days
        assert len(report.failed) == 3
    given = runs.run(5, failing)
    assert sorted(k for k, _ in given.given_up) == ["p", "q"]
    assert "on every one of 11 tries since 2026-11-09T00:00:00+00:00" in given.given_up[0][1]
    assert given.failed == [("t", "why t")]  # a 429 stays a failure
    seen = runs.seen()
    assert dc.permanent_class(seen["p"]["sig"]) == "server error, persistent"
    assert seen["q"] == {
        "first": "2026-11-09T00:00:00+00:00",
        "given_up": "2026-11-14T00:00:00+00:00",
        "retried": "2026-11-14T00:00:00+00:00",
        "sig": "server error, persistent: HTTP 502",
        "tries": 11,
    }
    done = runs.run(5.5, {"p": _http(500), "q": _http(503)})  # another 5xx: the same class
    assert done.failed == []
    assert dc.fetch_exit_code(done) == dc.EXIT_COMPLETE

    quick = _Passes(tmp_path / "quick.json")  # ten tries (and more) in two days
    for day in [d / 6 for d in range(13)]:
        assert quick.run(day, {"p": _http(500)}).given_up == [], day
    assert quick.seen()["p"]["tries"] == 13
    few = _Passes(tmp_path / "few.json")  # six days, four tries
    for day in (0, 2, 4, 6):
        assert few.run(day, {"p": _http(500)}).given_up == [], day
    assert few.seen()["p"]["tries"] == 4

    # The thresholds of the other classes are as they were: a refusal beside it takes two tries.
    both = _Passes(tmp_path / "both.json")
    both.run(0, {"r": _http(403), "p": _http(500)})
    assert [k for k, _ in both.run(1, {"r": _http(403), "p": _http(500)}).given_up] == ["r"]
    # More 5xx than the cap never keep a single refusal from being given up, or the other way round.
    crowd = _Passes(tmp_path / "crowd.json")
    busy = {"r": _http(403), **{f"k{i}": _http(500) for i in range(40)}}
    crowd.run(0, busy)
    mixed = crowd.run(1, busy)
    assert [k for k, _ in mixed.given_up] == ["r"]
    assert len(mixed.failed) == 40


def test_a_throttle_a_timeout_or_an_unreachable_host_is_never_a_persistent_server_error(
    tmp_path: Path,
) -> None:
    """Only a real HTTP 500-599 counts: 429, 408, 425, a timeout, a refused connection, a name that
    does not resolve, a robots.txt that could not be read, a 5xx beside another fault stay transient on
    13 runs over 6 days with the host proven each time, and leave no record.

    A guard for the transient half (it passes on the code before); the 5xx document beside them, given
    up on the same runs, fails on the code before and shows that the runs were enough."""
    from wsp_core.errors import FetchError, RobotsUnreachableError

    transient: dict[str, BaseException] = {
        "429": _http(429),
        "408": _http(408),
        "425": _http(425),
        "read": _http("ReadTimeout: The read operation timed out"),
        "connect": _http("ConnectError: [Errno 111] Connection refused"),
        "dns": _http("ConnectError: [Errno -2] Name or service not known"),
        "robots": RobotsUnreachableError("https://www.itu.int/robots.txt could not be read: HTTP 503"),
        "mixed": _http("HTTP 500; https://mirror/x: failed after 6 attempt(s): ConnectTimeout"),
        "later": FetchError("GET https://www.itu.int/x: HTTP 503, Retry-After 86400s too long"),
        "down": FetchError("HostUnreachable: www.itu.int took no connection 5 times in a row"),
    }
    for exc in transient.values():
        assert dc.permanent_kind(exc) is None, exc
    runs = _Passes(tmp_path / "failed-for-good.json")
    report = dc.FetchReport("itu-recommendations", "v")
    for day in HALF_DAYS:
        report = runs.run(day, {**transient, "p": _http(500)}, tried=100)
    assert [k for k, _ in report.given_up] == ["p"]
    assert sorted(k for k, _ in report.failed) == sorted(transient)
    assert list(runs.seen()) == ["p"]
    assert report.permanent_over == 0


def test_a_5xx_streak_broken_by_another_failure_or_a_download_starts_over(tmp_path: Path) -> None:
    """Nine tries over 4.5 days, then one run where the document is refused, times out, or is not
    among the failures (it downloaded): the count and the five days start again from the next 5xx.

    Fails on the code before: no 5xx streak was recorded, and none was ever given up at its end."""
    breaks = (("refused", _http(403)), ("timeout", _http("ReadTimeout: timed out")), ("ok", None))
    for name, between in breaks:
        runs = _Passes(tmp_path / f"{name}.json")
        for day in HALF_DAYS[:9]:
            runs.run(day, {"p": _http(500)})
        assert runs.seen()["p"]["tries"] == 9
        assert runs.run(4.5, {} if between is None else {"p": between}).given_up == []
        again = [5 + d / 2 for d in range(11)]  # from day 5: the 10th try at 9.5, five days at 10
        for day in again[:10]:
            assert runs.run(day, {"p": _http(500)}).given_up == [], (name, day)
        assert (runs.seen()["p"]["tries"], runs.seen()["p"]["first"]) == (10, "2026-11-14T00:00:00+00:00")
        assert [k for k, _ in runs.run(again[10], {"p": _http(500)}).given_up] == ["p"], name


def test_a_ledger_record_without_its_first_failure_starts_its_five_days_when_first_seen(
    tmp_path: Path,
) -> None:
    """A record written without ``first`` (or without ``tries``) by other code or by hand does not
    stop the run, and its days count from the run that first sees it, whatever its count says.

    Fails on the code before: the 5xx was not counted, the record was left as it was (no ``first``
    written) and the document was never given up."""
    runs = _Passes(tmp_path / "failed-for-good.json")
    runs.ledger.write_text(
        json.dumps({"p": {"sig": "server error, persistent: HTTP 500", "tries": 50}, "q": {"sig": "refused"}})
    )
    failing = {"p": _http(500), "q": _http(403)}
    assert runs.run(0, failing).given_up == []
    assert runs.seen()["p"] == {
        "first": "2026-11-09T00:00:00+00:00",
        "sig": "server error, persistent: HTTP 500",
        "tries": 51,
    }
    assert runs.seen()["q"]["tries"] == 1
    assert runs.run(4.9, {"p": _http(500)}).given_up == []
    assert [k for k, _ in runs.run(5, {"p": _http(500)}).given_up] == ["p"]


def test_a_persistent_server_error_given_up_is_tried_again_by_the_monthly_retry(tmp_path: Path) -> None:
    """The weekly refresh's ``wsp collections given-up`` lists its source 30 days after it was given
    up (or last tried); a retry that answers 5xx again keeps it given up and records the try, and one
    that downloads it clears its record.

    Fails on the code before: the document was never given up, so never listed."""
    runs = _Passes(tmp_path / "itu-recommendations" / "failed-for-good.json")
    report = [runs.run(day, {"p": _http(500)}) for day in HALF_DAYS[:11]][-1]
    assert [k for k, _ in report.given_up] == ["p"]  # at day 5
    start = datetime(2026, 11, 9, tzinfo=UTC)
    assert dc.due_for_retry(tmp_path, start + timedelta(days=34)) == []
    assert dc.due_for_retry(tmp_path, start + timedelta(days=35)) == [("itu-recommendations", "")]
    # The retry, alone and unproven (no clean take, nothing to probe): still given up, the try recorded.
    retry = runs.run(36, {"p": _http(500)}, good=0)
    assert [k for k, _ in retry.given_up] == ["p"]
    assert retry.failed == []
    assert runs.seen()["p"]["given_up"] == "2026-11-14T00:00:00+00:00"
    assert runs.seen()["p"]["retried"] == "2026-12-15T00:00:00+00:00"
    assert dc.due_for_retry(tmp_path, start + timedelta(days=40)) == []
    assert dc.due_for_retry(tmp_path, start + timedelta(days=66)) == [("itu-recommendations", "")]
    runs.run(67, {})  # it downloaded: no record left
    assert runs.seen() == {}
    assert dc.due_for_retry(tmp_path, start + timedelta(days=200)) == []


def test_a_document_answering_500_beside_held_siblings_is_given_up_through_a_probe(
    vault: FileVault, tmp_path: Path
) -> None:
    """The ITU shape through a whole fetch: the siblings are held, each pass downloads only the
    document that answers HTTP 500, and a held sibling downloaded again after it proves the host. An
    outage (every request 500, probes too) on a pass counts nothing and starts the streak over.

    Fails on the code before: the 500 was never counted, and every pass stayed partial."""
    from wsp_cli.doccollections import EXIT_PARTIAL, fetch_exit_code

    site = _Timeline(vault, tmp_path, [*SIBLINGS, ("down-1989.pdf", None)])
    site.status = {"down-1989.pdf": 500}
    first = site.fetch(0)
    assert len(first.fetched) == 3
    [(bad, why)] = first.failed
    assert "HTTP 500" in why
    for half in range(1, 10):  # ten tries by day 4.5
        report = site.fetch(half * 12)
        assert [k for k, _ in report.failed] == [bad], half
        assert report.given_up == []
        assert fetch_exit_code(report) == EXIT_PARTIAL
    site.outage = 500  # day 5: the whole host answers 500, the held siblings too
    assert site.fetch(120).given_up == []
    ledger = tmp_path / "work" / "test-pubs" / "failed-for-good.json"
    assert json.loads(ledger.read_text()) == {}
    site.outage = None
    for half in range(11, 21):  # from day 5.5: ten tries by day 10
        report = site.fetch(half * 12)
        assert report.given_up == [], half
    given = site.fetch(21 * 12)  # five days after day 5.5
    assert [k for k, _ in given.given_up] == [bad]
    assert given.failed == []
    assert fetch_exit_code(given) != EXIT_PARTIAL
    seen = json.loads(ledger.read_text())
    assert seen[bad]["sig"] == "server error, persistent: HTTP 500"
    assert (seen[bad]["tries"], seen[bad]["first"]) == (11, "2026-11-14T18:00:00+00:00")


EXTRA = ("FM-6-0_2022.pdf", make_pdf([PUBLIC_A, ["Chapter 2"]]))


def _failing_fetches(vault: FileVault, tmp_path: Path, bad: str, body: Any) -> tuple[Any, Any]:
    """``fetch(version)`` of [*OBJECTS, EXTRA] where ``bad`` fails as ``body()`` says (an exception to
    raise, or bytes served in its place), and ``backdate()`` of its failed-for-good ledger by 3 h."""
    from datetime import UTC, datetime, timedelta

    objects = [*OBJECTS, EXTRA]
    url = f"{BUCKET}/{PREFIX}{bad}"
    ledger = tmp_path / "work" / "test-pubs" / "failed-for-good.json"

    class Failing(FakeFetcher):
        def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
            if remote.url == url:
                got = body()
                if isinstance(got, BaseException):
                    raise got
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(got)
                return FetchResult(dest, len(got), "0" * 64, "0" * 64)
            return super().download(remote, dest)

    def fetch(version: str) -> Any:
        fetcher = CollectionFetcher(
            vault.root,
            Failing(routes(objects)),
            keys={},
            user_agent="test",
            workdir=tmp_path / "work",
            out=io.StringIO(),
            throttle=False,
        )
        return fetcher.run(spec(objects), version=version)

    def backdate() -> None:
        seen = json.loads(ledger.read_text())
        for e in seen.values():
            e["first"] = (datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="seconds")
        ledger.write_text(json.dumps(seen))

    return fetch, backdate


def test_a_document_refused_on_two_fetches_hours_apart_is_failed_for_good_and_the_fetch_complete(
    vault: FileVault, tmp_path: Path
) -> None:
    from wsp_cli.doccollections import EXIT_ALERT, EXIT_PARTIAL, fetch_exit_code
    from wsp_fetch.errors import MirrorsExhaustedError

    answer = {"why": "HTTP 403"}
    fetch, backdate = _failing_fetches(
        vault,
        tmp_path,
        "unmarked.pdf",
        lambda: MirrorsExhaustedError(f"no usable source: failed after 1 attempt(s): {answer['why']}"),
    )
    first = fetch("2026-10-10")
    assert [k for k, _ in first.failed] == ["test:unmarked.pdf"]
    assert first.given_up == []
    assert fetch_exit_code(first) == EXIT_PARTIAL
    ingest(vault, spec([*OBJECTS, EXTRA]))
    backdate()
    # A pass over what is held: nothing downloaded but the failing document and the restricted one
    # (refused by the screen: no proof); two held documents downloaded again show the host is well.
    second = fetch("2026-10-10")
    assert second.failed == []
    assert [k for k, _ in second.given_up] == ["test:unmarked.pdf"]
    assert fetch_exit_code(second) == EXIT_ALERT  # complete (the restricted document's alert stands)
    assert second.given_up_ledger == str(tmp_path / "work" / "test-pubs" / "failed-for-good.json")
    answer["why"] = "HTTP 503"  # the server fails now: transient, the run is partial again
    third = fetch("2026-10-10")
    assert [k for k, _ in third.failed] == ["test:unmarked.pdf"]
    assert third.given_up == []
    assert fetch_exit_code(third) == EXIT_PARTIAL


def test_a_document_refused_401_and_403_by_turns_is_failed_for_good(vault: FileVault, tmp_path: Path) -> None:
    """2026-11-10: archive.org answers ``usmc:mcrp0002unse`` 401 on most tries and 403 on some. Counted
    by the exact code, each change started the count over and it never reached given up; one refusal
    class counts every try, and the ledger shows the latest code."""
    from wsp_cli.doccollections import EXIT_PARTIAL, fetch_exit_code
    from wsp_fetch.errors import MirrorsExhaustedError

    codes = iter(["HTTP 401", "HTTP 403", "HTTP 401"])
    fetch, backdate = _failing_fetches(
        vault,
        tmp_path,
        "unmarked.pdf",
        lambda: MirrorsExhaustedError(f"no usable source: failed after 1 attempt(s): {next(codes)}"),
    )
    ledger = tmp_path / "work" / "test-pubs" / "failed-for-good.json"
    first = fetch("2026-10-10")  # 401
    assert [k for k, _ in first.failed] == ["test:unmarked.pdf"]
    ingest(vault, spec([*OBJECTS, EXTRA]))
    second = fetch("2026-10-10")  # 403: too soon after the first, but counted with it
    assert second.given_up == []
    assert fetch_exit_code(second) == EXIT_PARTIAL
    assert json.loads(ledger.read_text())["test:unmarked.pdf"]["tries"] == 2
    backdate()
    third = fetch("2026-10-10")  # 401 again, hours after the first
    assert third.failed == []
    assert [k for k, _ in third.given_up] == ["test:unmarked.pdf"]
    assert fetch_exit_code(third) != EXIT_PARTIAL
    seen = json.loads(ledger.read_text())["test:unmarked.pdf"]
    assert seen["tries"] == 3
    assert seen["sig"] == "refused: HTTP 401"  # the exact code, for display


def test_a_web_page_in_place_of_a_pdf_is_failed_for_good_after_three_fetches(
    vault: FileVault, tmp_path: Path
) -> None:
    """2026-11-09 (NTRS materials): one listed PDF is always an HTML page. Its wrong body is counted
    for the host listed for it, and the third fetch over 2 hours gives it up."""
    from wsp_cli.doccollections import EXIT_ALERT, EXIT_PARTIAL, fetch_exit_code

    page = b"<!DOCTYPE html><html><head><title>NTRS</title></head><body>Document</body></html>"
    fetch, backdate = _failing_fetches(vault, tmp_path, "unmarked.pdf", lambda: page)
    first = fetch("2026-10-10")
    assert [k for k, _ in first.failed] == ["test:unmarked.pdf"]
    assert first.permanent_hosts == {"test:unmarked.pdf": "bucket.example.gov"}
    ingest(vault, spec([*OBJECTS, EXTRA]))
    second = fetch("2026-10-10")
    assert second.given_up == []
    assert fetch_exit_code(second) == EXIT_PARTIAL
    backdate()
    third = fetch("2026-10-10")
    assert [k for k, _ in third.given_up] == ["test:unmarked.pdf"]
    assert third.failed == []
    assert fetch_exit_code(third) == EXIT_ALERT
    seen = json.loads((tmp_path / "work" / "test-pubs" / "failed-for-good.json").read_text())
    assert seen["test:unmarked.pdf"]["tries"] == 3
    assert seen["test:unmarked.pdf"]["sig"].startswith("not the file: ")


def test_a_zipped_pdf_download_keeps_the_pdf_it_holds(tmp_path: Path) -> None:
    """ITU-R's !!ZPF-E.zip: the document is the one PDF inside (2026-11-02)."""
    import zipfile

    from wsp_cli.doccollections import unzip_pdf
    from wsp_core.errors import FetchError

    path = tmp_path / "itu__R-REC-BO.1724-1-200701-I-abcd1234.pdf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("R-REC-BO.1724-1-200701-I!!PDF-E.pdf", b"%PDF-1.4 body")
        z.writestr("readme.txt", b"x")
    unzip_pdf(path)
    assert path.read_bytes() == b"%PDF-1.4 body"
    unzip_pdf(path)  # already the PDF: left as it is
    assert path.read_bytes() == b"%PDF-1.4 body"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("a.pdf", b"%PDF-1")
        z.writestr("b.pdf", b"%PDF-2")
    with pytest.raises(WrongBodyError, match="2 PDF files"):
        unzip_pdf(path)
    with zipfile.ZipFile(path, "w") as z:  # BO.1724's zip: the Recommendation and the ETSI standard it cites
        z.writestr("En301790.V1.3.1.pdf", b"%PDF-etsi")
        z.writestr("BO1724-1E.pdf", b"%PDF-rec")
    assert unzip_pdf(path, match="BO1724") == ("BO1724-1E.pdf", 2)
    assert path.read_bytes() == b"%PDF-rec"
    path.write_bytes(b"<html>error</html>")
    with pytest.raises(FetchError, match="not a zip"):
        unzip_pdf(path)
    assert not list(tmp_path.glob("*.unzip"))


def _zip_of(path: Path, members: Mapping[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def test_a_zip_of_two_pdfs_gives_the_one_named_after_the_document_without_the_resolvers_hint(
    tmp_path: Path,
) -> None:
    """The two ITU-R zips that failed on 29 passes (2026-11-13), with their real member names.

    BO.1724 was resolved before the resolver gave ``unzip_match`` and sat in the 45-day resolve
    cache without it: the key says as much. BT.1700's files are named by the number alone. Size is
    never the rule: the cited ETSI standard is the larger file."""
    from wsp_cli.doccollections import unzip_pdf

    bo = _zip_of(
        tmp_path / "itu__R-REC-BO.1724-1-200701-I-846d5a56.pdf",
        {"En301790.V1.3.1.pdf": b"%PDF-etsi, the larger one " * 40, "BO1724-1E.pdf": b"%PDF-rec"},
    )
    assert unzip_pdf(bo, key="itu:R-REC-BO.1724-1-200701-I") == ("BO1724-1E.pdf", 2)
    assert bo.read_bytes() == b"%PDF-rec"
    bt = _zip_of(
        tmp_path / "itu__R-REC-BT.1700-0-200502-I-dded1abb.pdf",
        {"S170m-2004.pdf": b"%PDF-smpte", "1700-e.pdf": b"%PDF-rec"},
    )
    assert unzip_pdf(bt, match="BT1700", key="itu:R-REC-BT.1700-0-200502-I") == ("1700-e.pdf", 2)
    assert bt.read_bytes() == b"%PDF-rec"
    one = _zip_of(tmp_path / "one.pdf", {"anything.pdf": b"%PDF-1", "notes.txt": b"x"})
    assert unzip_pdf(one, key="itu:R-REC-BT.1700-0-200502-I") is None  # no choice was made
    assert one.read_bytes() == b"%PDF-1"


@pytest.mark.parametrize(
    ("key", "members", "want"),
    [
        # 2026-11-13 review: a substring without bounds took the cited standard in each of these
        ("itu:R-REC-S.1011-1-199710-I", ["1011-e.pdf", "ts_101154v010901p.pdf"], "1011-e.pdf"),
        ("itu:R-REC-M.2004-0-201202-I", ["2004-e.pdf", "S170m-2004.pdf"], "2004-e.pdf"),
        ("itu:T-REC-G.711-198811-I", ["711-e.pdf", "G7110.pdf"], "711-e.pdf"),
        ("itu:R-REC-BT.601-7-201103-I", ["Rec-E.pdf", "IEC-601.pdf"], None),
        ("itu:R-REC-BT.2004-0-201202-I", ["rec.pdf", "S170m-2004.pdf"], None),
        ("itu:T-REC-X.1-198811-I", ["main.pdf", "annex-1.pdf"], None),  # a short number names nothing
        ("itu:T-REC-X.1-198811-I", ["1-e.pdf", "other.pdf"], None),
        ("itu:T-REC-X.25-199610-I", ["T-REC-X.25-E.pdf", "other.pdf"], "T-REC-X.25-E.pdf"),
        ("itu:R-REC-BO.1724-1-200701-I", ["dir/bo_1724.PDF", "dir/En301790.pdf"], "dir/bo_1724.PDF"),
        ("no-series-in-this-key", ["a.pdf", "b.pdf"], None),
    ],
)
def test_the_pdf_named_after_the_document_is_never_a_cited_standard(
    key: str, members: list[str], want: str | None
) -> None:
    assert dc._named_after(members, "", key) == want  # pyright: ignore[reportPrivateUsage]
    assert dc._named_after(members[::-1], "", key) == want  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "members",
    [
        ["a.pdf", "b.pdf"],  # nothing names the document
        ["1700-e.pdf", "1700-f.pdf"],  # the number names two
        ["BT1700-e.pdf", "BT1700-annex.pdf"],  # the series and number name two
        ["17001-e.pdf", "S21700.pdf"],  # 1700 only inside longer numbers
        ["sub/1700/a.pdf", "sub/1700/b.pdf"],  # in a folder's name, not the file's
        ["1700\\a.pdf", "x\\b.pdf"],  # a folder named by a Windows zip
        ["Rec-E.pdf", "IEC-1700.pdf"],  # the number only further in: the cited standard's
        ["rec.pdf", "S170m-1700.pdf"],
    ],
)
def test_a_zip_of_several_pdfs_none_alone_named_after_the_document_fails_for_good(
    tmp_path: Path, members: list[str]
) -> None:
    """No safe rule: nothing is taken (never the largest), the zip stays as it is, and the failure
    is of the ledger's "not the file" class, so the lane stops fetching it on every pass."""
    from wsp_cli.doccollections import unzip_pdf
    from wsp_core.errors import FetchError

    path = _zip_of(tmp_path / "x.pdf", {name: b"%PDF-" + name.encode() for name in members})
    before = path.read_bytes()
    with pytest.raises(WrongBodyError, match="2 PDF files in the zip, not one") as caught:
        unzip_pdf(path, match="BT1700", key="itu:R-REC-BT.1700-0-200502-I")
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.unzip"))
    assert dc.permanent_class(dc.permanent_kind(caught.value) or "") == "not the file"
    empty = _zip_of(tmp_path / "none.pdf", {"readme.txt": b"x"})
    with pytest.raises(FetchError, match="0 PDF files") as none:  # as before: a failed download
        unzip_pdf(empty)
    assert dc.permanent_kind(none.value) is None


# --- 2026-11-05 deep dive: a fetch of a cycle screens the copies earlier runs left ------------------------


class _Counting(FakeFetcher):
    """Records the documents downloaded (the listing is read through ``get_bytes``)."""

    def __init__(self, routes: dict[str, bytes]) -> None:
        super().__init__(routes)
        self.downloaded: list[str] = []

    def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
        self.downloaded.append(remote.url.rsplit("/", 1)[-1])
        return super().download(remote, dest)


def _fetcher(vault: FileVault, fetcher: FakeFetcher, tmp_path: Path) -> CollectionFetcher:
    return CollectionFetcher(
        vault.root,
        fetcher,
        keys={},
        user_agent="t",
        workdir=tmp_path / "work",
        out=io.StringIO(),
        throttle=False,
    )


TWO = [("FM-5-0_2022.pdf", GOOD), ("FM-6-0_2022.pdf", make_pdf([PUBLIC_A, ["Chapter 2"]]))]


def test_a_cycle_fetched_again_screens_what_its_ingest_staged_and_downloads_nothing(
    vault: FileVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """crs-reports' cycle 2026-10-20 was downloaded whole six times (23,188 files, up to 34 hours a
    time): every lane restart fetched again what the cycle's unsealed ingest had already staged."""
    s = spec(TWO)
    first = _fetcher(vault, _Counting(routes(TWO)), tmp_path).run(s, version="2026-10-10")
    assert len(first.fetched) == 2
    real_seal = vault.seal

    def reboot(*a: Any, **kw: Any) -> Any:
        raise OSError("the machine rebooted before the seal")

    monkeypatch.setattr(vault, "seal", reboot)
    result = Ingestor(vault, [s], out=io.StringIO()).run(source_id=s.id)
    assert [v.status for v in result.versions] == [VersionStatus.FAILED]  # staged, not sealed
    monkeypatch.setattr(vault, "seal", real_seal)
    counting = _Counting(routes(TWO))
    again = _fetcher(vault, counting, tmp_path).run(s, version="2026-10-10")
    assert counting.downloaded == []
    assert again.reused == 2
    assert sorted(d.file for d in again.fetched) == sorted(d.file for d in first.fetched)
    staging = vault.staging_path(s.id, "2026-10-10")
    assert all((staging / d.file).is_file() for d in again.fetched)  # read where it is, never moved
    ingest(vault, s)
    gen = vault.generation_path(s.id, "2026-10-10")
    assert all((gen / d.file).is_file() for d in first.fetched)


def test_a_cycle_fetched_again_before_its_ingest_takes_its_downloads_by_the_plans_checksum(
    vault: FileVault, tmp_path: Path
) -> None:
    s = spec(TWO)
    first = _fetcher(vault, _Counting(routes(TWO)), tmp_path).run(s, version="2026-10-10")
    one = vault.root / "_incoming" / s.id / "2026-10-10" / first.fetched[0].file.removeprefix("upstream/")
    one.write_bytes(GOOD[:-1] + b"!")  # the same size, other bytes: not what the plan names
    counting = _Counting(routes(TWO))
    again = _fetcher(vault, counting, tmp_path).run(s, version="2026-10-10")
    assert counting.downloaded == ["FM-5-0_2022.pdf"]
    assert again.reused == 1
    assert one.read_bytes() == GOOD
    ingest(vault, s)


def test_a_document_held_for_review_is_screened_again_from_its_copy_not_downloaded(
    vault: FileVault, tmp_path: Path
) -> None:
    """DLA's i04 r2 (2026-11-05) downloaded its 4,121 documents held for review again to find 11 new
    ones; the owner's accepted rows were never taken while the copies sat in the quarantine."""
    held_only = [("unmarked.pdf", UNMARKED)]
    s = spec(held_only)
    first = _fetcher(vault, _Counting(routes(held_only)), tmp_path).run(s, version="2026-10-10")
    (row,) = first.held
    copy = Path(row["quarantined"])
    counting = _Counting(routes(held_only))
    again = _fetcher(vault, counting, tmp_path).run(s, version="2026-10-11")
    assert counting.downloaded == []
    assert again.reused == 1
    assert [r["key"] for r in again.held] == [row["key"]]
    assert copy.is_file()  # held again, where it was
    # the issuer's file changed (another size and MD5 in the listing): downloaded
    changed = [("unmarked.pdf", make_pdf([["PLANNING HANDBOOK", "Contents", "Second edition"]]))]
    counting = _Counting(routes(changed))
    _fetcher(vault, counting, tmp_path).run(spec(changed), version="2026-10-12")
    assert counting.downloaded == ["unmarked.pdf"]
    # the owner accepts it: taken from the copy into the cycle, and ingested
    rules = tmp_path / "rules.yaml"
    rules.write_text("allow_all: true\n", encoding="utf-8")
    (tmp_path / "decisions").mkdir()
    (tmp_path / "decisions" / "rules.yaml").write_text(
        yaml.safe_dump({"allow_documents": [row["key"]], "allow_content_review": [row["key"]]}),
        encoding="utf-8",
    )
    raw = dict(s.raw)
    access = cast(dict[str, Any], raw["access"])
    accepted = replace(
        s, raw={**raw, "access": {**access, "filter": {**access["filter"], "rules": str(rules)}}}
    )
    counting = _Counting(routes(changed))
    taken = _fetcher(vault, counting, tmp_path).run(accepted, version="2026-10-13")
    assert counting.downloaded == []
    assert [d.cand.key for d in taken.fetched] == [row["key"]]
    assert not taken.held
    assert not copy.exists()  # moved into the cycle
    ingest(vault, accepted)


def test_a_held_copy_with_no_listed_size_is_trusted_only_for_a_while(tmp_path: Path) -> None:
    import os
    import time

    from wsp_cli.doccollections import REFETCH_AFTER

    cand = Candidate(key="t:a", url="https://bucket.example.gov/a.pdf", title="A", issuer="T")
    copy = tmp_path / cand.file_name
    copy.write_bytes(b"%PDF-held")
    fresh = CollectionFetcher._held_fresh  # pyright: ignore[reportPrivateUsage]
    assert fresh(cand, copy)
    old = time.time() - REFETCH_AFTER.total_seconds() - 86400
    os.utime(copy, (old, old))
    assert not fresh(cand, copy)  # an issuer's quiet correction is still picked up


def test_a_fetch_killed_partway_keeps_its_whole_downloads_with_no_listed_checksum(
    vault: FileVault, tmp_path: Path
) -> None:
    """2026-11-05 review: the plan is written only when a fetch ends, so a lane restarted partway
    through a cycle's first fetch (aozora's take 34-41 hours) downloaded everything again where the
    listing gives no MD5 (crs-reports, aozora, NIST)."""
    import re

    three = [*TWO, ("FM-7-0_2022.pdf", make_pdf([PUBLIC_A, ["Chapter 3"]]))]
    no_md5 = {
        k: re.sub(rb"<ETag>.*?</ETag>", b"", v) if k.endswith("/") else v for k, v in routes(three).items()
    }

    class Killed(_Counting):
        def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
            if len(self.downloaded) == 2:
                raise KeyboardInterrupt  # SIGTERM, an OOM: the run stops here, with no plan written
            return super().download(remote, dest)

    s = spec(three)
    killed = Killed(no_md5)
    with pytest.raises(KeyboardInterrupt):
        _fetcher(vault, killed, tmp_path).run(s, version="2026-10-10")
    assert killed.downloaded == ["FM-5-0_2022.pdf", "FM-6-0_2022.pdf"]
    dest = vault.root / "_incoming" / s.id / "2026-10-10"
    (dest / "FM-7-0_2022.pdf.partial").write_bytes(b"%PDF-half")  # never taken: not a final name
    counting = _Counting(no_md5)
    again = _fetcher(vault, counting, tmp_path).run(s, version="2026-10-10")
    assert counting.downloaded == ["FM-7-0_2022.pdf"]
    assert again.reused == 2
    assert sorted(d.cand.title for d in again.fetched) == ["FM 5 0 2022", "FM 6 0 2022", "FM 7 0 2022"]
    assert not again.failed
    ingest(vault, s)


def test_a_left_download_is_found_by_its_name_or_its_true_suffix_and_trusted_only_for_a_while(
    tmp_path: Path,
) -> None:
    import os
    import time

    from wsp_cli.doccollections import REFETCH_AFTER

    fetch = CollectionFetcher(
        tmp_path / "vault", FakeFetcher(), keys={}, user_agent="t", workdir=tmp_path, throttle=False
    )
    dest = tmp_path / "dest"
    dest.mkdir()
    for name in ("a.txt", "b.pdf", "b.docx", "c.pdf.partial", "d.doc", "d.xls", "e.html", "x.zip"):
        (dest / name).write_bytes(b"%PDF-x")
    names = {"a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pdf", "x.zip"}
    left = fetch._earlier_copies(spec(TWO), "2026-10-10", dest, names).left  # pyright: ignore[reportPrivateUsage]
    # renamed by the screen; its own name before a renamed copy; ambiguous; a document's own .zip
    assert left == {"a.pdf": "a.txt", "b.pdf": "b.pdf", "d.pdf": None, "x.zip": "x.zip"}

    fresh = CollectionFetcher._left_fresh  # pyright: ignore[reportPrivateUsage]
    cand = Candidate(key="t:b", url="https://bucket.example.gov/b.pdf", title="B", issuer="T")
    copy = dest / "b.pdf"
    assert fresh(cand, copy)
    assert fresh(replace(cand, size=6), copy)
    assert not fresh(replace(cand, size=7), copy)  # not what the listing offers
    assert not fresh(replace(cand, extra={"unzip": "pdf"}), copy)  # maybe never unpacked
    old = time.time() - REFETCH_AFTER.total_seconds() - 86400
    os.utime(copy, (old, old))
    assert not fresh(cand, copy)
    assert fresh(replace(cand, md5="0" * 32), copy)  # the MD5 is checked as it is screened


def test_a_held_copy_today_s_rules_deny_stays_held_for_the_owner(vault: FileVault, tmp_path: Path) -> None:
    """2026-11-06 review: a copy held for the owner's review was moved out of the quarantine and
    screened again; a deny under today's rules deleted it, perhaps the only copy, before the owner had
    decided. It is held again, the deny noted; only the owner's own deny deletes it."""
    held_only = [("unmarked.pdf", UNMARKED)]
    s = spec(held_only)
    (row,) = _fetcher(vault, _Counting(routes(held_only)), tmp_path).run(s, version="2026-10-10").held
    copy = Path(row["quarantined"])
    raw = dict(s.raw)
    access = cast(dict[str, Any], raw["access"])

    def with_filter(**extra: object) -> SourceSpec:
        return replace(s, raw={**raw, "access": {**access, "filter": {**access["filter"], **extra}}})

    denying = with_filter(deny_keywords=["planning handbook"])
    counting = _Counting(routes(held_only))
    again = _fetcher(vault, counting, tmp_path).run(denying, version="2026-10-11")
    assert counting.downloaded == []
    assert not again.rejected
    (held,) = again.held
    assert held["rule"].startswith("deny:keyword=planning handbook")
    assert "owner to decide" in held["rule"]
    assert copy.is_file()  # held again, never deleted
    # the owner's own deny is a decision: that one deletes it
    fetcher = _fetcher(vault, _Counting(routes(held_only)), tmp_path)
    report = FetchReport(source_id=s.id, version="2026-10-12")
    denied = Screen(Decision(Verdict.DENY, "owner=deny"), Marking(), False)
    cand = Candidate(key=row["key"], url=row["url"], title=row["title"], issuer="T")
    fetcher._screen(cand, copy, Rules(), report, {}, screen=denied, was_held=True)  # pyright: ignore[reportPrivateUsage]
    assert [k for k, _ in report.rejected] == [row["key"]]
    assert not report.held
    assert not copy.exists()
