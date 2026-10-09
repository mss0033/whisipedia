"""Layered configuration (PLAN §12.2): defaults < local file < environment.

Environment overrides use ``WSP__<SECTION>__<KEY>=value`` (double underscores), e.g.
``WSP__VAULT__ROOT=/mnt/z/whisipedia``. Values from the environment are parsed as TOML
scalars when possible (``true``, ``42``) and fall back to plain strings.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

ENV_PREFIX = "WSP__"


def _merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _merge(cast(dict[str, Any], out[key]), cast(Mapping[str, Any], value))
        else:
            out[key] = value
    return out


def _parse_scalar(raw: str) -> object:
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw


def _env_layer(environ: Mapping[str, str]) -> dict[str, Any]:
    layer: dict[str, Any] = {}
    for name, raw in environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in name[len(ENV_PREFIX) :].split("__") if p]
        if not parts:
            continue
        node = layer
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = _parse_scalar(raw)
    return layer


def load_config(config_dir: Path, *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Load ``default.toml`` then ``local.toml`` (optional, gitignored) then environment overrides."""
    cfg: dict[str, Any] = {}
    for name in ("default.toml", "local.toml"):
        path = config_dir / name
        if path.is_file():
            cfg = _merge(cfg, tomllib.loads(path.read_text(encoding="utf-8")))
    return _merge(cfg, _env_layer(os.environ if environ is None else environ))
