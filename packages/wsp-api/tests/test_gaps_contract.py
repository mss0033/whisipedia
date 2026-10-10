from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest

from wsp_api.gaps import JsonlGapLog
from wsp_core.model import Gap
from wsp_core.ports import GapLog
from wsp_core.testing.gap_log_contract import GapLogContract


class TestJsonlGapLogContract(GapLogContract):
    @pytest.fixture
    def gap_log(self, tmp_path: Path) -> GapLog:
        return JsonlGapLog(tmp_path / "gaps.jsonl")

    def read_back(self, gap_log: GapLog) -> Sequence[Gap]:
        assert isinstance(gap_log, JsonlGapLog)
        out: list[Gap] = []
        for line in gap_log.path.read_text(encoding="utf-8").splitlines():
            obj = json.loads(line)
            out.append(Gap(datetime.fromisoformat(obj["at"]), obj["href"], obj["from_doc_id"]))
        return out
