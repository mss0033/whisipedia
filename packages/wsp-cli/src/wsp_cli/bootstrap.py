"""Composition root: the one place that turns configuration into concrete port implementations.

Everything else (API, CLI commands) receives a :class:`wsp_api.Services` and never imports an
implementation. Today every backend is an in-memory fake seeded with a small sample library; each
``TODO(integration)`` marks where the lead wires the real component, selected by config (PLAN §12.2).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from wsp_api import ApiSettings, Services
from wsp_api.services import LOOPBACK_HOSTS
from wsp_api.testing.fakes import (
    SAMPLE_SOURCES,
    EchoGenerator,
    InMemoryChangeFeed,
    InMemoryDocumentStore,
    InMemoryLedger,
    InMemoryLexicalIndex,
    InMemoryVault,
    seed_sample_library,
)
from wsp_core.config import load_config
from wsp_core.model import SourceSpec
from wsp_core.registry import load_registry

BACKEND_NOTE = "using in-memory sample backends (real vault/index not wired yet)"


@dataclass(frozen=True, slots=True)
class Layout:
    """Where the repository-relative inputs live. ``root`` is None outside a checkout."""

    root: Path | None
    config_dir: Path
    registry: Path
    schema: Path
    navigator_dist: Path


def find_root(start: Path | None = None) -> Path | None:
    here = (start or Path.cwd()).resolve()
    for parent in (here, *here.parents):
        if (parent / "registry" / "sources.yaml").is_file():
            return parent
    return None


def layout(config_dir: Path | None = None, *, environ: Mapping[str, str] | None = None) -> Layout:
    env = os.environ if environ is None else environ
    root = find_root()
    base = root or Path.cwd()
    cfg_dir = config_dir or Path(env.get("WSP_CONFIG_DIR", base / "config"))
    return Layout(
        root=root,
        config_dir=cfg_dir,
        registry=base / "registry" / "sources.yaml",
        schema=base / "registry" / "schema" / "source.schema.json",
        navigator_dist=base / "navigator" / "dist",
    )


def load(lay: Layout, *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    return load_config(lay.config_dir, environ=environ)


def _section(cfg: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = cfg.get(name, {})
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def load_sources(cfg: Mapping[str, Any], lay: Layout) -> Sequence[SourceSpec]:
    reg = _section(cfg, "registry")
    registry = Path(reg.get("path", lay.registry))
    schema = Path(reg.get("schema", lay.schema))
    if not registry.is_file():
        return ()
    return load_registry(registry, schema)


def api_settings(cfg: Mapping[str, Any], lay: Layout, *, static_dir: Path | None = None) -> ApiSettings:
    api = _section(cfg, "api")
    static = static_dir or (Path(api["static_dir"]) if "static_dir" in api else lay.navigator_dist)
    extra = tuple(str(h) for h in api.get("allowed_hosts", ()))
    return ApiSettings(static_dir=static, allowed_hosts=(*LOOPBACK_HOSTS, *extra))


def build_services(cfg: Mapping[str, Any], lay: Layout) -> Services:
    """Build the ports from config. Nothing here may open a network connection (PLAN §10.5)."""
    # TODO(integration): vault = wsp_vault.FileVault(Path(cfg["vault"]["root"]))  (read-only mount)
    vault = InMemoryVault()
    # TODO(integration): ledger = wsp_vault.FileLedger(<vault root>/ledger)
    ledger = InMemoryLedger()
    # TODO(integration): index = wsp_index.TantivyLexicalIndex(Path(cfg["index"]["root"]), read_only=True)
    index = InMemoryLexicalIndex()
    # TODO(integration): documents = wsp_index.ParquetDocumentStore(<index root>, vault)
    documents = InMemoryDocumentStore()
    # TODO(integration): changes = wsp_index.SqliteChangeFeed(<index root>/changes.db)
    changes = InMemoryChangeFeed()
    # TODO(integration): generator = wsp_oracle.LocalGenerator(cfg["oracle"]) when configured; must be a
    # loopback llama.cpp/vLLM endpoint or in-process model. None makes /v1/summarize answer 501.
    generator = EchoGenerator() if _section(cfg, "api").get("demo_generator", True) else None

    seed_sample_library(index=index, documents=documents, changes=changes, vault=vault, ledger=ledger)
    return Services(
        index=index,
        documents=documents,
        changes=changes,
        vault=vault,
        ledger=ledger,
        sources=load_sources(cfg, lay) or SAMPLE_SOURCES,  # sample specs outside a checkout
        generator=generator,
    )


# --- Secret redaction for `wsp config show` -----------------------------------------------------

_SECRET_KEY = re.compile(
    r"(secret|token|passw|credential|private|api[_-]?key|(^|_)key$|signing)", re.IGNORECASE
)
REDACTED = "***"


def redact(cfg: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in cfg.items():
        if isinstance(value, Mapping):
            out[key] = redact(cast(Mapping[str, Any], value))
        elif _SECRET_KEY.search(key):
            out[key] = REDACTED
        else:
            out[key] = value
    return out
