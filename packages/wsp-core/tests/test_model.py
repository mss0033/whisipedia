from wsp_core.model import ReleaseState, allowed_transitions


def test_forward_path_and_failure() -> None:
    assert allowed_transitions(ReleaseState.FETCHED) == {ReleaseState.VERIFIED, ReleaseState.FAILED}
    assert allowed_transitions(ReleaseState.FAILED) == {
        ReleaseState.PLANNED,
        ReleaseState.QUARANTINED,
    }
    assert allowed_transitions(ReleaseState.PROMOTED) == frozenset()
