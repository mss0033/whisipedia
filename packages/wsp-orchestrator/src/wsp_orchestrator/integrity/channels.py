"""Independent verification channels: digests fetched apart from the bytes (ADR-0009).

A digest that arrives with the bytes it describes proves little against a tampered mirror. After a
download, the guard fetches the digest again through a second channel and compares:

* **Wikimedia dumps** (``wikimedia-dumps``, ``wikidata``): ``<wiki>-<date>-sha1sums.txt`` and
  ``-md5sums.txt`` from the **primary** (``dumps.wikimedia.org``), while the bytes usually come
  from mirrors;
* **Kiwix** (``kiwix-opds``): ``<file>.sha256`` from ``lb.download.kiwix.org`` (the bytes come from
  the mirrors its Metalink lists; the Metalink's own hashes are cross-checked by the fetcher);
* **everything else**: ``<url>.<algorithm>`` sidecars, preferring a mirror that did not serve the
  bytes, then the origin.

The channel is *independent* when its host served none of the bytes, *same-origin* when it did,
*unavailable* when no channel answered. Any channel digest that disagrees with the bytes or with
the digest declared at discovery is a *mismatch*: the release is quarantined.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from wsp_core.model import RemoteFile

DEFAULT_WIKIMEDIA_PRIMARY = "https://dumps.wikimedia.org/"
# Not download.kiwix.org: its robots.txt disallows every agent (2026-10-23); lb serves the same sidecars.
DEFAULT_KIWIX_PRIMARY = "https://lb.download.kiwix.org/"
SIDECAR_ALGORITHMS = ("sha256", "sha1", "md5", "sha512")
_HEX = re.compile(r"^[0-9a-fA-F]{32,128}$")
_BSD = re.compile(r"^(?P<algo>[A-Za-z0-9-]+)\s*\((?P<name>.+)\)\s*=\s*(?P<hex>[0-9a-fA-F]+)$")
_HEX_LEN = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}


@dataclass(frozen=True, slots=True)
class ChannelRef:
    url: str
    algorithm: str | None  # None: any algorithm the document lists for the file
    name: str  # file name to look up in a multi-file checksum document
    kind: str  # "primary-list" | "primary-sidecar" | "mirror-sidecar" | "origin-sidecar"


@dataclass(frozen=True, slots=True)
class ChannelObservation:
    url: str
    host: str
    kind: str
    digests: Mapping[str, str] = field(default_factory=dict[str, str])
    error: str | None = None
    tls_issuer_family: str | None = None
    tls_sha256: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "url": self.url,
            "host": self.host,
            "kind": self.kind,
            "digests": dict(self.digests),
            "error": self.error,
            "tls_issuer_family": self.tls_issuer_family,
            "tls_sha256": self.tls_sha256,
        }


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _swap_base(url: str, primary: str) -> str:
    """Re-root ``url``'s path at ``primary`` when it is not already there (a mirror URL)."""
    p, u = urlsplit(primary), urlsplit(url)
    if (u.hostname or "").lower() == (p.hostname or "").lower():
        return url
    return urlunsplit((p.scheme, p.netloc, u.path, "", ""))


def wikimedia_refs(remote: RemoteFile, primary: str = DEFAULT_WIKIMEDIA_PRIMARY) -> list[ChannelRef]:
    """``<dir>/<wiki>-<date>-sha1sums.txt`` (and md5) on the primary dumps server."""
    url = _swap_base(remote.url, primary)
    directory, _, name = url.rpartition("/")
    parts = name.split("-")
    if len(parts) < 3:
        return []
    prefix = f"{parts[0]}-{parts[1]}"
    return [
        ChannelRef(f"{directory}/{prefix}-{algo}sums.txt", algo, name, "primary-list")
        for algo in ("sha1", "md5")
    ]


def kiwix_refs(remote: RemoteFile, primary: str = DEFAULT_KIWIX_PRIMARY) -> list[ChannelRef]:
    url = _swap_base(remote.url, primary)
    return [ChannelRef(url + ".sha256", "sha256", url.rpartition("/")[2], "primary-sidecar")]


def sidecar_refs(remote: RemoteFile, byte_hosts: Iterable[str], *, limit: int = 2) -> list[ChannelRef]:
    """``<url>.<algo>`` on a mirror that served no bytes first, then the origin."""
    served = set(byte_hosts)
    algos = [c.algorithm.lower() for c in remote.checksums if c.algorithm.lower() in SIDECAR_ALGORITHMS]
    if not algos:
        return []
    algo = algos[0]
    candidates = [m for m in remote.mirrors if host_of(m) not in served]
    out = [
        ChannelRef(f"{m}.{algo}", algo, m.rpartition("/")[2], "mirror-sidecar") for m in candidates[:limit]
    ]
    if len(out) < limit:
        out.append(ChannelRef(f"{remote.url}.{algo}", algo, remote.name, "origin-sidecar"))
    return out


def channel_refs(
    adapter: str | None,
    remote: RemoteFile,
    byte_hosts: Iterable[str],
    *,
    wikimedia_primary: str = DEFAULT_WIKIMEDIA_PRIMARY,
    kiwix_primary: str = DEFAULT_KIWIX_PRIMARY,
) -> list[ChannelRef]:
    match adapter:
        case "wikimedia-dumps" | "wikidata":
            return wikimedia_refs(remote, wikimedia_primary)
        case "kiwix-opds":
            return kiwix_refs(remote, kiwix_primary)
        case _:
            return sidecar_refs(remote, byte_hosts)


def parse_checksum_doc(text: str, name: str, algorithm: str | None) -> dict[str, str]:
    """Digests for ``name`` in a checksum document.

    Understands ``<hex>  <name>`` / ``<hex> *<name>`` lists (sha1sums.txt), BSD style
    ``SHA256 (<name>) = <hex>`` and single-digest sidecars (``<hex>`` or ``<hex>  <name>``).
    """
    out: dict[str, str] = {}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    for line in lines:
        bsd = _BSD.match(line)
        if bsd:
            if bsd["name"].rpartition("/")[2] == name:
                out[bsd["algo"].lower().replace("-", "")] = bsd["hex"].lower()
            continue
        tokens = line.split()
        if not tokens or not _HEX.match(tokens[0]):
            continue
        digest = tokens[0].lower()
        listed = tokens[1].lstrip("*").rpartition("/")[2] if len(tokens) > 1 else None
        if listed is not None and listed != name:
            continue
        algo = algorithm or _HEX_LEN.get(len(digest))
        if algo is None or (algorithm is not None and _HEX_LEN.get(len(digest)) not in (None, algorithm)):
            continue
        out[algo] = digest
    return out


GetBytes = Callable[[str], bytes]
ObservationOf = Callable[[str], Mapping[str, object] | None]


def observe_channels(
    refs: Sequence[ChannelRef], get_bytes: GetBytes, observation_of: ObservationOf | None = None
) -> list[ChannelObservation]:
    out: list[ChannelObservation] = []
    for ref in refs:
        try:
            text = get_bytes(ref.url).decode("utf-8", errors="replace")
        except Exception as exc:  # 404, network: the channel is unavailable, which is not a mismatch
            out.append(
                ChannelObservation(ref.url, host_of(ref.url), ref.kind, error=f"{type(exc).__name__}: {exc}")
            )
            continue
        digests = parse_checksum_doc(text, ref.name, ref.algorithm)
        seen = observation_of(ref.url) if observation_of is not None else None
        tls = seen.get("tls") if seen else None
        tls_map: Mapping[str, object] = tls if isinstance(tls, Mapping) else {}  # pyright: ignore[reportUnknownVariableType]
        final = str(seen.get("final_url") or ref.url) if seen else ref.url
        out.append(
            ChannelObservation(
                ref.url,
                host_of(final),
                ref.kind,
                digests,
                None if digests else f"no digest for {ref.name}",
                str(tls_map.get("issuer_family")) if tls_map.get("issuer_family") else None,
                str(tls_map.get("sha256")) if tls_map.get("sha256") else None,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class ChannelVerdict:
    status: str  # independent | same-origin | unavailable | mismatch
    detail: str
    observations: tuple[ChannelObservation, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.status,
            "detail": self.detail,
            "channels": [o.to_json() for o in self.observations],
        }


def judge_channels(
    observations: Sequence[ChannelObservation],
    *,
    ours: Mapping[str, str],
    declared: Mapping[str, str],
    byte_hosts: Iterable[str],
) -> ChannelVerdict:
    """Compare channel digests with the bytes (``ours``) and with the digests declared at discovery."""
    served = set(byte_hosts)
    agreed: list[ChannelObservation] = []
    compared = False
    for obs in observations:
        for algo, value in obs.digests.items():
            for label, ref in (("downloaded bytes", ours), ("discovery", declared)):
                if algo in ref:
                    compared = True
                    if ref[algo].lower() != value.lower():
                        return ChannelVerdict(
                            "mismatch",
                            f"{obs.url}: {algo} {value} disagrees with {label} ({ref[algo]})",
                            tuple(observations),
                        )
        if obs.digests and any(a in ours for a in obs.digests):
            agreed.append(obs)
    if not agreed:
        why = "no channel compared" if not compared else "no channel covered our digests"
        errors = "; ".join(f"{o.url}: {o.error}" for o in observations if o.error)
        return ChannelVerdict("unavailable", f"{why}{': ' + errors if errors else ''}", tuple(observations))
    if any(o.host not in served for o in agreed):
        hosts = sorted({o.host for o in agreed if o.host not in served})
        return ChannelVerdict("independent", f"digest confirmed via {', '.join(hosts)}", tuple(observations))
    return ChannelVerdict(
        "same-origin",
        "digest confirmed only by a host that also served bytes",
        tuple(observations),
    )
