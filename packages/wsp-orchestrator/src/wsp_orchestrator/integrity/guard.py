"""The integrity guard: supply-chain and compromise checks around the release pipeline (ADR-0009).

Called by :class:`~wsp_orchestrator.pipeline.ReleasePipeline` at three points:

1. **after the download** (FETCHED -> VERIFIED): build the provenance record from what the fetcher
   saw, cross-check mirrors (advertised sizes, corrupt pieces, spot checks), fetch the digest again
   through an independent channel, and compare every host with the source's pins. The strongest
   finding decides: continue, alert, *hold* for the owner, or *quarantine* (never promoted; the
   source is paused);
2. **after sealing**: write the signed ``PROVENANCE.json`` next to the manifest;
3. **before promotion**: profile the generation, compare it with the last promoted one, write the
   signed ``PROMOTION.json`` and hold the release when a check is out of range.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from wsp_core.model import Generation, RemoteFile, SourceSpec
from wsp_core.ports import Fetcher
from wsp_orchestrator.alerts import Alert, Alerter, Severity
from wsp_orchestrator.clock import Clock
from wsp_orchestrator.integrity.anomaly import (
    AnomalyThresholds,
    Check,
    ContentProfile,
    PromotionReport,
    compare_profiles,
    profile_summary,
)
from wsp_orchestrator.integrity.channels import (
    DEFAULT_KIWIX_PRIMARY,
    DEFAULT_WIKIMEDIA_PRIMARY,
    ChannelObservation,
    channel_refs,
    host_of,
    judge_channels,
    observe_channels,
)
from wsp_orchestrator.integrity.findings import Action, Finding, strongest, summarize
from wsp_orchestrator.integrity.origins import HostSeen, OriginPin, evaluate_origins
from wsp_orchestrator.store import StateStore, parse_ts

log = logging.getLogger("wsp.orchestrator.integrity")

PROVENANCE_NAME = "PROVENANCE.json"
PROMOTION_NAME = "PROMOTION.json"
CHANNEL_MAX_BYTES = 16 * 1024 * 1024

AttestHook = Callable[[str, str, str, Mapping[str, Any]], str]
"""Sign and write ``name`` (PROVENANCE.json / PROMOTION.json) into a sealed generation; -> BLAKE3."""
ProfileHook = Callable[[SourceSpec, Generation, Path], ContentProfile]
"""Content profile of a sealed generation (``generation_dir`` is its vault directory)."""
ScrubbedHook = Callable[[str, str], bool]
"""True when the rolling scrub has verified every file of ``(source_id, generation_id)``."""


@dataclass(frozen=True, slots=True)
class IntegritySettings:
    """``[integrity]`` (actions: ``info``, ``alert``, ``hold``, ``quarantine``)."""

    enabled: bool = True
    mirror_disagreement: Action = Action.QUARANTINE  # mirrors disagree on size or bytes
    channel_mismatch: Action = Action.QUARANTINE  # an independent digest disagrees
    unknown_origin: Action = Action.HOLD  # origin/checksum host on a domain never used by the source
    unverifiable: Action = Action.ALERT  # no digest at all, from any channel
    wikimedia_primary: str = DEFAULT_WIKIMEDIA_PRIMARY
    kiwix_primary: str = DEFAULT_KIWIX_PRIMARY
    settle_days: int = 14  # a new generation is the known-good anchor only after this and its scrub
    anomaly: AnomalyThresholds = field(default_factory=AnomalyThresholds)

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> IntegritySettings:
        sec = cast(Mapping[str, Any], cfg.get("integrity", {}) or {})
        base = cls()

        def act(key: str, default: Action) -> Action:
            return Action.parse(str(sec[key])) if key in sec else default

        return cls(
            enabled=bool(sec.get("enabled", True)),
            mirror_disagreement=act("mirror_disagreement", base.mirror_disagreement),
            channel_mismatch=act("channel_mismatch", base.channel_mismatch),
            unknown_origin=act("unknown_origin", base.unknown_origin),
            unverifiable=act("unverifiable", base.unverifiable),
            wikimedia_primary=str(sec.get("wikimedia_primary", base.wikimedia_primary)),
            kiwix_primary=str(sec.get("kiwix_primary", base.kiwix_primary)),
            settle_days=int(sec.get("settle_days", base.settle_days)),
            anomaly=AnomalyThresholds.from_mapping(cast(Mapping[str, Any], sec.get("anomaly", {}) or {})),
        )


@dataclass
class IntegrityHooks:
    """What the composition root plugs in (``Wiring.integrity``)."""

    settings: IntegritySettings = field(default_factory=IntegritySettings)
    attest: AttestHook | None = None
    profile: ProfileHook | None = None
    scrubbed: ScrubbedHook | None = None
    profiles_dir: Path | None = None  # local disk; default: <state_db dir>/profiles


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    action: Action
    findings: tuple[Finding, ...]
    provenance: Mapping[str, Any]

    @property
    def reason(self) -> str:
        return summarize(self.findings, max(self.action, Action.HOLD))


def _ours(fetched: Mapping[str, Any]) -> dict[str, str]:
    out = {"sha256": str(fetched.get("sha256", "")), "blake3": str(fetched.get("blake3", ""))}
    for c in cast(list[Mapping[str, Any]], fetched.get("verified", [])):
        out.setdefault(str(c["algorithm"]).lower(), str(c["hexdigest"]).lower())
    return {k: v for k, v in out.items() if v}


def _hash_file(path: Path, algorithms: Sequence[str]) -> dict[str, str]:
    hashers = {
        a: hashlib.new(a, usedforsecurity=False) for a in algorithms if a in hashlib.algorithms_available
    }
    if not hashers:
        return {}
    with path.open("rb") as fh:
        while chunk := fh.read(8 * 1024 * 1024):
            for h in hashers.values():
                h.update(chunk)
    return {a: h.hexdigest() for a, h in hashers.items()}


def _tls(entry: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    tls = entry.get("tls") if entry else None
    if not isinstance(tls, Mapping):
        return None, None
    t = cast(Mapping[str, Any], tls)
    fam, sha = t.get("issuer_family"), t.get("sha256")
    return (str(fam) if fam else None), (str(sha) if sha else None)


class IntegrityGuard:
    def __init__(
        self,
        store: StateStore,
        fetcher: Fetcher,
        hooks: IntegrityHooks,
        *,
        clock: Clock,
        alerter: Alerter | None,
        profiles_dir: Path,
        ledger_event: Callable[[str, str, Mapping[str, object]], None] | None = None,
    ) -> None:
        self.store = store
        self.fetcher = fetcher
        self.hooks = hooks
        self.settings = hooks.settings
        self.clock = clock
        self.alerter = alerter
        self.profiles_dir = hooks.profiles_dir or profiles_dir
        self.ledger_event = ledger_event

    # --- 1. after the download ---------------------------------------------------------------------

    def check_fetch(
        self, spec: SourceSpec, version: str, files: Sequence[RemoteFile], fetched: Mapping[str, Any]
    ) -> GuardVerdict:
        now = self.clock.now()
        findings: list[Finding] = []
        seen: list[HostSeen] = []
        files_doc: list[dict[str, Any]] = []
        all_independent = True
        observation_of = cast(
            Callable[[str], Mapping[str, object] | None] | None, getattr(self.fetcher, "observation", None)
        )
        for remote in files:
            fj = cast(Mapping[str, Any], fetched.get(remote.name, {}))
            fprov = cast(Mapping[str, Any], fj.get("provenance") or {})
            sources = cast(list[Mapping[str, Any]], fprov.get("sources", []))
            byte_hosts = {str(s.get("host")) for s in sources if int(s.get("bytes", 0) or 0) > 0}
            if not fprov:
                findings.append(
                    Finding(
                        "no-provenance", Action.INFO, f"{remote.name}: the fetcher recorded no provenance"
                    )
                )
            findings += self._mirror_findings(remote.name, fprov)
            ours = _ours(fj)
            declared = {c.algorithm.lower(): c.hexdigest.lower() for c in remote.checksums}
            refs = channel_refs(
                spec.adapter,
                remote,
                byte_hosts,
                wikimedia_primary=self.settings.wikimedia_primary,
                kiwix_primary=self.settings.kiwix_primary,
            )
            obs = observe_channels(
                refs, lambda u: self.fetcher.get_bytes(u, max_bytes=CHANNEL_MAX_BYTES), observation_of
            )
            missing = sorted({a for o in obs for a in o.digests} - set(ours))
            local = Path(str(fj.get("path", "")))
            if missing and local.is_file():
                ours.update(_hash_file(local, missing))
            verdict = judge_channels(obs, ours=ours, declared=declared, byte_hosts=byte_hosts)
            all_independent = all_independent and verdict.status == "independent"
            if verdict.status == "mismatch":
                findings.append(
                    Finding(
                        "checksum-channel-mismatch",
                        self.settings.channel_mismatch,
                        f"{remote.name}: {verdict.detail}",
                        {"file": remote.name},
                    )
                )
            elif verdict.status == "unavailable":
                action = Action.INFO if declared else self.settings.unverifiable
                findings.append(
                    Finding("checksum-channel-unavailable", action, f"{remote.name}: {verdict.detail}")
                )
            elif verdict.status == "same-origin":
                findings.append(
                    Finding("checksum-same-origin", Action.INFO, f"{remote.name}: {verdict.detail}")
                )
            seen += self._hosts(remote, sources, obs, observation_of)
            files_doc.append(
                {
                    "name": remote.name,
                    "url": remote.url,
                    "declared": [{"algorithm": a, "hexdigest": d} for a, d in sorted(declared.items())],
                    "digests": ours,
                    "size": fj.get("size"),
                    "fetch": dict(fprov),
                    "channel": verdict.to_json(),
                }
            )
        pins = self._pins(spec.id)
        origin = evaluate_origins(
            spec.id,
            pins,
            seen,
            now=now,
            independently_verified=all_independent,
            unknown_origin=self.settings.unknown_origin,
        )
        for pin in origin.pins:
            self.store.put_origin_pin(
                pin.source_id,
                pin.host,
                pin.role,
                pin.issuer_family,
                pin.cert_sha256,
                pin.first_seen,
                pin.last_seen,
            )
        findings += origin.findings
        action = strongest(findings)
        provenance: dict[str, Any] = {
            "version": version,
            "checked_at": now.isoformat(),
            "verdict": action.label,
            "channel_status": "independent" if all_independent else "partial",
            "files": files_doc,
            "origins": {
                "hosts": [_host_json(h) for h in seen],
                "blocked": [_host_json(h) for h in origin.blocked],
            },
            "findings": [f.to_json() for f in findings],
        }
        self.store.put_provenance(spec.id, version, action.label, provenance, now)
        self.store.set_meta(
            f"channel:{spec.id}",
            _dumps(
                {
                    "at": now.isoformat(),
                    "version": version,
                    "status": [f["channel"]["status"] for f in files_doc],
                    "verdict": action.label,
                }
            ),
        )
        self._alerts(spec.id, version, findings)
        return GuardVerdict(action, tuple(findings), provenance)

    def _mirror_findings(self, name: str, fprov: Mapping[str, Any]) -> list[Finding]:
        out: list[Finding] = []
        act = self.settings.mirror_disagreement
        for r in cast(list[Mapping[str, Any]], fprov.get("rejected", [])):
            if r.get("reason") == "size-disagreement":
                out.append(
                    Finding(
                        "mirror-size-disagreement",
                        act,
                        f"{name}: {r.get('url')} advertises {r.get('size')} bytes,"
                        f" others {r.get('expected')}",
                        {"url": r.get("url"), "file": name},
                    )
                )
        for s in cast(list[Mapping[str, Any]], fprov.get("sources", [])):
            if int(s.get("corrupt_pieces", 0) or 0) > 0:
                out.append(
                    Finding(
                        "mirror-corrupt-pieces",
                        act,
                        f"{name}: {s.get('url')} served {s.get('corrupt_pieces')} piece(s)"
                        " failing their hash",
                        {"url": s.get("url"), "file": name},
                    )
                )
            if s.get("spot_check") == "mismatch":
                out.append(
                    Finding(
                        "mirror-content-disagreement",
                        act,
                        f"{name}: {s.get('url')} serves bytes that differ from the verified file",
                        {"url": s.get("url"), "file": name},
                    )
                )
        return out

    @staticmethod
    def _hosts(
        remote: RemoteFile,
        sources: Sequence[Mapping[str, Any]],
        obs: Sequence[ChannelObservation],
        observation_of: Callable[[str], Mapping[str, object] | None] | None,
    ) -> list[HostSeen]:
        out: list[HostSeen] = []
        origin = host_of(remote.url)
        first = next((s for s in sources if s.get("url") == remote.url), None)
        if first is None and observation_of is not None:
            first = cast(Mapping[str, Any] | None, observation_of(remote.url))
        fam, sha = (None, None)
        if first is not None and str(first.get("host") or "") == origin:
            fam, sha = _tls(first)
        out.append(HostSeen(origin, "origin", remote.url, fam, sha))
        for o in obs:
            if o.error is None:
                out.append(HostSeen(o.host, "checksum", o.url, o.tls_issuer_family, o.tls_sha256))
        for s in sources:
            for hop in cast(list[Mapping[str, Any]], s.get("redirects", [])):
                out.append(HostSeen(host_of(str(hop.get("url", ""))), "mirror", str(hop.get("url", ""))))
            if int(s.get("bytes", 0) or 0) > 0 or s.get("url") != remote.url:
                fam2, sha2 = _tls(s)
                out.append(
                    HostSeen(str(s.get("host") or ""), "mirror", str(s.get("final_url", "")), fam2, sha2)
                )
        return [h for h in out if h.host]

    def _pins(self, source_id: str) -> list[OriginPin]:
        out: list[OriginPin] = []
        for r in self.store.origin_pins(source_id):
            first, last = parse_ts(r["first_seen"]), parse_ts(r["last_seen"])
            assert first is not None
            assert last is not None
            out.append(
                OriginPin(
                    str(r["source_id"]),
                    str(r["host"]),
                    str(r["role"]),
                    r["issuer_family"],
                    r["cert_sha256"],
                    first,
                    last,
                )
            )
        return out

    def _alerts(self, source_id: str, version: str, findings: Sequence[Finding]) -> None:
        if self.alerter is None:
            return
        for f in findings:
            if f.action is not Action.ALERT:
                continue
            target = str(f.data.get("host") or f.data.get("file") or f.data.get("url") or version)
            prefix = "origin-drift" if f.code.startswith("origin-") else "integrity"
            self.alerter.raise_(
                Alert(
                    key=f"{prefix}:{source_id}:{f.code}:{target}",
                    severity=Severity.WARNING,
                    message=f"{source_id} {version}: {f.message}",
                    data={"source_id": source_id, "version": version, **f.to_json()},
                )
            )

    def trust(self, source_id: str, version: str) -> list[str]:
        """Owner approved an origin hold: pin the blocked hosts so the release can continue."""
        stored = self.store.provenance(source_id, version)
        if stored is None:
            return []
        now = self.clock.now()
        blocked = cast(list[Mapping[str, Any]], stored[1].get("origins", {}).get("blocked", []))
        hosts: list[str] = []
        for h in blocked:
            host = str(h.get("host", ""))
            if not host:
                continue
            self.store.put_origin_pin(
                source_id,
                host,
                str(h.get("role", "origin")),
                h.get("issuer_family"),
                h.get("cert_sha256"),
                now,
                now,
            )
            hosts.append(host)
        if self.alerter is not None:
            for host in hosts:
                self.alerter.resolve(f"origin-drift:{source_id}:origin-unknown-domain:{host}")
        return hosts

    # --- 2. after sealing ----------------------------------------------------------------------------

    def attest_provenance(self, source_id: str, version: str, generation_id: str) -> str | None:
        if self.hooks.attest is None:
            return None
        stored = self.store.provenance(source_id, version)
        if stored is None:
            return None  # fetched before provenance capture existed: coverage shows the gap
        return self.hooks.attest(source_id, generation_id, PROVENANCE_NAME, {"release": stored[1]})

    # --- 3. before promotion ---------------------------------------------------------------------------

    def _profile_path(self, source_id: str, generation_id: str) -> Path:
        return self.profiles_dir / source_id / f"{generation_id}.json.gz"

    def profile(
        self, spec: SourceSpec, generation: Generation, generation_dir: Path
    ) -> ContentProfile | None:
        """The generation's content profile, computed once and kept on local disk."""
        if self.hooks.profile is None:
            return None
        path = self._profile_path(spec.id, generation.generation_id)
        if path.is_file():
            try:
                return ContentProfile.load(path)
            except (OSError, ValueError, KeyError):
                path.unlink(missing_ok=True)
        prof = self.hooks.profile(spec, generation, generation_dir)
        prof.save(path)
        return prof

    def baseline(
        self, source_id: str, generation_id: str, promoted: Sequence[str]
    ) -> tuple[str, ContentProfile] | None:
        """The newest promoted generation (other than this one) that has a stored profile."""
        for gid in reversed(promoted):
            if gid == generation_id:
                continue
            path = self._profile_path(source_id, gid)
            if path.is_file():
                try:
                    return gid, ContentProfile.load(path)
                except (OSError, ValueError, KeyError):
                    continue
        return None

    def promotion_report(
        self,
        spec: SourceSpec,
        generation: Generation,
        generation_dir: Path,
        *,
        sanity: Sequence[Check],
        promoted: Sequence[str],
    ) -> PromotionReport:
        checks = list(sanity)
        top: list[Mapping[str, object]] = []
        summary: Mapping[str, object] = {}
        try:
            prof = self.profile(spec, generation, generation_dir)
        except Exception as exc:  # cannot look at the content: the owner decides, data is never lost
            log.exception("content profile failed", extra={"fields": {"source": spec.id}})
            checks.append(Check("profile", False, None, None, f"content could not be profiled: {exc!r}"))
            prof = None
        base = self.baseline(spec.id, generation.generation_id, promoted) if prof is not None else None
        if prof is not None:
            summary = profile_summary(prof)
        if prof is not None and base is not None:
            more, top_changed = compare_profiles(base[1], prof, self.settings.anomaly)
            checks += more
            top = list(top_changed)
        elif prof is not None:
            checks.append(
                Check("baseline", True, None, None, "no earlier profile: this generation is the baseline")
            )
        return PromotionReport(
            spec.id,
            generation.generation_id,
            base[0] if base else None,
            tuple(checks),
            tuple(top),
            self.clock.now(),
            summary,
        )

    def write_report(self, report: PromotionReport, version: str) -> str | None:
        if self.hooks.attest is None:
            return None
        body: dict[str, Any] = {"version": version, **report.to_json()}
        return self.hooks.attest(report.source_id, report.generation_id, PROMOTION_NAME, body)

    # --- known-good anchoring (retention) ------------------------------------------------------------

    def settled(self, source_id: str, generation_id: str, promoted_at: datetime | None) -> bool:
        """Promoted at least ``settle_days`` ago and fully verified by the scrub since."""
        if promoted_at is None:
            return False
        if (self.clock.now() - promoted_at).days < self.settings.settle_days:
            return False
        return self.hooks.scrubbed(source_id, generation_id) if self.hooks.scrubbed is not None else True


def _host_json(h: HostSeen) -> dict[str, object]:
    return {
        "host": h.host,
        "role": h.role,
        "url": h.url,
        "issuer_family": h.issuer_family,
        "cert_sha256": h.cert_sha256,
    }


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def sanity_checks(reasons: Sequence[str], ok: bool) -> list[Check]:
    """The PLAN §6.4 count/size checks as report checks."""
    if ok:
        return [Check("sanity", True, None, None, "document count and size within limits")]
    return [Check("sanity", False, None, None, r) for r in reasons]
