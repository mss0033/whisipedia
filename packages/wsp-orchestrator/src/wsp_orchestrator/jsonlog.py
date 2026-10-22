"""Structured JSON logging (PLAN §12.6). Pass structured fields with ``extra={"fields": {...}}``."""

from __future__ import annotations

import logging
import sys
from typing import IO

from wsp_core.diag import JsonFormatter  # one format for every structured log

_ROOT = "wsp"


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
