"""Whisipedia source adapters (PLAN §6.1-6.2) and snapshot diffing (PLAN §6.4).

Adapters are registered under the ``whisipedia.adapters`` entry-point group and looked up by the
``access.adapter`` name used in ``registry/sources.yaml``.
"""

from __future__ import annotations

from importlib.metadata import entry_points

from wsp_core.ports import SourceAdapter

__version__ = "0.1.0"

ENTRY_POINT_GROUP = "whisipedia.adapters"


def available_adapters() -> tuple[str, ...]:
    """Names of every installed adapter."""
    return tuple(sorted(ep.name for ep in entry_points(group=ENTRY_POINT_GROUP)))


def load_adapter(name: str) -> SourceAdapter:
    """Instantiate the adapter registered as ``name``."""
    matches = entry_points(group=ENTRY_POINT_GROUP, name=name)
    if not matches:
        raise LookupError(f"no adapter named {name!r}; installed: {available_adapters()}")
    factory = next(iter(matches)).load()
    adapter = factory()
    if not isinstance(adapter, SourceAdapter):
        raise TypeError(f"entry point {name!r} does not build a SourceAdapter")
    return adapter
