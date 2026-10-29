"""wsp quality run: a drop is measured against the best recent run, not only the previous one."""

from __future__ import annotations

import json
from pathlib import Path

from wsp_cli.quality import REGRESSION, baseline, regression


def test_a_drop_stays_a_drop_until_quality_recovers(tmp_path: Path) -> None:
    """2026-10-26 review #46: week 3 at the same low score resolved week 2's alert."""
    for name, score in (("2026-10-01.json", 0.62), ("2026-10-08.json", 0.55), ("2026-10-15.json", 0.55)):
        (tmp_path / name).write_text(json.dumps({"ndcg10": score}))
    best = baseline(tmp_path, "2026-10-22.json")
    assert best is not None
    assert best["ndcg10"] == 0.62
    fallen = regression(0.55, best)
    assert fallen is not None
    assert fallen > REGRESSION  # still a regression in week 4
    back = regression(0.61, best)
    assert back is not None
    assert back <= REGRESSION  # recovered
