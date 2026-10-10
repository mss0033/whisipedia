"""Fetcher settings, built from the ``[acquirer]`` config section (PLAN §6.5, §12.2)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any, cast
from urllib.parse import urlsplit

DEFAULT_USER_AGENT = "Whisipedia/0.1 (personal offline archive)"  # config overrides it


def host_key(url: str) -> str:
    """Politeness key for a URL: ``host[:port]`` lowercased (caps apply per host, not per mirror URL)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return f"{host}:{parts.port}" if parts.port else host


@dataclass(frozen=True, slots=True)
class FetchConfig:
    """Tunables for :class:`~wsp_fetch.http.HttpFetcher` and :class:`~wsp_fetch.aria2.Aria2Fetcher`.

    ``max_connections_per_host`` is the default concurrent-request cap; Wikimedia's dumps server
    allows 2 per client IP. ``host_connections`` raises (or lowers) it for named hosts, e.g. mirrors
    that welcome more (see ``wsp_fetch.mirrors``).
    """

    user_agent: str = DEFAULT_USER_AGENT
    max_connections_per_host: int = 2
    host_connections: Mapping[str, int] = field(default_factory=dict[str, int])
    bandwidth_limit: int | None = None  # bytes/s across all downloads of this fetcher; None = unlimited
    piece_size: int = 8 << 20
    min_piece_size: int = 1 << 20
    hash_window_bytes: int = 256 << 20  # in-memory piece buffers kept for the in-order hasher
    connect_timeout: float = 15.0
    read_timeout: float = 60.0
    max_attempts: int = 6  # per request (get_bytes) and per piece across mirrors
    mirror_failure_limit: int = 4  # consecutive failures before a mirror is dropped
    backoff_base: float = 0.5
    backoff_max: float = 60.0
    max_retry_after: float = 600.0
    slow_mirror_factor: float = 4.0  # demote a mirror slower than best / factor
    endgame: bool = True  # duplicate straggling tail pieces on idle connections
    metalink_hosts: tuple[str, ...] = ("download.kiwix.org",)  # probe ``<url>.meta4`` on these hosts
    state_interval: float = 2.0  # seconds between durable resume checkpoints
    verify_readback: bool = False  # drop page cache and re-hash from disk before rename (SMB, ADR-0002)

    def connections_for(self, url_or_host: str) -> int:
        key = host_key(url_or_host) if "://" in url_or_host else url_or_host.lower()
        return max(1, int(self.host_connections.get(key, self.max_connections_per_host)))

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> FetchConfig:
        """Build from ``load_config(...)["acquirer"]``; unknown keys are ignored."""
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {k: v for k, v in section.items() if k in known}
        if "metalink_hosts" in kwargs:
            kwargs["metalink_hosts"] = tuple(cast(list[str], kwargs["metalink_hosts"]))
        if "host_connections" in kwargs:
            hc = cast(Mapping[str, int], kwargs["host_connections"])
            kwargs["host_connections"] = {str(k).lower(): int(v) for k, v in hc.items()}
        return cls(**kwargs)
