"""Curator bots, the proposal sink, the registry overlay, parity jobs and last-copy pruning."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from wsp_core.model import Generation, SourceSpec
from wsp_orchestrator.alerts import Alert, Severity
from wsp_orchestrator.bots import BotContext, BotResult
from wsp_orchestrator.clock import ManualClock
from wsp_orchestrator.curator import GapMiner, KiwixWatcher, ProposalFiles, WikimediaRankingBot
from wsp_orchestrator.curator.health import AdapterSmokeBot, RegistryHealthBot
from wsp_orchestrator.curator.kiwix import parse_catalog
from wsp_orchestrator.curator.registry_edit import (
    apply_to_registry_text,
    load_overlay,
    remove_from_overlay,
    upsert_overlay,
)
from wsp_orchestrator.jobs import JobKind, JobStatus
from wsp_orchestrator.orchestrator import LastCopyHook, Orchestrator
from wsp_orchestrator.policy import Candidate, CandidateKind, Decision, GateConfig, Proposal
from wsp_orchestrator.testing import FakeFetcher, FakeWorld, make_settings, make_spec, record_promotions

T0 = datetime(2026, 10, 15, 6, tzinfo=UTC)
FILES = {"a.zim": b"a" * 1000}
FOURS: dict[str, int] = dict.fromkeys(
    ("authority", "density", "uniqueness", "license", "bulk", "health", "longevity", "cost"), 4
)


def build(
    tmp_path: Path, sources: list[SourceSpec], **wiring: Any
) -> tuple[Orchestrator, FakeWorld, ManualClock]:
    clock = ManualClock(T0)
    world = FakeWorld.build(tmp_path, clock, sources)
    orch = Orchestrator(
        make_settings(tmp_path, scrub_slices=1), world.wiring(**wiring), clock=clock, check_local=False
    )
    return orch, world, clock


@dataclass
class StubBot:
    name: str = "stub"
    cadence: str | None = "P1W"
    network: bool = False
    results: list[BotResult] = field(default_factory=list[BotResult])
    seen_previous: list[Mapping[str, object]] = field(default_factory=list[Mapping[str, object]])

    def run(self, ctx: BotContext) -> BotResult:
        self.seen_previous.append(ctx.previous)
        return self.results.pop(0) if self.results else BotResult(notes={"n": 0})


def _candidate(key: str, kind: CandidateKind = CandidateKind.KIWIX_TITLE) -> Candidate:
    return Candidate(
        key=key,
        name=key,
        kind=kind,
        license="CC-BY-SA-4.0",
        domain=("encyclopedia",),
        found_by="stub",
        scores=FOURS,
        size_bytes=10,
        category="wikipedia",
        registry_entry={"id": key.replace(":", "-"), "name": key},
    )


def test_bots_are_scheduled_gated_recorded_and_promoted(tmp_path: Path) -> None:
    bot = StubBot(
        results=[
            BotResult(
                candidates=[_candidate("kiwix:a"), _candidate("x:new", CandidateKind.NEW_SOURCE)],
                alerts=[Alert("bot-alert", Severity.WARNING, "hello")],
                notes={"n": 2},
            ),
            BotResult(candidates=[_candidate("kiwix:a")], resolved=["bot-alert"]),
        ]
    )
    promoted: list[Proposal] = []
    files = ProposalFiles(tmp_path / "proposals")
    gate = GateConfig(
        license_allowlist=frozenset({"CC-BY-SA-4.0"}),
        kiwix_categories=frozenset({"wikipedia"}),
        domain_budget_bytes={"encyclopedia": 100},
    )
    orch, world, clock = build(
        tmp_path, [], bots=[bot], gate=gate, promoter=promoted.append, proposal_recorder=files.write
    )
    orch.start()
    orch.run_pending()
    assert [p.candidate.key for p in promoted] == ["kiwix:a"]
    docs = {d["candidate"]["key"]: d for d in files.list()}
    assert docs["kiwix:a"]["status"] == "applied"
    assert docs["x:new"]["status"] == "open"
    assert docs["x:new"]["decision"] == Decision.REVIEW.value
    assert "bot-alert" in orch.alerter.open_keys()
    assert orch.bot_runs()["stub"]["candidates"] == 2
    assert world.ledger.of_kind("curator.bot")

    clock.advance(timedelta(days=8))  # weekly cadence: runs again, sees its previous notes
    orch.run_pending()
    assert bot.seen_previous[-1] == {"n": 2}
    assert "bot-alert" not in orch.alerter.open_keys()
    assert len(files.list()) == 2, "a candidate key is proposed once"


def test_parity_job_defers_while_work_remains(tmp_path: Path) -> None:
    calls: list[int] = []

    def parity(last_copy: LastCopyHook) -> Mapping[str, object]:
        calls.append(1)
        return {"pending": 2 - len(calls), "in_progress": "s/g" if len(calls) < 2 else None, "sets_done": 1}

    orch, world, clock = build(tmp_path, [], parity=parity)
    orch.start()
    orch.run_pending()
    assert len(calls) == 1
    job = orch.queue.active(JobKind.PARITY, "backfill")
    assert job is not None
    clock.advance(timedelta(minutes=3))
    orch.run_pending()
    assert len(calls) == 2
    assert orch.queue.active(JobKind.PARITY, "backfill") is None
    assert len(world.ledger.of_kind("parity.run")) == 2


def test_prune_keeps_generations_upstream_no_longer_serves(tmp_path: Path) -> None:
    spec = make_spec("src", raw={"retention": {"gfs": {"last": 1, "monthly": 0, "yearly": 0}}})
    clock = ManualClock(T0)
    world = FakeWorld.build(tmp_path, clock, [spec])
    orch = Orchestrator(
        make_settings(tmp_path), world.wiring(pruner=world.vault.prune), clock=clock, check_local=False
    )
    for gid in ("2026-07-01", "2026-08-01", "2026-09-01"):
        world.vault.add_sealed("src", gid)
    world.vault.add_sealed("src", "2026-10-01", current=True)
    record_promotions(orch.store, "src", ["2026-10-01"], T0)
    # upstream still serves 2026-08-01 and 2026-10-01; 07 and 09 are gone (last copies)
    world.adapters["src"].add_release("2026-08-01", FILES)
    world.adapters["src"].add_release("2026-10-01", FILES)
    # No observation yet: nothing can be proven redundant.
    gen = Generation("src", "2026-08-01", world.vault.generations("src")[1].status)
    assert orch.holds_last_copy("src", gen)
    orch._upstream(spec)  # pyright: ignore[reportPrivateUsage] - records the offers
    assert not orch.holds_last_copy("src", gen)
    orch.queue.enqueue(JobKind.PRUNE, "src", now=T0, payload={"source_id": "src"})
    for job in orch.claim_ready():
        orch.execute(job)
    assert world.vault.pruned == [("src", "2026-08-01")]
    plan = world.ledger.of_kind("retention.plan")[-1]
    assert plan.data["dry_run"] is False
    assert set(plan.data["last_copy"]) == {"2026-07-01", "2026-09-01"}  # type: ignore[arg-type]


def _never_last(source_id: str, generation: Generation) -> bool:
    return False


def _no_ctx(spec: SourceSpec) -> Any:
    return None


def test_prune_dry_run_flag(tmp_path: Path) -> None:
    spec = make_spec("src", raw={"retention": {"gfs": {"last": 1, "monthly": 0, "yearly": 0}}})
    clock = ManualClock(T0)
    world = FakeWorld.build(tmp_path, clock, [spec])
    orch = Orchestrator(
        make_settings(tmp_path, prune_dry_run=True),
        world.wiring(pruner=world.vault.prune, last_copy=_never_last),
        clock=clock,
        check_local=False,
    )
    world.vault.add_sealed("src", "2026-09-01")
    world.vault.add_sealed("src", "2026-10-01", current=True)
    record_promotions(orch.store, "src", ["2026-10-01"], T0)
    orch.queue.enqueue(JobKind.PRUNE, "src", now=T0, payload={"source_id": "src"})
    for job in orch.claim_ready():
        orch.execute(job)
    assert world.vault.pruned == []
    assert world.ledger.of_kind("retention.plan")[-1].data["prune"] == ["2026-09-01"]
    assert orch.queue.jobs(status=JobStatus.DONE)


# --- concrete bots ---------------------------------------------------------------------------------


def _ctx(tmp_path: Path, sources: list[SourceSpec], docs: Mapping[str, bytes], **kw: Any) -> BotContext:
    def no_adapter(spec: SourceSpec) -> Any:
        raise LookupError(spec.id)

    return BotContext(
        fetcher=FakeFetcher(dict(docs)),
        sources=sources,
        now=T0,
        user_agent="t",
        workdir=tmp_path,
        gate=GateConfig(),
        adapter_for=kw.pop("adapter_for", no_adapter),
        ctx_for=kw.pop("ctx_for", no_adapter),
        **kw,
    )


OPDS = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/">
<totalResults>3</totalResults>
%s</feed>"""


def _entry(name: str, flavour: str, length: int) -> bytes:
    url = f"https://download.kiwix.org/zim/x/{name}_{flavour}_2026-09.zim.meta4"
    return (
        f"<entry><title>{name}</title><name>{name}</name><flavour>{flavour}</flavour>"
        f"<category>wikivoyage</category><language>eng</language><updated>2026-09-01T00:00:00Z</updated>"
        f'<link rel="http://opds-spec.org/acquisition/open-access" href="{url}" length="{length}"/></entry>'
    ).encode()


def test_kiwix_watcher_proposes_uncovered_titles(tmp_path: Path) -> None:
    body = OPDS % (
        _entry("wikivoyage_en_all", "maxi", 100)
        + _entry("wikivoyage_en_all", "nopic", 50)
        + _entry("wikipedia_en_medicine", "maxi", 5)
    )
    entries, total = parse_catalog(body)
    assert total == 3
    assert len(entries) == 3
    url = "https://library.kiwix.org/catalog/v2/entries?category=wikivoyage&lang=eng&count=500&start=0"
    covered = make_spec(
        "wikipedia-en-zim",
        adapter="kiwix-opds",
        raw={"access": {"adapter": "kiwix-opds", "selector": {"name": "wikipedia_en_all"}}},
    )
    result = KiwixWatcher(categories=["wikivoyage"]).run(_ctx(tmp_path, [covered], {url: body}))
    (cand,) = result.candidates
    assert cand.key == "kiwix:wikivoyage_en_all"
    assert cand.license == "CC-BY-SA-4.0"
    assert cand.registry_entry["access"] == {
        "adapter": "kiwix-opds",
        "selector": {"name": "wikivoyage_en_all", "flavour": "maxi"},
    }
    assert result.notes["redundant_subsets"] == 1


def test_wikimedia_ranking_detects_a_changed_top(tmp_path: Path) -> None:
    spec = make_spec("wikipedia-top10", raw={"languages": ["en", "de"], "scores": FOURS})
    url = "https://{}.wikipedia.org/w/api.php?action=query&meta=siteinfo&siprop=statistics&format=json"
    docs = {
        url.format(lang): json.dumps({"query": {"statistics": {"articles": n}}}).encode()
        for lang, n in (("en", 7), ("de", 3), ("fr", 5))
    }
    result = WikimediaRankingBot(candidates=["en", "de", "fr"], top=2).run(_ctx(tmp_path, [spec], docs))
    (cand,) = result.candidates
    assert cand.kind is CandidateKind.NEW_VARIANT
    assert cand.registry_entry["languages"] == ["en", "fr"]
    same = WikimediaRankingBot(candidates=["en", "de"], top=2).run(_ctx(tmp_path, [spec], docs))
    assert not same.candidates


def test_registry_health_alerts_after_two_failed_probes(tmp_path: Path) -> None:
    spec = make_spec("dead", raw={"access": {"endpoints": ["https://gone.example/"]}})
    bot = RegistryHealthBot()
    first = bot.run(_ctx(tmp_path, [spec], {}))
    assert first.alerts == []
    second = bot.run(_ctx(tmp_path, [spec], {}, previous=first.notes))
    assert [a.key for a in second.alerts] == ["registry-health:dead"]
    assert second.candidates[0].kind is CandidateKind.REGISTRY_FIX
    ok = bot.run(_ctx(tmp_path, [spec], {"https://gone.example/": b"hi"}))
    assert ok.resolved == ["registry-health:dead"]


def test_adapter_smoke_reports_contract_problems(tmp_path: Path) -> None:
    world = FakeWorld.build(tmp_path, ManualClock(T0), [make_spec("src")])
    adapter = world.adapters["src"]
    smoke = AdapterSmokeBot()

    def adapter_for(spec: SourceSpec) -> Any:
        return adapter

    ctx = _ctx(tmp_path, world.sources, {}, adapter_for=adapter_for, ctx_for=_no_ctx)
    bad = smoke.run(ctx)
    assert "no release" in str(bad.notes["adapters"])
    adapter.add_release("2026-10-01", FILES)
    good = smoke.run(ctx)
    assert good.resolved == ["adapter-contract:src"]


def test_gap_miner_groups_broken_links_and_empty_searches(tmp_path: Path) -> None:
    gaps, queries = tmp_path / "gaps.jsonl", tmp_path / "queries.jsonl"
    at = T0.isoformat()
    gaps.write_text(
        "".join(json.dumps({"at": at, "href": f"https://www.nature.com/a{i}"}) + "\n" for i in range(5))
        + json.dumps({"at": at, "href": "wsp://src/missing"})
        + "\n"
    )
    queries.write_text(
        "".join(json.dumps({"at": at, "q": "Bird Migration routes", "total": 0}) + "\n" for _ in range(5))
        + json.dumps({"at": "2020-01-01T00:00:00+00:00", "q": "old", "total": 0})
        + "\n"
    )
    result = GapMiner(gaps_path=gaps, queries_path=queries).run(_ctx(tmp_path, [make_spec("src")], {}))
    keys = [c.key for c in result.candidates]
    assert "gap:domain:nature.com" in keys
    assert "gap:query:bird migration routes" in keys
    assert "gap:word:migration" in keys
    assert not any("src" in k for k in keys), "a page missing from a held source is not a new source"


def test_registry_text_edit_and_overlay(tmp_path: Path) -> None:
    text = "schema_version: 1\nsources:\n  - id: a\n    name: A\n\n  - id: b\n    name: B\n"
    replaced = apply_to_registry_text(text, {"id": "a", "name": "A2", "domain": ["x"]})
    assert yaml.safe_load(replaced)["sources"][0] == {"id": "a", "name": "A2", "domain": ["x"]}
    assert replaced.endswith("  - id: b\n    name: B\n")
    # 2026-10-26 review #58: the next section's comment and the blank line before it stay.
    sectioned = "sources:\n  - id: a\n    name: A\n\n  # --- Section B ---\n  - id: b\n    name: B\n"
    edited = apply_to_registry_text(sectioned, {"id": "a", "name": "A2"})
    assert edited.endswith("\n\n  # --- Section B ---\n  - id: b\n    name: B\n")
    appended = apply_to_registry_text(text, {"id": "c", "name": "C"})
    assert [s["id"] for s in yaml.safe_load(appended)["sources"]] == ["a", "b", "c"]
    overlay = tmp_path / "registry.local.yaml"
    upsert_overlay(overlay, {"id": "c", "name": "C"})
    upsert_overlay(overlay, {"id": "c", "name": "C2"})
    assert load_overlay(overlay) == [{"id": "c", "name": "C2"}]
    assert remove_from_overlay(overlay, "c")
    assert load_overlay(overlay) == []


def test_a_release_over_an_issuers_ftp_service_is_well_formed_and_plain_http_is_not() -> None:
    """NCBI's PubMed files come over ftp:// (owner decision D16, 2026-10-24)."""
    from wsp_orchestrator.curator.health import _release_problems  # pyright: ignore[reportPrivateUsage]

    ftp = [("ftp://ftp.ncbi.nlm.nih.gov/pubmed/baseline/pubmed26n0001.xml.gz", "pubmed26n0001.xml.gz")]
    assert _release_problems("pubmed2026", ftp) == []
    plain = [("http://example.org/a.gz", "a.gz")]
    assert _release_problems("v1", plain) == ["a.gz: not https (http://example.org/a.gz)"]
