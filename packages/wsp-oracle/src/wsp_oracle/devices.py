"""Optional heavy dependencies (``wsp-oracle[gpu]``) and device selection.

``torch`` and ``transformers`` are imported dynamically: the rest of the workspace installs, type-
checks and tests without them (CI has no GPU). The modules are typed ``Any`` at this one seam; the
classes that use them expose fully typed interfaces. Importing sets the Hugging Face libraries to
offline mode, so a running Oracle never reaches the network (PLAN §10.5).
"""

from __future__ import annotations

import importlib
import os
from functools import cache
from typing import Any


class OracleDependencyError(RuntimeError):
    """A model backend was requested but ``wsp-oracle[gpu]`` is not installed."""


def offline_env() -> None:
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        os.environ.setdefault(key, "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "true")


def _import(name: str) -> Any:
    offline_env()
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise OracleDependencyError(
            f"{name} is not installed; install the model backends with `uv sync --all-packages --extra gpu`"
        ) from exc


@cache
def torch() -> Any:
    return _import("torch")


@cache
def transformers() -> Any:
    mod = _import("transformers")
    mod.logging.set_verbosity_error()
    return mod


def has_torch() -> bool:
    try:
        torch()
    except OracleDependencyError:
        return False
    return True


def cuda_available() -> bool:
    return has_torch() and bool(torch().cuda.is_available())


def resolve_device(pref: str) -> str:
    """``auto`` → ``cuda`` when a GPU is visible, else ``cpu``. An explicit ``cuda`` without one raises."""
    if pref == "auto":
        return "cuda" if cuda_available() else "cpu"
    if pref.startswith("cuda") and not cuda_available():
        raise OracleDependencyError(f"device {pref!r} requested but no CUDA GPU is available")
    return pref
