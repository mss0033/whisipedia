"""Chaos drills (PLAN §8.5): break a sandboxed copy of a sealed generation, expect detect + repair.

The generation is copied into ``<workdir>/vault`` (the real vault is only read). A fault is
injected there, a fresh :class:`Scrubber` must find it, and a :class:`RepairLadder` that uses the
real vault as its second copy must fix it, after which a full re-scrub must be clean. The result
is optionally appended to the real ledger as ``drill.result`` for the health page.
"""

from __future__ import annotations

import os
import random
import shutil
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from wsp_core.ports import Ledger, ParityProvider
from wsp_vault.ledger import JsonlLedger
from wsp_vault.manifest import MANIFEST_NAME, SIGNATURE_NAME
from wsp_vault.repair import Refetcher, RepairLadder
from wsp_vault.scrub import Scrubber
from wsp_vault.vault import CURRENT_NAME, FileVault


class DrillKind(StrEnum):
    BIT_FLIP = "bit-flip"
    TRUNCATE = "truncate"
    DELETE = "delete"
    BAD_SIGNATURE = "bad-signature"
    INTERRUPTED_PROMOTION = "interrupted-promotion"


@dataclass(frozen=True, slots=True)
class DrillResult:
    kind: DrillKind
    source_id: str
    generation_id: str
    target: str
    detected: bool
    repaired: bool
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.detected and self.repaired


def run_drill(
    vault: FileVault,
    source_id: str,
    generation_id: str,
    workdir: Path,
    kind: DrillKind,
    *,
    rng: random.Random | None = None,
    parity: ParityProvider | None = None,
    refetch: Refetcher | None = None,
    ledger: Ledger | None = None,
) -> DrillResult:
    rng = rng or random.Random()  # noqa: S311 - fault placement, not cryptography
    if workdir.resolve().is_relative_to(vault.root.resolve()):
        raise ValueError("drill workdir must be outside the vault")
    if workdir.exists() and any(workdir.iterdir()):
        raise ValueError(f"drill workdir {workdir} is not empty")
    sandbox_root = workdir / "vault"
    sandbox_ledger = JsonlLedger(workdir / "ledger", vault.signer)
    sandbox = FileVault(
        sandbox_root, vault.signer, domains={source_id: vault.domain_of(source_id)}, ledger=sandbox_ledger
    )
    manifest = vault.manifest(source_id, generation_id)
    src_lib = vault.library_path(source_id, generation_id)
    lib = sandbox.library_path(source_id, generation_id)
    shutil.copytree(src_lib, lib)
    (sandbox.source_dir(source_id) / CURRENT_NAME).write_text(f"{generation_id}\n", encoding="utf-8")

    if kind is DrillKind.INTERRUPTED_PROMOTION:
        return _record(ledger, _interrupted_promotion(sandbox, source_id, generation_id))

    data = [e for e in manifest.data if e.size > 0] or list(manifest.data)
    target = rng.choice(data).path if kind is not DrillKind.BAD_SIGNATURE else SIGNATURE_NAME
    path = lib / target
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    if kind is DrillKind.BIT_FLIP:
        raw = bytearray(path.read_bytes())
        if raw:
            raw[rng.randrange(len(raw))] ^= 1 << rng.randrange(8)
        else:
            raw = bytearray(b"\0")
        path.write_bytes(bytes(raw))
    elif kind is DrillKind.TRUNCATE:
        os.truncate(path, path.stat().st_size // 2)
    elif kind is DrillKind.DELETE:
        path.unlink()
    else:
        path.write_bytes(vault.signer.sign(b"not the manifest"))

    scrubber = Scrubber(sandbox, workdir / "scrub-state.sqlite", ledger=sandbox_ledger)
    report = scrubber.run()
    expect = None if target in (MANIFEST_NAME, SIGNATURE_NAME) else target
    detected = any(f.relpath == expect for f in report.findings)

    ladder = RepairLadder(
        sandbox,
        sandbox_ledger,
        parity=parity,
        mirror_root=vault.root,
        refetch=refetch,
        scratch=workdir / "scratch",
    )
    results = ladder.repair_findings(report.findings)
    remaining = scrubber.scrub_generation(source_id, generation_id)
    repaired = bool(results) and all(r.ok for r in results) and not remaining
    detail = "; ".join(f"{f.relpath}: {f.detail}" for f in remaining) or ", ".join(
        f"{p} via {r}" for res in results for p, r in res.repaired.items()
    )
    return _record(
        ledger, DrillResult(kind, source_id, generation_id, target, detected, repaired, detail or "restored")
    )


def _interrupted_promotion(sandbox: FileVault, source_id: str, generation_id: str) -> DrillResult:
    pointer = sandbox.source_dir(source_id) / CURRENT_NAME
    pointer.with_name(CURRENT_NAME + ".partial").write_bytes(b"half-writ")
    current = sandbox.current(source_id)
    detected = current is not None and current.generation_id == generation_id
    sandbox.promote(source_id, generation_id)
    after = sandbox.current(source_id)
    repaired = after is not None and after.generation_id == generation_id
    return DrillResult(
        DrillKind.INTERRUPTED_PROMOTION,
        source_id,
        generation_id,
        CURRENT_NAME,
        detected,
        repaired,
        "pointer unaffected by a torn write; re-promotion succeeded",
    )


def _record(ledger: Ledger | None, result: DrillResult) -> DrillResult:
    if ledger is not None:
        ledger.append(
            "drill.result",
            f"{result.source_id}/{result.generation_id}",
            {
                "kind": str(result.kind),
                "target": result.target,
                "detected": result.detected,
                "repaired": result.repaired,
                "detail": result.detail,
            },
        )
    return result
