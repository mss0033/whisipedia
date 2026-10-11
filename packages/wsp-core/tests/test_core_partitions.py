import pytest

from wsp_core.model import SourceSpec, SourceStatus, Tier, Verdict, VerdictStatus
from wsp_core.partitions import (
    PartitionError,
    check_partition,
    current_set_ids,
    group_by_partition,
    partition_generation_id,
    partition_of,
    same_partition,
)


def test_partition_of() -> None:
    assert partition_of("2026-10-11.ca") == "ca"
    assert partition_of("2026-10-11.b0001") == "b0001"
    assert partition_of("2026-10-11") is None
    assert partition_of(".ca") is None
    assert partition_of("2026-10-11.CA") is None
    assert partition_of("2026-10-11.") is None


def test_partition_generation_id() -> None:
    assert partition_generation_id("2026-10-11", "ca") == "2026-10-11.ca"
    assert partition_generation_id("2026-10-11.ca", "ca") == "2026-10-11.ca"  # resume a cycle
    with pytest.raises(PartitionError):
        partition_generation_id("2026-10-11", "C A")
    assert check_partition("b0001") == "b0001"
    with pytest.raises(PartitionError):
        check_partition("-x")


def test_grouping_and_current_set() -> None:
    ids = ["2026-01-01.ak", "2026-02-01.ca", "2026-03-01.ak", "2025-01-01"]
    assert group_by_partition(ids, str) == {
        "ak": ["2026-01-01.ak", "2026-03-01.ak"],
        "ca": ["2026-02-01.ca"],
        None: ["2025-01-01"],
    }
    assert same_partition(ids, "2027-01-01.ak") == ["2026-01-01.ak", "2026-03-01.ak"]
    pointers = {"ak": "2026-03-01.ak", "ca": "2026-02-01.ca"}
    assert current_set_ids(pointers, "2026-03-01.ak") == ["2026-02-01.ca", "2026-03-01.ak"]
    assert current_set_ids(pointers, "2025-01-01") == ["2025-01-01", "2026-02-01.ca", "2026-03-01.ak"]
    assert current_set_ids({}, None) == []


def test_source_spec_partitioned_and_policy_verdicts() -> None:
    def spec(raw: dict[str, object]) -> SourceSpec:
        return SourceSpec("s", "S", Tier.T2, SourceStatus.CANDIDATE, "PD", "pd", ("maps",), raw=raw)

    assert spec({"access": {"partitioned": True}}).partitioned
    assert not spec({"access": {"partitioned": False}}).partitioned
    assert not spec({}).partitioned
    for status in (VerdictStatus.REJECTED, VerdictStatus.HELD):
        assert Verdict(status).withheld
        assert not Verdict(status).ok
    assert not Verdict(VerdictStatus.MISMATCH).withheld
