"""Remembered, shared link checks (wsp_api.offload.LinkChecks)."""

from __future__ import annotations

import threading

from wsp_api.offload import DaemonPool, LinkChecks


def test_a_lookup_that_ends_late_answers_the_next_round_at_once() -> None:
    release = threading.Event()
    calls: list[str] = []

    def check(address: str) -> bool | None:
        calls.append(address)
        if address == "slow":
            release.wait(5)
        return address != "here"

    checks = LinkChecks(DaemonPool(4, "t"))
    missing, unchecked = checks.missing(["here", "slow"], check, timeout=0.5)
    assert (missing, unchecked) == ([], ["slow"])  # "here" is there; "slow" not answered yet
    # Asked again while it still runs: the same lookup, not a second one.
    assert checks.missing(["slow"], check, timeout=0.1) == ([], ["slow"])
    release.set()
    assert checks.missing(["slow"], check, timeout=2) == (["slow"], [])
    assert checks.missing(["here", "slow"], check, timeout=0.1) == (["slow"], [])  # both remembered
    assert sorted(calls) == ["here", "slow"]


def test_a_busy_answer_is_not_remembered_and_an_old_one_expires() -> None:
    answers = iter([None, True])

    def check(address: str) -> bool | None:
        return next(answers)

    checks = LinkChecks(DaemonPool(1, "t"), ttl=0.0)
    assert checks.missing(["x"], check, timeout=1) == ([], ["x"])  # busy: asked again next time
    assert checks.missing(["x"], check, timeout=1) == (["x"], [])
