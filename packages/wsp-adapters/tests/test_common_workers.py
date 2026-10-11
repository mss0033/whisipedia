import pytest

from wsp_adapters._common import default_workers


def test_worker_cap_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WSP_MAX_WORKERS", "2")
    assert default_workers() <= 2
    monkeypatch.setenv("WSP_MAX_WORKERS", "junk")
    assert default_workers() >= 1
