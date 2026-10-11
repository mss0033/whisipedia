"""Child side of :class:`~wsp_orchestrator.isolation.IsolatedRunner`.

``python -m wsp_orchestrator.child <module:function> <request.json> <result.json>`` calls
``function(payload)`` and writes ``{"ok": true, "value": ...}`` or, when it raises,
``{"ok": false, "error": "...", "kind": <FailureKind>}`` atomically to ``result.json``. A child that is
killed (OOM, signal) writes nothing; the parent reads that as a crash.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from wsp_orchestrator.errors import classify
from wsp_orchestrator.isolation import write_json_atomic
from wsp_orchestrator.jsonlog import configure_logging

log = logging.getLogger("wsp.orchestrator.child")

ChildFunction = Callable[[Mapping[str, Any]], Any]


def resolve(target: str) -> ChildFunction:
    module_name, sep, attr = target.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(f"child target must be 'package.module:function', not {target!r}")
    return cast(ChildFunction, getattr(importlib.import_module(module_name), attr))


def run(target: str, request: Path, result: Path) -> int:
    """Call ``target`` with the request payload; write the result file. Returns the exit code."""
    try:
        payload = cast(Mapping[str, Any], json.loads(request.read_text(encoding="utf-8")))
        value = resolve(target)(payload)
    except Exception as exc:
        kind = classify(exc)
        log.exception("child step failed", extra={"fields": {"target": target, "kind": kind.value}})
        write_json_atomic(result, {"ok": False, "error": f"{type(exc).__name__}: {exc}", "kind": kind.value})
        return 1
    write_json_atomic(result, {"ok": True, "value": value})
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        print(
            "usage: python -m wsp_orchestrator.child MODULE:FUNCTION REQUEST.json RESULT.json",
            file=sys.stderr,
        )
        return 2
    configure_logging("INFO")
    return run(args[0], Path(args[1]), Path(args[2]))


if __name__ == "__main__":
    raise SystemExit(main())
