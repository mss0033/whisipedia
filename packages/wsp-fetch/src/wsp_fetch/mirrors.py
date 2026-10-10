"""Mirror catalogs: Wikimedia dumps (config data) and Kiwix (from Metalink) (PLAN §6.5).

The Wikimedia list lives in ``data/wikimedia_mirrors.toml`` and needs periodic refresh; mirrors
are only ever *candidates* — the fetcher probes each one and drops any that 404 or advertise a
different size, and every byte is verified against upstream digests regardless of origin.
"""

from __future__ import annotations

import datetime as dt
import logging
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from wsp_core.model import RemoteFile
from wsp_fetch.config import host_key
from wsp_fetch.metalink import parse_metalink

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Mirror:
    name: str
    base: str  # ends with "/"
    connections: int = 2
    wikis: tuple[str, ...] = ()  # empty = all
    enabled: bool = True
    retention: str = ""


@dataclass(frozen=True, slots=True)
class MirrorCatalog:
    origin: str
    mirrors: tuple[Mirror, ...]
    reviewed: dt.date | None = None
    refresh_by: dt.date | None = None

    def is_stale(self, today: dt.date | None = None) -> bool:
        return self.refresh_by is not None and (today or dt.date.today()) > self.refresh_by

    def urls_for(self, url: str) -> tuple[str, ...]:
        """All candidate URLs (origin first) for a file under ``origin``; unknown URLs map to themselves."""
        if not url.startswith(self.origin):
            return (url,)
        rel = url[len(self.origin) :]
        wiki = rel.split("/", 1)[0]
        out = [url]
        for m in self.mirrors:
            if m.enabled and (not m.wikis or wiki in m.wikis):
                out.append(m.base + rel)
        return tuple(dict.fromkeys(out))

    def with_mirrors(self, remote: RemoteFile) -> RemoteFile:
        """``remote`` with every catalog mirror appended to ``mirrors``."""
        extra = [u for u in self.urls_for(remote.url) if u != remote.url]
        return replace(remote, mirrors=tuple(dict.fromkeys([*remote.mirrors, *extra])))

    def host_connections(self) -> dict[str, int]:
        """Per-host caps to merge into ``FetchConfig.host_connections``."""
        return {host_key(m.base): m.connections for m in self.mirrors if m.enabled}


def _parse_catalog(raw: Mapping[str, Any]) -> MirrorCatalog:
    mirrors: list[Mirror] = []
    for entry in cast(list[dict[str, Any]], raw.get("mirror", [])):
        base = str(entry["base"])
        mirrors.append(
            Mirror(
                name=str(entry.get("name", urlsplit(base).hostname or base)),
                base=base if base.endswith("/") else base + "/",
                connections=int(entry.get("connections", 2)),
                wikis=tuple(cast(list[str], entry.get("wikis", []))),
                enabled=bool(entry.get("enabled", True)),
                retention=str(entry.get("retention", "")),
            )
        )
    reviewed = raw.get("reviewed")
    refresh_by = raw.get("refresh_by")
    return MirrorCatalog(
        origin=str(raw["origin"]),
        mirrors=tuple(mirrors),
        reviewed=reviewed if isinstance(reviewed, dt.date) else None,
        refresh_by=refresh_by if isinstance(refresh_by, dt.date) else None,
    )


def load_wikimedia_catalog(path: Path | None = None) -> MirrorCatalog:
    """Load the packaged Wikimedia mirror list (or an override file); warns when it is due a refresh."""
    if path is None:
        text = (
            resources.files("wsp_fetch").joinpath("data/wikimedia_mirrors.toml").read_text(encoding="utf-8")
        )
    else:
        text = path.read_text(encoding="utf-8")
    catalog = _parse_catalog(tomllib.loads(text))
    if catalog.is_stale():
        log.warning("Wikimedia mirror list was due a refresh on %s", catalog.refresh_by)
    return catalog


def wikimedia_mirror_urls(url: str, catalog: MirrorCatalog | None = None) -> tuple[str, ...]:
    return (catalog or load_wikimedia_catalog()).urls_for(url)


# --- Kiwix ------------------------------------------------------------------------------------


def kiwix_metalink_url(url: str) -> str:
    """Kiwix (MirrorBrain) serves a Metalink v4 next to every file."""
    return url + ".meta4"


def kiwix_mirrors(meta4: bytes, name: str | None = None) -> tuple[str, ...]:
    """Mirror URLs from a Kiwix ``.meta4`` document, best priority first."""
    files = parse_metalink(meta4)
    chosen = next((f for f in files if name is None or f.name == name), files[0])
    return chosen.mirror_urls()


def kiwix_mirror_bases(meta4: bytes, relpath: str) -> tuple[str, ...]:
    """Base URLs of Kiwix mirrors: each mirror URL with ``relpath`` (e.g. ``zim/x/y.zim``) stripped."""
    out: list[str] = []
    for u in kiwix_mirrors(meta4):
        if u.endswith(relpath):
            out.append(u[: -len(relpath)])
    return tuple(dict.fromkeys(out))
