"""Film and recordings are their own kinds (the Screening Room): the registry's media domains map to
them, and the kind list the Navigator compiles against carries them."""

from __future__ import annotations

from wsp_api.schemas import DocKind
from wsp_api.sites import kind_of
from wsp_core.model import SourceSpec, SourceStatus, Tier


def spec(*domain: str) -> SourceSpec:
    return SourceSpec(
        id="x",
        name="X",
        tier=Tier.T2,
        status=SourceStatus.CANDIDATE,
        license="CC0-1.0",
        legal_basis="public-domain",
        domain=domain,
        adapter="doc-collection",
        raw={},
    )


def test_media_domains_map_to_the_screening_room_kinds() -> None:
    assert kind_of(spec("film")) is DocKind.VIDEO
    assert kind_of(spec("lectures", "science")) is DocKind.VIDEO  # the first media domain wins
    assert kind_of(spec("audiobooks")) is DocKind.AUDIO
    assert kind_of(spec("radio")) is DocKind.AUDIO
    assert kind_of(spec("music")) is DocKind.AUDIO
    assert kind_of(spec("howto")) is DocKind.OTHER
    assert {DocKind.VIDEO.value, DocKind.AUDIO.value} <= {k.value for k in DocKind}
