"""Repository-wide test guard: tests never see a machine's ``config/local.toml``.

``config/local.toml`` points at the real vault (NAS) and index. Without this, any test that loads the
repository's config directory would read or write live data.
"""

import os

os.environ["WSP_NO_LOCAL_CONFIG"] = "1"

import pytest


@pytest.fixture(autouse=True)
def _no_real_shutdown_probe(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """A heavy step that ends without a result waits for a shutdown to show and asks systemd whether
    the machine is stopping (``IsolatedRunner._ended_by_shutdown``). Tests must neither wait 2 s per
    such case nor depend on the state of the machine they run on."""
    try:
        from wsp_orchestrator import isolation
    except ImportError:  # a checkout without the orchestrator package installed
        return
    monkeypatch.setattr(isolation, "system_stopping", lambda: False)
    monkeypatch.setattr(isolation, "SHUTDOWN_GRACE_SECONDS", 0.05)
