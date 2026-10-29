"""``wsp collections`` (fetch + dry run) and ``wsp ingest`` of a document-collection generation."""

from __future__ import annotations

import dataclasses
import io
import json
import subprocess
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
import yaml

from wsp_adapters.doccollection import DocCollectionAdapter
from wsp_adapters.doccollection.adapter import CATALOG, screen_text
from wsp_adapters.doccollection.catalog import read_catalog_file
from wsp_adapters.doccollection.model import Candidate
from wsp_adapters.doccollection.policy import Rules, load_rules
from wsp_adapters.doccollection.testing import PUBLIC_A, RESTRICTED_C, make_pdf, s3_listing
from wsp_cli.doccollections import (
    CollectionFetcher,
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

    with patch.object(dc, "screen_document", flaky), patch("wsp_cli.nasretry.time.sleep"):
        report = runner(vault, OBJECTS, tmp_path).run(spec(OBJECTS), version="2026-10-10")
    assert [d.cand.title for d in report.fetched] == ["FM 5 0 2022"]  # second try went through
    (failed,) = report.failed
    assert failed[0] == "test:restricted.pdf"
    assert "the share did not answer" in failed[1]
    assert len(report.held) == 1  # the third document was screened as usual


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
