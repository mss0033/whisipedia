"""Clearing DEGRADED, only after a full re-verification passes (vault-fetch audit #1).

``DEGRADED.txt`` used to be permanent: a generation degraded by a transient fault or a wrong
signing key stayed unusable for promotion forever. :func:`reverify_and_clear` is the only way back,
and it proves the generation is intact first:

* under the generation's exclusive lock (nothing can repair, degrade or prune it meanwhile);
* a fresh signature check of the manifest, the parity sidecar and the attestations, a walk for
  unexpected files, and every byte of every listed file re-hashed now (no scrub-cycle shortcut);
* only if that finds nothing at all (an unreadable file counts as a failure here), ``DEGRADED.txt``
  is moved into ``quarantine/`` (kept as history) and ``vault.undegraded`` is written to the
  ledger with what was verified; otherwise ``vault.undegrade_refused`` lists the problems.

The function does not re-promote anything: which generation is current stays an owner decision
(``promote`` refuses a degraded generation, and accepts it again once cleared). It is independent
of the CLI so the orchestrator, ``wsp`` and tests use the same path.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from wsp_vault.manifest import DEGRADED_NAME
from wsp_vault.scrub import Scrubber, ScrubFinding
from wsp_vault.vault import FileVault


@dataclass(frozen=True, slots=True)
class UndegradeResult:
    source_id: str
    generation_id: str
    cleared: bool
    detail: str
    files: int = 0
    bytes: int = 0
    findings: tuple[ScrubFinding, ...] = ()


def reverify_and_clear(
    vault: FileVault, scrubber: Scrubber, source_id: str, generation_id: str
) -> UndegradeResult:
    """Fully re-verify a DEGRADED generation and clear the flag if, and only if, it is intact."""
    subject = f"{source_id}/{generation_id}"
    with vault.exclusive_generation(source_id, generation_id) as lib:
        marker = lib / DEGRADED_NAME
        if not marker.is_file():
            return UndegradeResult(source_id, generation_id, False, "not degraded")
        record = marker.read_bytes()
        check = scrubber.verify_generation(source_id, generation_id)
        if not check.ok:
            ledger = vault.ledger
            if ledger is not None:
                ledger.append(
                    "vault.undegrade_refused",
                    subject,
                    {
                        "findings": [
                            {"path": f.relpath, "status": str(f.status), "detail": f.detail}
                            for f in check.findings[:50]
                        ],
                        "files": check.files,
                    },
                )
            return UndegradeResult(
                source_id,
                generation_id,
                False,
                f"{len(check.findings)} problem(s) remain",
                check.files,
                check.bytes,
                check.findings,
            )
        vault._clear_degraded(  # pyright: ignore[reportPrivateUsage] - the one sanctioned caller
            source_id,
            generation_id,
            {
                "files_verified": check.files,
                "bytes_verified": check.bytes,
                "degraded_sha256": hashlib.sha256(record).hexdigest(),
            },
        )
    return UndegradeResult(
        source_id, generation_id, True, "verified in full; DEGRADED cleared", check.files, check.bytes
    )
