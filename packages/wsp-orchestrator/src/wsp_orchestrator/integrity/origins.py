"""Origin pinning (trust on first use) and drift detection per source (ADR-0009).

The first time a source is fetched, every host involved is pinned in the local state DB together
with the family of the CA that issued its certificate (``Let's Encrypt``, ``DigiCert Inc`` ...).
Later fetches are compared with the pins:

* an **origin** or **checksum** host (where the release and its digests come from) on a domain
  never seen for this source blocks the release (held for ``wsp approve``); a new host on a
  known domain raises an alert;
* a new **mirror** host (bytes that are checked against an independent digest anyway) is only
  recorded, unless the bytes could not be verified independently, then it raises an alert;
* a changed issuer family on a pinned origin/checksum host raises an alert (certificate
  rotation within the same CA is normal and only recorded).

Pure functions over plain values; the guard persists the pins.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from wsp_orchestrator.integrity.findings import Action, Finding

ROLES = ("origin", "checksum", "mirror")
TRUST_ROLES = frozenset({"origin", "checksum"})

# Common two-label public suffixes; enough to tell "mirror.example.ac.uk" from "example.ac.uk"
# without shipping the public suffix list (we only compare domains a source already used).
_MULTI_LABEL_SUFFIXES = frozenset(
    {
        "ac.at", "ac.be", "ac.cn", "ac.il", "ac.in", "ac.jp", "ac.kr", "ac.nz", "ac.uk", "ac.za",
        "co.at", "co.id", "co.il", "co.in", "co.jp", "co.kr", "co.nz", "co.uk", "co.za",
        "com.ar", "com.au", "com.br", "com.cn", "com.hk", "com.mx", "com.sg", "com.tr", "com.tw",
        "edu.au", "edu.cn", "edu.tw", "go.jp", "gov.au", "gov.uk", "ne.jp", "net.au", "or.jp",
        "org.au", "org.uk",
    }
)  # fmt: skip


def registrable_domain(host: str) -> str:
    """``dumps.wikimedia.org`` -> ``wikimedia.org``; ``ftp.acc.umu.se`` -> ``umu.se``; IPs unchanged."""
    host = host.strip().lower().rstrip(".")
    try:
        ipaddress.ip_address(host.strip("[]"))
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


@dataclass(frozen=True, slots=True)
class OriginPin:
    source_id: str
    host: str
    role: str
    issuer_family: str | None
    cert_sha256: str | None
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True, slots=True)
class HostSeen:
    """One host involved in a fetch."""

    host: str
    role: str  # origin | checksum | mirror
    url: str = ""
    issuer_family: str | None = None
    cert_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class OriginResult:
    findings: tuple[Finding, ...]
    pins: tuple[OriginPin, ...]  # new or refreshed pins to store (never the blocked hosts)
    blocked: tuple[HostSeen, ...]  # hosts held for the owner's decision


def _merge(seen: Sequence[HostSeen]) -> list[HostSeen]:
    """One entry per host, keeping the most trusted role and any TLS detail."""
    order = {r: i for i, r in enumerate(ROLES)}
    out: dict[str, HostSeen] = {}
    for h in seen:
        if not h.host:
            continue
        prev = out.get(h.host)
        if prev is None:
            out[h.host] = h
            continue
        role = h.role if order[h.role] < order[prev.role] else prev.role
        out[h.host] = replace(
            prev,
            role=role,
            url=prev.url or h.url,
            issuer_family=prev.issuer_family or h.issuer_family,
            cert_sha256=prev.cert_sha256 or h.cert_sha256,
        )
    return list(out.values())


def evaluate_origins(
    source_id: str,
    pins: Sequence[OriginPin],
    seen: Sequence[HostSeen],
    *,
    now: datetime,
    independently_verified: bool,
    unknown_origin: Action = Action.HOLD,
) -> OriginResult:
    """Compare what a fetch used with the pins of ``source_id`` (see module docstring)."""
    hosts = _merge(seen)
    by_host = {p.host: p for p in pins}
    if not pins:  # trust on first use: pin everything, say so once
        new = tuple(
            OriginPin(source_id, h.host, h.role, h.issuer_family, h.cert_sha256, now, now) for h in hosts
        )
        finding = Finding(
            "origin-first-trust",
            Action.INFO,
            f"first fetch: pinned {len(new)} host(s) for {source_id}",
            {"hosts": sorted(h.host for h in hosts)},
        )
        return OriginResult((finding,), new, ())
    domains = {registrable_domain(p.host) for p in pins}
    findings: list[Finding] = []
    out: list[OriginPin] = []
    blocked: list[HostSeen] = []
    for h in hosts:
        pin = by_host.get(h.host)
        trusted_role = h.role in TRUST_ROLES
        if pin is None:
            known_domain = registrable_domain(h.host) in domains
            data: dict[str, object] = {"host": h.host, "role": h.role, "url": h.url}
            if trusted_role and not known_domain:
                findings.append(
                    Finding(
                        "origin-unknown-domain",
                        unknown_origin,
                        f"{h.role} host {h.host} is on a domain never used by {source_id}",
                        data,
                    )
                )
                if unknown_origin >= Action.HOLD:
                    blocked.append(h)
                    continue
            elif trusted_role:
                findings.append(
                    Finding(
                        "origin-new-host", Action.ALERT, f"new {h.role} host {h.host} (known domain)", data
                    )
                )
            else:
                action = Action.INFO if independently_verified else Action.ALERT
                findings.append(Finding("mirror-new-host", action, f"new mirror host {h.host}", data))
            out.append(OriginPin(source_id, h.host, h.role, h.issuer_family, h.cert_sha256, now, now))
            continue
        if h.issuer_family and pin.issuer_family and h.issuer_family != pin.issuer_family:
            role = pin.role if pin.role in TRUST_ROLES else h.role
            findings.append(
                Finding(
                    "origin-issuer-changed",
                    Action.ALERT if role in TRUST_ROLES else Action.INFO,
                    f"{h.host}: certificate issuer changed from {pin.issuer_family!r} to {h.issuer_family!r}",
                    {"host": h.host, "role": role, "was": pin.issuer_family, "now": h.issuer_family},
                )
            )
        elif h.cert_sha256 and pin.cert_sha256 and h.cert_sha256 != pin.cert_sha256:
            findings.append(
                Finding(
                    "origin-cert-rotated",
                    Action.INFO,
                    f"{h.host}: new certificate from the same CA family",
                    {"host": h.host},
                )
            )
        role = h.role if ROLES.index(h.role) < ROLES.index(pin.role) else pin.role
        out.append(
            replace(
                pin,
                role=role,
                issuer_family=h.issuer_family or pin.issuer_family,
                cert_sha256=h.cert_sha256 or pin.cert_sha256,
                last_seen=now,
            )
        )
    return OriginResult(tuple(findings), tuple(out), tuple(blocked))
