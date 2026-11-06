"""Registry health probes and the weekly adapter contract smoke (PLAN §5.5).

* :class:`RegistryHealthBot` checks that every listed ``access.endpoints`` URL still answers (a HEAD,
  or a small GET). An active source unreachable on two runs in a row gets an alert and a
  "registry fix" proposal for the owner. An adapter whose endpoints are bases it builds URLs from
  (``SupportsProbeUrls``: Open Library's ``/data/``, CourtListener's S3 prefix) names the URLs to
  probe instead; a HEAD of a base is a 404, and both sources were reported unreachable every week
  while they discovered fine (2026-11-05 deep dive).
* :class:`AdapterSmokeBot` runs each adapter in use against live upstream with a small sample:
  ``discover`` must offer a sortable, checksummed release and ``plan`` must accept it. It catches
  format drift a week before a cycle depends on it; nothing is downloaded.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

from wsp_core.errors import FetchError, NotFoundError, RobotsDisallowedError
from wsp_core.model import SourceSpec, SourceStatus
from wsp_core.ports import SupportsHead, SupportsProbeUrls
from wsp_orchestrator.alerts import Alert, Severity
from wsp_orchestrator.bots import BotContext, BotResult
from wsp_orchestrator.policy import Candidate, CandidateKind


def endpoints(spec: SourceSpec) -> list[str]:
    access = spec.raw.get("access")
    if not isinstance(access, Mapping):
        return []
    raw = cast(Mapping[str, Any], access).get("endpoints")
    return [str(u) for u in cast(list[object], raw)] if isinstance(raw, list) else []


# A HEAD the host answered with a refusal of the method or of a bare base (403, 404, 405, 501): a
# small GET may still answer, as S3 and some file hosts do.
_HEAD_REFUSED = re.compile(r"HTTP (4\d\d|501)\b")


def probe_urls(ctx: BotContext, spec: SourceSpec) -> list[str]:
    """What the health bot probes for ``spec``: its adapter's ``probe_urls`` (``SupportsProbeUrls``)
    when it has them, else ``access.endpoints``."""
    if spec.adapter:
        try:
            adapter = ctx.adapter_for(spec)
        except Exception:  # no adapter (a candidate): its endpoints are all there is
            return endpoints(spec)
        if isinstance(adapter, SupportsProbeUrls):
            return [str(u) for u in adapter.probe_urls(spec)]
    return endpoints(spec)


class RegistryHealthBot:
    name = "registry-health"
    cadence: str | None = "P1W"
    network = True

    def __init__(self, *, max_endpoints: int = 3) -> None:
        self.max_endpoints = max_endpoints

    def _probe(self, ctx: BotContext, url: str) -> str | None:
        try:
            if isinstance(ctx.fetcher, SupportsHead):
                try:
                    ctx.fetcher.head(url)
                    return None
                except RobotsDisallowedError:
                    raise
                except FetchError as exc:
                    if not isinstance(exc, NotFoundError) and not _HEAD_REFUSED.search(str(exc)):
                        raise
            ctx.fetcher.get_bytes(url, max_bytes=256 * 1024)
        except FetchError as exc:
            if "max" in str(exc) and "bytes" in str(exc):  # answered, just large
                return None
            return f"{type(exc).__name__}: {exc}"
        except OSError as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    def run(self, ctx: BotContext) -> BotResult:
        prev_streaks = cast(Mapping[str, Any], ctx.previous.get("failing", {}))
        failing: dict[str, dict[str, object]] = {}
        ok: list[str] = []
        alerts: list[Alert] = []
        candidates: list[Candidate] = []
        for spec in ctx.sources:
            if spec.status not in (SourceStatus.ACTIVE, SourceStatus.PAUSED, SourceStatus.CANDIDATE):
                continue
            urls = probe_urls(ctx, spec)[: self.max_endpoints]
            if not urls:
                continue
            errors = {u: e for u in urls if (e := self._probe(ctx, u)) is not None}
            if len(errors) < len(urls):
                ok.append(spec.id)
                continue
            before = prev_streaks.get(spec.id)
            streak = (
                int(cast(Mapping[str, Any], before).get("streak", 0)) + 1
                if isinstance(before, Mapping)
                else 1
            )
            since = str(cast(Mapping[str, Any], before).get("since")) if isinstance(before, Mapping) else None
            since = since or ctx.now.date().isoformat()
            failing[spec.id] = {"streak": streak, "since": since, "errors": errors}
            if streak >= 2 and spec.status is SourceStatus.ACTIVE:
                alerts.append(
                    Alert(
                        key=f"registry-health:{spec.id}",
                        severity=Severity.WARNING,
                        message=f"{spec.id}: no listed endpoint answers ({streak} probes since {since})",
                        data={"source_id": spec.id, "errors": errors},
                    )
                )
                candidates.append(
                    Candidate(
                        key=f"health:{spec.id}:{since}",
                        name=f"Check the endpoints of {spec.name}",
                        kind=CandidateKind.REGISTRY_FIX,
                        license=spec.license,
                        domain=spec.domain,
                        found_by=self.name,
                        existing_source_id=spec.id,
                        url=urls[0],
                        registry_entry={},
                    )
                )
        return BotResult(
            candidates=candidates,
            alerts=alerts,
            resolved=[f"registry-health:{sid}" for sid in ok],
            notes={"checked": len(ok) + len(failing), "failing": failing},
        )


class AdapterSmokeBot:
    name = "adapter-contract"
    cadence: str | None = "P1W"
    network = True

    def run(self, ctx: BotContext) -> BotResult:
        sample: dict[str, SourceSpec] = {}
        for spec in ctx.sources:
            if spec.status is SourceStatus.ACTIVE and spec.adapter and spec.adapter not in sample:
                sample[spec.adapter] = spec
        results: dict[str, str] = {}
        alerts: list[Alert] = []
        resolved: list[str] = []
        for adapter_id, spec in sorted(sample.items()):
            key = f"adapter-contract:{adapter_id}"
            problem = self._check(ctx, spec)
            results[adapter_id] = problem or "ok"
            if problem is None:
                resolved.append(key)
            else:
                alerts.append(
                    Alert(
                        key=key,
                        severity=Severity.WARNING,
                        message=f"adapter {adapter_id} failed its live smoke test on {spec.id}: {problem}",
                        data={"adapter": adapter_id, "source_id": spec.id, "problem": problem},
                    )
                )
        return BotResult(alerts=alerts, resolved=resolved, notes={"adapters": results})

    @staticmethod
    def _check(ctx: BotContext, spec: SourceSpec) -> str | None:
        try:
            adapter = ctx.adapter_for(spec)
        except Exception as exc:
            return f"no adapter: {exc}"
        try:
            sctx = ctx.ctx_for(spec)
            refs = list(adapter.discover(sctx))
            if not refs:
                return "discover() offered no release"
            newest = max(refs, key=lambda r: r.version)
            problems = _release_problems(newest.version, [(f.url, f.name) for f in newest.files])
            if problems:
                return "; ".join(problems)
            plan = adapter.plan(sctx, None, newest)
            if not plan.files:
                return "plan() selected no files for a fresh copy"
        except Exception as exc:  # anything an adapter raises is a contract failure here
            return f"{type(exc).__name__}: {exc}"
        return None


def _release_problems(version: str, files: Sequence[tuple[str, str]]) -> list[str]:
    """Shape checks; missing checksums are the health probe's business (an alert, not a failure)."""
    out: list[str] = []
    if not version.strip():
        out.append("empty version")
    if not files:
        out.append("release has no files")
    for url, name in files:
        # ftp:// is an issuer's FTP service, allowed where the owner chose it over a robots-disallowed
        # HTTPS host (D16, 2026-10-24: NCBI); plain http:// never.
        if not url.startswith(("https://", "ftp://")):
            out.append(f"{name}: not https ({url[:40]})")
        if not name or "/" in name:
            out.append(f"bad file name {name!r}")
    return out[:5]
