from pathlib import Path

import pytest
from adapters_testkit import make_spec

from wsp_adapters._common import available_cpus, default_workers, parse_checksum_file, workers_for


def test_worker_cap_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WSP_MAX_WORKERS", "2")
    assert default_workers() <= 2
    monkeypatch.setenv("WSP_MAX_WORKERS", "junk")
    assert default_workers() >= 1


def test_registry_workers_never_exceed_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit 2026-10-11 LOW: ``access.workers`` used to bypass ``WSP_MAX_WORKERS``."""
    spec = make_spec("s", {"adapter": "x", "workers": 16})
    monkeypatch.setenv("WSP_MAX_WORKERS", "3")
    assert workers_for(spec) == 3
    monkeypatch.delenv("WSP_MAX_WORKERS")
    assert workers_for(spec) == 16
    assert workers_for(make_spec("s", {"adapter": "x"}), 4) == 4


def test_cgroup_cpu_quota_lowers_the_cpu_count(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("200000 100000\n")  # 200% of one CPU
    assert available_cpus(tmp_path) <= 2
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert available_cpus(tmp_path) >= 1


def test_bare_digest_sidecar_names_the_file() -> None:
    digest = "ab" * 32
    assert parse_checksum_file(f"{digest}\n", "x.zim") == {"x.zim": digest}
    assert parse_checksum_file(f"{digest}  y.zim\n", "x.zim") == {"y.zim": digest}
    assert parse_checksum_file("garbage", "x.zim") == {}
