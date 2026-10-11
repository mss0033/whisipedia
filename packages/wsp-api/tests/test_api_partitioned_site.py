"""A partitioned source (ADR-0010) is one Navigator site made of every current partition."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath

from wsp_api.convert import source_info
from wsp_core.model import Artifact, Generation, GenerationStatus, SourceSpec, SourceStatus, Tier

SPEC = SourceSpec(
    id="usgs-historical-topo",
    name="USGS historical topographic maps",
    tier=Tier.T1,
    status=SourceStatus.CANDIDATE,
    license="LicenseRef-US-Gov-PD",
    legal_basis="public-domain",
    domain=("geography", "maps"),
    adapter="doc-collection",
    raw={"access": {"adapter": "doc-collection", "partitioned": True}},
)


def gen(gid: str, size: int, day: int) -> Generation:
    art = Artifact(SPEC.id, gid, PurePosixPath(f"upstream/{gid}.pdf"), size, "0" * 64, "0" * 64)
    return Generation(SPEC.id, gid, GenerationStatus.SEALED, (art,), datetime(2026, 10, day, tzinfo=UTC))


def test_the_site_is_every_partition_not_the_last_one_promoted() -> None:
    hi, ri = gen("2026-10-11.hi", 300, 11), gen("2026-10-09.ri", 200, 9)
    info = source_info(SPEC, ri, members=[ri, hi])  # CURRENT.txt names the partition promoted last
    assert info.bytes == 500
    assert info.current_generation == "2026-10-11.hi"  # the newest partition
    assert info.as_of == hi.sealed_at


def test_an_ordinary_source_is_its_current_generation() -> None:
    g = gen("2026-10-11", 300, 11)
    info = source_info(SPEC, g)
    assert (info.bytes, info.current_generation) == (300, "2026-10-11")
    assert source_info(SPEC, None).bytes is None
