"""Source integrity around the release pipeline (ADR-0009): provenance, independent checksum
channels, mirror agreement, origin pinning, content anomalies and known-good anchoring."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wsp_core.model import (
    Checksum,
    Generation,
    GenerationStatus,
    ReleaseRef,
    ReleaseState,
    RemoteFile,
    SourceSpec,
)
from wsp_orchestrator.clock import ManualClock
from wsp_orchestrator.errors import ResourceBusyError
from wsp_orchestrator.integrity import (
    Action,
    AnomalyThresholds,
    ContentProfile,
    HostSeen,
    IntegrityHooks,
    IntegritySettings,
    OriginPin,
    ProfileBuilder,
    compare_profiles,
    evaluate_origins,
    merge_profiles,
    registrable_domain,
)
from wsp_orchestrator.integrity.channels import (
    ChannelObservation,
    judge_channels,
    kiwix_refs,
    parse_checksum_doc,
    sidecar_refs,
    wikimedia_refs,
)
from wsp_orchestrator.jobs import JobKind
from wsp_orchestrator.orchestrator import Orchestrator
from wsp_orchestrator.pipeline import Outcome
from wsp_orchestrator.retention import GenerationInfo, GfsPolicy, plan_retention
from wsp_orchestrator.testing import (
    FakeWorld,
    PipelineHarness,
    make_settings,
    make_spec,
    mirror_provenance,
    sha256_hex,
)

KIWIX = "https://lb.download.kiwix.org/zim/wikipedia/"
DATA_1 = b"<html><body>generation one</body></html>\n" * 40
DATA_2 = b"<html><body>generation two</body></html>\n" * 40


class Attestations:
    """In-memory stand-in for ``wsp_vault.attest.write_attestation``."""

    def __init__(self) -> None:
        self.docs: dict[tuple[str, str, str], Mapping[str, Any]] = {}

    def __call__(self, source_id: str, generation_id: str, name: str, body: Mapping[str, Any]) -> str:
        self.docs[(source_id, generation_id, name)] = body
        return f"b3:{len(self.docs)}"


class Profiles:
    """Profile hook returning prepared profiles per generation."""

    def __init__(self) -> None:
        self.by_gen: dict[str, ContentProfile] = {}
        self.calls = 0

    def __call__(self, spec: SourceSpec, generation: Generation, generation_dir: Path) -> ContentProfile:
        self.calls += 1
        return self.by_gen[generation.generation_id]


def profile(
    n: int = 1000,
    *,
    changed: int = 0,
    deleted: int = 0,
    scripts: int = 0,
    mime: str = "text/html",
    executable: bool = False,
) -> ContentProfile:
    b = ProfileBuilder(sample_rate=0.25)
    for i in range(deleted, n):
        body = b"<p>doc %d</p>" % i
        if i < deleted + changed:
            body += b" edited"
        if scripts and i % 10 == 0:
            body += b"<script>x()</script>" * scripts
        if executable and i == n - 1:
            body = b"MZ\x90\x00binary"
        b.add(f"A/doc{i}", namespace="A", mimetype=mime if i == n - 1 else "text/html", size=len(body),
              title=f"Doc {i}", content=body)  # fmt: skip
    return b.build()


def kiwix_release(source_id: str, version: str, data: bytes, *, origin: str = KIWIX) -> ReleaseRef:
    name = f"wikipedia_en_test_{version}.zim"
    return ReleaseRef(
        source_id=source_id,
        version=version,
        published=datetime.fromisoformat(version).replace(tzinfo=UTC),
        files=(
            RemoteFile(
                url=f"{origin}{name}",
                name=name,
                size=len(data),
                checksums=(Checksum("sha256", sha256_hex(data)),),
            ),
        ),
        format="zim",
    )


def setup(
    tmp_path: Path, *, settings: IntegritySettings | None = None, profiles: Profiles | None = None
) -> tuple[PipelineHarness, Attestations]:
    attest = Attestations()
    hooks = IntegrityHooks(settings=settings or IntegritySettings(), attest=attest, profile=profiles)
    h = PipelineHarness(tmp_path, sources=[make_spec("wp", adapter="kiwix-opds")], integrity=hooks)
    return h, attest


def offer(
    h: PipelineHarness,
    version: str,
    data: bytes,
    *,
    origin: str = KIWIX,
    channel_digest: str | None = None,
    mirrors: Mapping[str, str] | None = None,
    issuer: str = "Let's Encrypt",
    **prov: Any,
) -> ReleaseRef:
    """Upstream offers ``version``: bytes from mirrors, the .sha256 on the primary."""
    ref = kiwix_release("wp", version, data, origin=origin)
    remote = ref.files[0]
    h.adapter.releases.append(ref)
    h.adapter.contents[(version, remote.name)] = data
    srcs = (
        mirrors
        if mirrors is not None
        else {"https://mirror.example.org/" + remote.name: "mirror.example.org"}
    )
    h.adapter.provenance[(version, remote.name)] = mirror_provenance(
        remote, len(data), mirrors=srcs, issuer=issuer, **prov
    )
    digest = channel_digest or sha256_hex(data)
    sidecar = "https://lb.download.kiwix.org/" + remote.url.split("/", 3)[3] + ".sha256"
    h.fetcher.documents[sidecar] = f"{digest}  {remote.name}\n".encode()
    h.fetcher.observations[sidecar] = {
        "final_url": sidecar,
        "tls": {"issuer_family": issuer, "sha256": "cert-primary"},
    }
    return ref


def run(h: PipelineHarness, ref: ReleaseRef) -> Outcome:
    h.pipeline.register(ref)
    return h.pipeline.run("wp", ref.version).outcome


# --- the happy path -------------------------------------------------------------------------------


def test_clean_release_is_promoted_with_signed_provenance_and_report(tmp_path: Path) -> None:
    h, attest = setup(tmp_path)
    ref = offer(h, "2026-10-01", DATA_1)
    assert run(h, ref) is Outcome.PROMOTED
    stored = h.store.provenance("wp", "2026-10-01")
    assert stored is not None
    verdict, doc = stored
    assert verdict == "info"
    assert doc["channel_status"] == "independent"
    channel = doc["files"][0]["channel"]
    assert channel["status"] == "independent"
    assert channel["channels"][0]["url"].startswith("https://lb.download.kiwix.org/zim/")
    assert doc["files"][0]["fetch"]["sources"][0]["host"] == "mirror.example.org"
    assert {(r["host"], r["role"]) for r in h.store.origin_pins("wp")} == {
        ("lb.download.kiwix.org", "origin"),
        ("mirror.example.org", "mirror"),
    }
    prov = attest.docs[("wp", "2026-10-01", "PROVENANCE.json")]
    assert prov["release"]["version"] == "2026-10-01"
    report = attest.docs[("wp", "2026-10-01", "PROMOTION.json")]
    assert report["verdict"] == "pass"
    assert [c["name"] for c in report["checks"]] == ["sanity"]


# --- independent channel and mirror agreement ------------------------------------------------------


def test_independent_checksum_disagreement_quarantines_and_pauses(tmp_path: Path) -> None:
    h, attest = setup(tmp_path)
    ref = offer(h, "2026-10-01", DATA_1, channel_digest="0" * 64)
    assert run(h, ref) is Outcome.QUARANTINED
    rec = h.pipeline.get("wp", "2026-10-01")
    assert rec is not None
    assert "checksum-channel-mismatch" in (rec.last_error or "")
    assert h.store.source_health("wp").paused
    assert "integrity:wp/2026-10-01" in h.sink.alert_keys()
    assert h.vault.current("wp") is None
    assert h.vault.calls["seal"] == 0  # never reached the vault
    assert not attest.docs


@pytest.mark.parametrize(
    ("prov", "code"),
    [
        (
            {
                "rejected": [
                    {
                        "url": "https://m2.example.net/x",
                        "reason": "size-disagreement",
                        "size": 1,
                        "expected": 2,
                    }
                ]
            },
            "mirror-size-disagreement",
        ),
        ({"spot_check": "mismatch"}, "mirror-content-disagreement"),
        ({"corrupt_pieces": 2}, "mirror-corrupt-pieces"),
    ],
)
def test_tampered_mirror_quarantines(tmp_path: Path, prov: dict[str, Any], code: str) -> None:
    h, _ = setup(tmp_path)
    mirrors = {
        "https://mirror.example.org/a.zim": "mirror.example.org",
        "https://m2.example.net/a.zim": "m2.example.net",
    }
    ref = offer(h, "2026-10-01", DATA_1, mirrors=mirrors, **prov)
    assert run(h, ref) is Outcome.QUARANTINED
    stored = h.store.provenance("wp", "2026-10-01")
    assert stored is not None
    assert code in {f["code"] for f in stored[1]["findings"]}
    assert stored[0] == "quarantine"


def test_mirror_disagreement_can_be_downgraded_to_an_alert(tmp_path: Path) -> None:
    h, _ = setup(tmp_path, settings=IntegritySettings(mirror_disagreement=Action.ALERT))
    ref = offer(h, "2026-10-01", DATA_1, spot_check="mismatch")
    assert run(h, ref) is Outcome.PROMOTED
    assert any(k.startswith("integrity:wp:mirror-content-disagreement") for k in h.sink.alert_keys())


def test_unavailable_channel_is_recorded_not_blocking(tmp_path: Path) -> None:
    h, _ = setup(tmp_path)
    ref = offer(h, "2026-10-01", DATA_1)
    h.fetcher.documents.clear()  # the .sha256 is gone (404)
    assert run(h, ref) is Outcome.PROMOTED
    stored = h.store.provenance("wp", "2026-10-01")
    assert stored is not None
    assert stored[1]["files"][0]["channel"]["status"] == "unavailable"


# --- origin pinning --------------------------------------------------------------------------------


def test_unknown_origin_domain_holds_until_owner_trusts_it(tmp_path: Path) -> None:
    h, _ = setup(tmp_path)
    assert run(h, offer(h, "2026-10-01", DATA_1)) is Outcome.PROMOTED
    evil = offer(h, "2026-11-01", DATA_2, origin="https://download.kiwix.evil.example/zim/")
    assert run(h, evil) is Outcome.HELD
    rec = h.pipeline.get("wp", "2026-11-01")
    assert rec is not None
    assert rec.state is ReleaseState.FETCHED
    assert "origin-unknown-domain" in (rec.hold_reason or "")
    assert "download.kiwix.evil.example" not in {r["host"] for r in h.store.origin_pins("wp")}
    h.pipeline.approve("wp", "2026-11-01", note="kiwix moved; confirmed on their blog")
    assert h.pipeline.run("wp", "2026-11-01").outcome is Outcome.PROMOTED
    assert "download.kiwix.evil.example" in {r["host"] for r in h.store.origin_pins("wp")}
    approved = [e for e in h.ledger.of_kind("release.hold") if e.data.get("action") == "approved"]
    assert approved[-1].data["trusted_hosts"] == ["download.kiwix.evil.example"]


def test_certificate_issuer_change_alerts_but_continues(tmp_path: Path) -> None:
    h, _ = setup(tmp_path)
    assert run(h, offer(h, "2026-10-01", DATA_1)) is Outcome.PROMOTED
    assert run(h, offer(h, "2026-11-01", DATA_2, issuer="Shady Certs Ltd")) is Outcome.PROMOTED
    keys = h.sink.alert_keys()
    assert "origin-drift:wp:origin-issuer-changed:lb.download.kiwix.org" in keys


def test_origin_rules() -> None:
    now = datetime(2026, 10, 15, tzinfo=UTC)
    first = evaluate_origins(
        "s", [], [HostSeen("dumps.wikimedia.org", "origin")], now=now, independently_verified=True
    )
    assert [f.code for f in first.findings] == ["origin-first-trust"]
    pins = list(first.pins)
    res = evaluate_origins(
        "s",
        pins,
        [
            HostSeen("dumps2.wikimedia.org", "checksum"),  # new host, known domain: alert
            HostSeen("ftp.acc.umu.se", "mirror"),  # new mirror, verified bytes: info
        ],
        now=now,
        independently_verified=True,
    )
    assert {(f.code, f.action) for f in res.findings} == {
        ("origin-new-host", Action.ALERT),
        ("mirror-new-host", Action.INFO),
    }
    unverified = evaluate_origins(
        "s", pins, [HostSeen("ftp.acc.umu.se", "mirror")], now=now, independently_verified=False
    )
    assert unverified.findings[0].action is Action.ALERT
    blocked = evaluate_origins(
        "s", pins, [HostSeen("dumps.evil.example", "origin")], now=now, independently_verified=True
    )
    assert blocked.findings[0].action is Action.HOLD
    assert [b.host for b in blocked.blocked] == ["dumps.evil.example"]
    assert not blocked.pins
    pin = OriginPin("s", "a.example.org", "origin", "Let's Encrypt", "c1", now, now)
    again = HostSeen("a.example.org", "origin", issuer_family="Let's Encrypt", cert_sha256="c2")
    rotated = evaluate_origins("s", [pin], [again], now=now, independently_verified=True)
    assert [f.code for f in rotated.findings] == ["origin-cert-rotated"]
    assert registrable_domain("mirror.example.ac.uk") == "example.ac.uk"
    assert registrable_domain("ftp.acc.umu.se") == "umu.se"
    assert registrable_domain("127.0.0.1") == "127.0.0.1"


# --- channels --------------------------------------------------------------------------------------


def test_channel_locations() -> None:
    wm = RemoteFile(
        "https://ftp.acc.umu.se/mirror/wikimedia.org/dumps/enwiki/20261001/enwiki-20261001-pages-articles-multistream.xml.bz2",
        "enwiki-20261001-pages-articles-multistream.xml.bz2",
    )
    refs = wikimedia_refs(wm)
    assert (
        refs[0].url
        == "https://dumps.wikimedia.org/mirror/wikimedia.org/dumps/enwiki/20261001/enwiki-20261001-sha1sums.txt"
    )
    primary = replace(wm, url="https://dumps.wikimedia.org/enwiki/20261001/" + wm.name)
    assert [r.url for r in wikimedia_refs(primary)] == [
        "https://dumps.wikimedia.org/enwiki/20261001/enwiki-20261001-sha1sums.txt",
        "https://dumps.wikimedia.org/enwiki/20261001/enwiki-20261001-md5sums.txt",
    ]
    kx = RemoteFile("https://mirror.example/zim/x.zim", "x.zim")
    assert kiwix_refs(kx)[0].url == "https://lb.download.kiwix.org/zim/x.zim.sha256"
    osm = RemoteFile(
        "https://planet.osm.org/pbf/p.osm.pbf",
        "p.osm.pbf",
        checksums=(Checksum("md5", "a" * 32),),
        mirrors=("https://m1.example/p.osm.pbf", "https://m2.example/p.osm.pbf"),
    )
    assert [r.url for r in sidecar_refs(osm, {"m1.example"})] == [
        "https://m2.example/p.osm.pbf.md5",
        "https://planet.osm.org/pbf/p.osm.pbf.md5",
    ]


def test_checksum_documents_and_judgement() -> None:
    sums = "aa" * 20 + "  enwiki-x.xml.bz2\n" + "bb" * 20 + " *other.bz2\n"
    assert parse_checksum_doc(sums, "enwiki-x.xml.bz2", "sha1") == {"sha1": "aa" * 20}
    assert parse_checksum_doc("SHA256 (x.zim) = " + "cc" * 32, "x.zim", None) == {"sha256": "cc" * 32}
    assert parse_checksum_doc("dd" * 16 + "\n", "p.osm.pbf", "md5") == {"md5": "dd" * 16}
    ok = ChannelObservation(
        "https://dumps.wikimedia.org/s", "dumps.wikimedia.org", "primary-list", {"sha1": "aa" * 20}
    )
    ours = {"sha1": "aa" * 20, "sha256": "x"}
    assert judge_channels([ok], ours=ours, declared={}, byte_hosts={"ftp.acc.umu.se"}).status == "independent"
    assert (
        judge_channels([ok], ours=ours, declared={}, byte_hosts={"dumps.wikimedia.org"}).status
        == "same-origin"
    )
    assert judge_channels([ok], ours={"sha1": "ff" * 20}, declared={}, byte_hosts=set()).status == "mismatch"
    assert (
        judge_channels([ok], ours=ours, declared={"sha1": "ee" * 20}, byte_hosts=set()).status == "mismatch"
    )
    gone = ChannelObservation("u", "h", "k", error="NotFoundError")
    assert judge_channels([gone], ours=ours, declared={}, byte_hosts=set()).status == "unavailable"


# --- content anomalies ------------------------------------------------------------------------------


def test_profile_comparison_thresholds() -> None:
    base = profile()
    same = compare_profiles(base, profile(changed=10))[0]
    assert all(c.ok for c in same)

    def failed(new: ContentProfile, t: AnomalyThresholds = AnomalyThresholds()) -> set[str]:  # noqa: B008
        return {c.name for c in compare_profiles(base, new, t)[0] if not c.ok and c.holds}

    assert failed(profile(deleted=100)) >= {"deleted-fraction"}  # mass deletion
    assert "changed-fraction" in failed(profile(changed=600))
    assert "script-density" in failed(profile(scripts=5))
    assert "new-mimetypes" in failed(profile(mime="application/x-msdownload"))
    assert "executables" in failed(profile(executable=True))
    assert "new-mimetypes" not in failed(
        profile(mime="image/webp"), AnomalyThresholds(mime_allowlist=frozenset({"image/webp"}))
    )
    lenient = AnomalyThresholds(
        max_deleted_fraction=0.5, max_namespace_change=0.5, max_histogram_distance=1.0
    )
    assert not failed(profile(deleted=100), lenient)
    _, top = compare_profiles(base, profile(changed=50, deleted=10))
    assert top
    assert {"key", "title", "old_size", "new_size"} <= set(top[0])
    assert AnomalyThresholds.from_mapping(
        {"max_changed_fraction": 0.5, "mime_allowlist": ["A/B"]}
    ) == AnomalyThresholds(max_changed_fraction=0.5, mime_allowlist=frozenset({"a/b"}))


def test_profile_round_trip_and_merge(tmp_path: Path) -> None:
    p = profile(n=200, scripts=2)
    p.save(tmp_path / "p.json.gz")
    assert ContentProfile.load(tmp_path / "p.json.gz") == p
    merged = merge_profiles([p, profile(n=100)])
    assert merged.doc_count == 300
    assert merged.namespaces["A"] == 300


def test_anomalous_generation_is_held_then_approved(tmp_path: Path) -> None:
    profiles = Profiles()
    h, attest = setup(tmp_path, profiles=profiles)
    profiles.by_gen["2026-10-01"] = profile()
    profiles.by_gen["2026-11-01"] = profile(deleted=200)  # 20 % of articles gone
    assert run(h, offer(h, "2026-10-01", DATA_1)) is Outcome.PROMOTED
    baseline = attest.docs[("wp", "2026-10-01", "PROMOTION.json")]
    assert baseline["verdict"] == "pass"
    assert baseline["checks"][-1]["name"] == "baseline"
    assert run(h, offer(h, "2026-11-01", DATA_2)) is Outcome.HELD
    rec = h.pipeline.get("wp", "2026-11-01")
    assert rec is not None
    assert rec.state is ReleaseState.REFINED  # held before the index: never searchable (H2)
    assert ("wp", "2026-11-01") not in h.refinery.indexed
    assert "deleted-fraction" in (rec.hold_reason or "")
    report = attest.docs[("wp", "2026-11-01", "PROMOTION.json")]
    assert (report["verdict"], report["baseline"]) == ("hold", "2026-10-01")
    assert report["top_changed"]
    assert h.vault.current("wp") is not None
    assert h.vault.current("wp").generation_id == "2026-10-01"  # type: ignore[union-attr]
    h.pipeline.approve("wp", "2026-11-01", note="upstream removed spam")
    assert h.pipeline.run("wp", "2026-11-01").outcome is Outcome.PROMOTED
    report = attest.docs[("wp", "2026-11-01", "PROMOTION.json")]
    assert (report["verdict"], report["decision"]) == ("approved", "approved by the owner")
    assert profiles.calls == 2  # the held generation's profile was kept on disk, not recomputed


# --- known-good anchoring ---------------------------------------------------------------------------


def info(gid: str, *, good: bool = True, settled: bool = True) -> GenerationInfo:
    return GenerationInfo(gid, date.fromisoformat(gid), GenerationStatus.SEALED, False, good, settled)


def test_known_good_anchor_is_kept_until_the_new_generation_settles() -> None:
    policy = GfsPolicy(last=1, monthly=0, yearly=0)
    gens = [info("2026-08-01"), info("2026-09-01"), info("2026-10-01", settled=False)]
    plan = plan_retention(gens, policy, current="2026-10-01")
    assert plan.prune == ("2026-08-01",)
    assert "known-good anchor" in " ".join(plan.reasons["2026-09-01"])
    settled = [info("2026-08-01"), info("2026-09-01"), info("2026-10-01")]
    assert plan_retention(settled, policy, current="2026-10-01").prune == ("2026-08-01", "2026-09-01")
    # the newest generation failed its checks (never promoted): the last good one stays
    bad_newest = [info("2026-09-01"), info("2026-10-01", good=False, settled=False)]
    plan = plan_retention(bad_newest, policy, current="2026-09-01")
    assert plan.prune == ()


def never_last_copy(source_id: str, generation: Generation) -> bool:
    return False


def test_prune_job_uses_promotion_and_settle_state(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 10, 15, tzinfo=UTC))
    spec = make_spec("src", raw={"retention": {"gfs": {"last": 1, "monthly": 0, "yearly": 0}}})
    world = FakeWorld.build(tmp_path, clock, [spec])
    scrubbed = {"2026-08-01"}
    hooks = IntegrityHooks(settings=IntegritySettings(settle_days=14), scrubbed=lambda s, g: g in scrubbed)
    orch = Orchestrator(
        make_settings(tmp_path),
        world.wiring(integrity=hooks, pruner=world.vault.prune, last_copy=never_last_copy),
        clock=clock,
        check_local=False,
    )
    adapter = world.adapters["src"]
    for version, days in (("2026-08-01", 1), ("2026-09-01", 21), ("2026-10-01", 0)):
        adapter.add_release(version, {"f.txt": version.encode() * 10})
        orch.pipeline.register(adapter.releases[-1])
        assert orch.pipeline.run("src", version).outcome is Outcome.PROMOTED
        clock.advance(timedelta(days=days))
    orch.queue.enqueue(JobKind.PRUNE, "src", now=clock.now(), payload={"source_id": "src"})
    orch.run_pending()
    # 2026-10-01 is new and 2026-09-01 was never scrubbed: 2026-08-01 (settled, scrubbed) is the
    # known-good anchor and stays; GFS alone would have pruned it.
    assert world.vault.pruned == [("src", "2026-09-01")]
    orch.close()


def test_a_failing_profiler_holds_instead_of_failing_the_release(tmp_path: Path) -> None:
    profiles = Profiles()  # no prepared profile: the hook raises KeyError
    h, attest = setup(tmp_path, profiles=profiles)
    assert run(h, offer(h, "2026-10-01", DATA_1)) is Outcome.HELD
    rec = h.pipeline.get("wp", "2026-10-01")
    assert rec is not None
    assert rec.attempts == 0
    assert "content could not be profiled" in (rec.hold_reason or "")
    assert attest.docs[("wp", "2026-10-01", "PROMOTION.json")]["verdict"] == "hold"


def test_a_busy_or_oom_killed_profiler_is_retried_not_held(tmp_path: Path) -> None:
    """Audit H1/C2: a profile unit that found the HEAVY lock held says nothing about the content."""
    profiles = Profiles()
    profiles.by_gen["2026-10-01"] = profile()
    failures: list[Exception] = [ResourceBusyError("another 'heavy' is running")]

    def flaky(spec: SourceSpec, generation: Generation, generation_dir: Path) -> ContentProfile:
        if failures:
            raise failures.pop(0)
        return profiles(spec, generation, generation_dir)

    attest = Attestations()
    hooks = IntegrityHooks(attest=attest, profile=flaky)
    h = PipelineHarness(tmp_path, sources=[make_spec("wp", adapter="kiwix-opds")], integrity=hooks)
    result = run(h, offer(h, "2026-10-01", DATA_1))
    assert result is Outcome.DEFERRED
    rec = h.pipeline.get("wp", "2026-10-01")
    assert rec is not None
    assert (rec.state, rec.hold_reason, rec.attempts) == (ReleaseState.REFINED, None, 0)
    assert ("wp", "2026-10-01", "PROMOTION.json") not in attest.docs
    h.clock.advance(timedelta(minutes=11))
    assert h.pipeline.run("wp", "2026-10-01").outcome is Outcome.PROMOTED


def test_pins_are_staged_until_promotion_and_evidence_is_kept_per_release(tmp_path: Path) -> None:
    """Audit M5/M3: a quarantined first release must not pin its hosts; its files stay as evidence
    in a directory of their own."""
    h, _ = setup(tmp_path)
    bad = offer(h, "2026-10-01", DATA_1, channel_digest="0" * 64)
    assert run(h, bad) is Outcome.QUARANTINED
    assert h.store.origin_pins("wp") == []
    name = bad.files[0].name
    evidence = tmp_path / "work" / "_quarantine" / "wp" / "2026-10-01" / "evidence" / name
    assert evidence.read_bytes() == DATA_1
    assert not (tmp_path / "work" / "wp" / name).exists()

    from wsp_orchestrator.pipeline import resume_source

    resume_source(h.store, h.pipeline.ledger_outbox_writer, "wp", alerter=h.alerter)
    good = offer(h, "2026-11-01", DATA_2)
    h.pipeline.register(good)
    h.pipeline.step("wp", "2026-11-01")  # PLANNED
    h.pipeline.step("wp", "2026-11-01")  # FETCHING
    h.pipeline.step("wp", "2026-11-01")  # FETCHED
    h.pipeline.step("wp", "2026-11-01")  # VERIFIED: pins staged, not committed
    assert h.store.origin_pins("wp") == []
    assert h.pipeline.run("wp", "2026-11-01").outcome is Outcome.PROMOTED
    assert {r["host"] for r in h.store.origin_pins("wp")} == {"lb.download.kiwix.org", "mirror.example.org"}
    assert evidence.read_bytes() == DATA_1, "a later release never overwrites earlier evidence"
