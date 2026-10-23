"""Fetcher settings, built from the ``[acquirer]`` config section (PLAN §6.5, §12.2)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any, cast
from urllib.parse import urlsplit

DEFAULT_USER_AGENT = "Whisipedia/0.1 (personal offline archive)"  # config overrides it
UNVERIFIED_POLICIES = ("corroborate", "allow", "refuse")


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
    that welcome more (see ``wsp_fetch.mirrors``). Caps apply to the host named in the URL we were
    given, not to the host a redirect lands on.

    ``unverified_policy`` decides what happens to a file whose upstream publishes neither a checksum
    nor Metalink piece hashes (vault-fetch audit #8):

    * ``corroborate`` (default): download it a second time, in full, preferably from another source,
      and accept it only if both downloads have the same size and digests
      (``FetchReport.corroborated``; a disagreement raises ``ChecksumMismatchError`` and the first
      copy is kept as ``<name>.disputed``). Costs the file's size again in transfer.
    * ``allow``: accept a single download; the report says ``integrity == "unverified"`` so the
      caller can see it and decide (never silently).
    * ``refuse``: raise ``UnverifiableError``.

    ``user_agent`` must not contain ``@``: an e-mail address in a header is sent to every mirror.
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
    # Probe ``<url>.meta4`` on these hosts. Not download.kiwix.org: its robots.txt disallows every agent
    # (2026-10-23); the catalog's links and the metalinks are on lb.download.kiwix.org.
    metalink_hosts: tuple[str, ...] = ("lb.download.kiwix.org",)
    # Honour each host's robots.txt on every request, mirror and redirect (owner decision 2026-10-21:
    # strictly; until 2026-10-23 only the document collections checked it). Off only in tests.
    robots: bool = True
    state_interval: float = 2.0  # seconds between durable resume checkpoints
    verify_readback: bool = False  # drop page cache and re-hash from disk before rename (SMB, ADR-0002)
    spot_checks: int = 2  # random ranges re-read from every mirror and compared (ADR-0009); 0 = off
    spot_check_bytes: int = 64 * 1024
    unverified_policy: str = "corroborate"  # see the class docstring
    max_reresolve: int = 5  # re-probes per mirror when a signed redirect URL expires (401/403/410)
    stall_timeout: float = 900.0  # aria2c: kill it when the file has not grown for this long
    lock_dir: str | None = None  # local directory for per-destination fetch locks (None: temp dir)

    def __post_init__(self) -> None:
        if "@" in self.user_agent:
            raise ValueError("acquirer.user_agent must not contain '@' (no e-mail addresses in requests)")
        if self.unverified_policy not in UNVERIFIED_POLICIES:
            raise ValueError(f"acquirer.unverified_policy must be one of {UNVERIFIED_POLICIES}")

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
