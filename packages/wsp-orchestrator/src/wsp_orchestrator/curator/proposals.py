"""The proposal sink: one YAML file per proposal on local disk, readable without our software.

There is no GitHub CLI on the host, so a "pull request" is a file::

    <proposals dir>/00012-kiwix-wikivoyage-en-all.yaml   (default: next to the index root)

It holds the gate's decision, the score breakdown, every check, the drafted registry entry and a
Markdown body. ``status`` is ``open`` (waiting for the owner), ``applied`` (auto-promoted into the
local overlay), ``accepted`` (written to ``registry/sources.yaml`` by ``wsp proposals accept``) or
``rejected``. The API lists them at ``/v1/proposals``; the CLI decides them.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import yaml

from wsp_orchestrator.policy import Proposal

STATUSES = ("open", "applied", "accepted", "rejected")
_SLUG_BAD = re.compile(r"[^a-z0-9]+")
_FILE = re.compile(r"^(\d{5,})-[a-z0-9-]*\.yaml$")


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    """A stored proposal: what the orchestrator hands to the proposal files after the gate."""

    id: int
    status: str
    created_at: datetime
    proposal: Proposal


def _slug(text: str) -> str:
    return _SLUG_BAD.sub("-", text.casefold()).strip("-")[:60] or "proposal"


class ProposalFiles:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, record: ProposalRecord) -> Path:
        return self.root / f"{record.id:05d}-{_slug(record.proposal.candidate.key)}.yaml"

    def write(self, record: ProposalRecord) -> Path:
        doc: dict[str, Any] = {
            "id": record.id,
            "status": record.status,
            "created_at": record.created_at.isoformat(),
            "decided_at": record.created_at.isoformat() if record.status != "open" else None,
            "note": "auto-promoted into config/registry.local.yaml" if record.status == "applied" else None,
            **record.proposal.to_json(),
        }
        path = self.path_for(record)
        self._save(path, doc)
        return path

    def _save(self, path: Path, doc: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".partial")
        tmp.write_text(
            yaml.safe_dump(dict(doc), sort_keys=False, allow_unicode=True, width=110), encoding="utf-8"
        )
        os.replace(tmp, path)

    def files(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return sorted(p for p in self.root.iterdir() if _FILE.match(p.name))

    def load(self, path: Path) -> dict[str, Any]:
        return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        docs = [self.load(p) for p in self.files()]
        return [d for d in docs if status is None or d.get("status") == status]

    def find(self, proposal_id: int) -> Path | None:
        for path in self.files():
            m = _FILE.match(path.name)
            if m and int(m[1]) == proposal_id:
                return path
        return None

    def decide(
        self, proposal_id: int, status: str, *, at: datetime, note: str | None = None
    ) -> dict[str, Any]:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        path = self.find(proposal_id)
        if path is None:
            raise LookupError(f"no proposal {proposal_id} under {self.root}")
        doc = self.load(path)
        doc["status"] = status
        doc["decided_at"] = at.isoformat()
        if note:
            doc["note"] = note
        self._save(path, doc)
        return doc
