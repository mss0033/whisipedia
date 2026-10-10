"""Load and validate the source registry (PLAN §5.1)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import jsonschema
import yaml

from wsp_core.model import SourceSpec, SourceStatus, Tier, UpdateStrategy


class RegistryError(ValueError):
    pass


def _jsonable(value: object) -> object:
    # YAML parses bare dates into date objects; the schema expects strings.
    return json.loads(json.dumps(value, default=str))


def merge_overlay(data: object, overlay: object) -> object:
    """Entries of ``overlay["sources"]`` replace same-id entries of ``data`` or are appended."""
    original: object = data
    if not isinstance(data, dict) or not isinstance(overlay, dict):
        return original
    base = cast(dict[str, Any], data)
    extra: object = cast(dict[str, Any], overlay).get("sources")
    if not isinstance(extra, list) or not extra:
        return original
    entries = [cast(dict[str, Any], e) for e in cast(list[object], extra) if isinstance(e, dict)]
    by_id = {str(e.get("id")): e for e in entries}
    merged = [by_id.pop(str(e.get("id")), e) for e in cast(list[dict[str, Any]], base.get("sources", []))]
    out: dict[str, Any] = {**base, "sources": [*merged, *by_id.values()]}
    return out


def load_registry(
    registry_path: Path, schema_path: Path, *, overlay: Path | None = None
) -> Sequence[SourceSpec]:
    """Load and validate the registry; ``overlay`` (``config/registry.local.yaml``, if it exists) is
    merged first, so the result is validated as a whole (PLAN §5.4 auto-promotions)."""
    data = _jsonable(yaml.safe_load(registry_path.read_text(encoding="utf-8")))
    if overlay is not None and overlay.is_file():
        data = merge_overlay(data, _jsonable(yaml.safe_load(overlay.read_text(encoding="utf-8"))))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        raise RegistryError(f"{registry_path}: {exc.message} at {list(exc.absolute_path)}") from exc

    entries = cast(list[dict[str, Any]], cast(dict[str, Any], data)["sources"])
    ids = [e["id"] for e in entries]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise RegistryError(f"duplicate source ids: {dupes}")
    return tuple(_to_spec(e) for e in entries)


def _to_spec(entry: Mapping[str, Any]) -> SourceSpec:
    access = cast(Mapping[str, Any], entry.get("access") or {})
    update = cast(Mapping[str, Any], entry.get("update") or {})
    strategy = update.get("strategy")
    return SourceSpec(
        id=entry["id"],
        name=entry["name"],
        tier=Tier(entry["tier"]),
        status=SourceStatus(entry["status"]),
        license=entry["license"],
        legal_basis=entry["legal_basis"],
        domain=tuple(entry["domain"]),
        adapter=access.get("adapter"),
        cadence=update.get("cadence"),
        strategy=UpdateStrategy(strategy) if strategy else None,
        irreplaceable=bool(entry.get("irreplaceable", False)),
        raw=dict(entry),
    )
