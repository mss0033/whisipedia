"""Structured JSON logging (PLAN §12.6). Pass structured fields with ``extra={"fields": {...}}``."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import IO, Any, cast

_ROOT = "wsp"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            for k, v in cast(Mapping[str, Any], fields).items():
                out.setdefault(k, v)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


class JsonHandler(logging.StreamHandler[IO[str]]):
    pass


def configure_logging(level: str = "INFO", stream: IO[str] | None = None) -> logging.Handler:
    """Attach one JSON handler to the ``wsp`` logger tree (idempotent). Returns the handler."""
    logger = logging.getLogger(_ROOT)
    for h in list(logger.handlers):
        if isinstance(h, JsonHandler):
            logger.removeHandler(h)
    handler = JsonHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False
    return handler
