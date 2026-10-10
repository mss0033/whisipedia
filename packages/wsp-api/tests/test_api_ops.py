"""System Monitor operations data, proposals and the local query log."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from wsp_api import create_app
from wsp_api.gaps import JsonlGapLog, JsonlQueryLog, rotate
from wsp_api.schemas import OpsStatus, ProposalInfo, ProposalPage, ProposalSummary, SystemStatus
from wsp_api.seams import OpsProvider, QueryLog
from wsp_api.testing import local_client
from wsp_api.testing.fakes import sample_services
from wsp_core.model import Gap

PROPOSAL = ProposalInfo(
    id=7,
    key="kiwix:x",
    title="registry: add X",
    status="open",
    decision="review",
    kind="kiwix-title",
    found_by="kiwix-opds",
)


class FakeOps:
    def status(self, now: datetime) -> OpsStatus:
        return OpsStatus(
            slo=[],
            disk=[],
            budgets=[],
            lanes=[],
            queue=[],
            alerts=[],
            bots=[],
            proposals=ProposalSummary(open=1, latest=[PROPOSAL]),
            orchestrator_state=True,
        )

    def proposals(self, status: str | None = None) -> Sequence[ProposalInfo]:
        return [PROPOSAL] if status in (None, "open") else []


def test_system_includes_ops_and_proposals_are_listed(tmp_path: Path) -> None:
    assert isinstance(FakeOps(), OpsProvider)
    log = JsonlQueryLog(tmp_path / "queries.jsonl")
    assert isinstance(log, QueryLog)
    services = replace(sample_services(), ops=FakeOps(), queries=log)
    with local_client(create_app(services)) as c:
        system = SystemStatus.model_validate(c.get("/v1/system").json())
        assert system.ops is not None
        assert system.ops.proposals.open == 1
        page = ProposalPage.model_validate(c.get("/v1/proposals").json())
        assert [p.id for p in page.items] == [7]
        assert ProposalPage.model_validate(c.get("/v1/proposals?status=rejected").json()).items == []
        assert c.get("/v1/proposals?status=bogus").status_code == 422
        c.get("/v1/search", params={"q": "zzzzqqq"})
        c.get("/v1/search", params={"q": "turing"})
    lines = [json.loads(x) for x in (tmp_path / "queries.jsonl").read_text().splitlines()]
    assert [(x["q"], x["total"] == 0) for x in lines] == [("zzzzqqq", True), ("turing", False)]


def test_system_without_ops_and_empty_proposals() -> None:
    with local_client(create_app(sample_services())) as c:
        assert c.get("/v1/system").json()["ops"] is None
        assert c.get("/v1/proposals").json() == {"items": []}


def test_logs_rotate(tmp_path: Path) -> None:
    gaps = JsonlGapLog(tmp_path / "gaps.jsonl", max_bytes=200, keep=2)
    for i in range(20):
        gaps.record(Gap(at=datetime(2026, 10, 10), href=f"https://example.org/{i}"))
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["gaps.jsonl", "gaps.jsonl.1", "gaps.jsonl.2"]
    rotate(tmp_path / "missing.jsonl")
