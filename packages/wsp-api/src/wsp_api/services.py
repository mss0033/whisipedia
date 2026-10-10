"""What the API needs from the rest of the system: ports only, injected by a composition root."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from wsp_core.model import SourceSpec
from wsp_core.ports import ChangeFeed, DocumentStore, Generator, Ledger, LexicalIndex, Vault


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Services:
    """Ports backing the API. Real ones come from ``wsp_cli.bootstrap``; fakes from ``wsp_api.testing``."""

    index: LexicalIndex
    documents: DocumentStore
    changes: ChangeFeed
    vault: Vault
    ledger: Ledger
    sources: Sequence[SourceSpec]
    generator: Generator | None = None  # None: /v1/summarize answers 501
    clock: Callable[[], datetime] = _utcnow

    def source(self, source_id: str) -> SourceSpec | None:
        return next((s for s in self.sources if s.id == source_id), None)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8737
LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")


@dataclass(frozen=True, slots=True)
class ApiSettings:
    """Server behaviour that is not a port. Built from the ``[api]`` config section."""

    static_dir: Path | None = None  # Navigator bundle (navigator/dist); None serves a placeholder page
    allowed_hosts: tuple[str, ...] = LOOPBACK_HOSTS  # Host-header allow-list (DNS-rebinding defence)
    sse_poll_seconds: float = 1.0
    sse_heartbeat_seconds: float = 15.0
    ledger_tail: int = 20
