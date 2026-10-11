"""The per-generation catalog ``meta/documents.jsonl`` (research doc §5.2, §5.4).

Each cycle's catalog lists the *whole* collection: documents fetched this cycle (bytes in this
generation) and every document carried over from earlier generations (``generation`` names where
the bytes are). Editions are labelled after the merge, so a new FM 5-0 makes the 2014 edition
``superseded`` without touching the sealed generation that holds it. A document the issuer no
longer lists is kept and labelled ``withdrawn-from-index`` (``rescinded`` only when the issuer says so).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from wsp_adapters.doccollection.model import Candidate, DocumentMeta
from wsp_adapters.doccollection.policy import Currency, CurrencyRules, Edition, assign_currency


@dataclass(frozen=True, slots=True)
class FetchedDoc:
    """A document fetched and screened this cycle."""

    cand: Candidate
    file: str  # relative to the generation, e.g. upstream/<name>
    sha256: str
    bytes: int
    decision: str
    distribution: str = ""
    distribution_text: str = ""
    fetched_at: str = ""
    http: Mapping[str, str] = field(default_factory=dict[str, str])
    license: str = ""
    mirror_sha256: str = ""


def meta_for(doc: FetchedDoc) -> DocumentMeta:
    c = doc.cand
    rescinded = c.status.upper() in ("RESCINDED", "OBSOLETE", "WITHDRAWN")
    return DocumentMeta(
        doc_key=c.key,
        file=doc.file,
        issuer=c.issuer,
        title=c.title,
        pub_number=c.pub_number,
        edition_date=c.edition_date,
        distribution=doc.distribution,
        distribution_text=doc.distribution_text,
        currency=Currency.RESCINDED.value if rescinded else Currency.CURRENT.value,
        filter_decision=doc.decision,
        origin_url=c.url,
        landing_url=c.landing_url,
        mirrors=c.mirrors,
        fetched_at=doc.fetched_at,
        http=doc.http,
        sha256=doc.sha256,
        bytes=doc.bytes,
        mirror_sha256=doc.mirror_sha256,
        language=c.language,
        subjects=c.subjects,
        license=doc.license,
    )


def build_catalog(
    previous: Iterable[DocumentMeta],
    previous_generation: str | None,
    fetched: Sequence[FetchedDoc],
    rules: CurrencyRules,
    *,
    listed: Collection[str] | None = None,
) -> list[DocumentMeta]:
    """Merge the previous catalog with this cycle's documents and label every edition."""
    by_key: dict[str, DocumentMeta] = {}
    for m in previous:
        by_key[m.doc_key] = replace(m, generation=m.generation or (previous_generation or ""))
    for doc in fetched:
        by_key[doc.cand.key] = meta_for(doc)
    labels = assign_currency(
        [Edition(m.doc_key, m.pub_number, m.edition_date) for m in by_key.values()], rules
    )
    out: list[DocumentMeta] = []
    for key in sorted(by_key):
        m = by_key[key]
        label = labels[key]
        currency = label.currency.value
        if m.currency == Currency.RESCINDED.value:
            currency = m.currency
        elif listed is not None and key not in listed and label.currency is Currency.CURRENT:
            currency = Currency.WITHDRAWN.value
        out.append(
            replace(
                m,
                currency=currency,
                historical=label.historical,
                banner=label.banner or "",
                supersedes=label.supersedes or "",
                superseded_by=label.superseded_by or "",
            )
        )
    return out


def write_catalog(path: Path, docs: Iterable[DocumentMeta]) -> str:
    """Write the catalog atomically; returns its SHA-256 (the ingest plan verifies it)."""
    import hashlib

    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(d.to_line() + "\n" for d in docs).encode("utf-8")
    tmp = path.with_name(path.name + ".partial")
    tmp.write_bytes(data)
    tmp.replace(path)
    return hashlib.sha256(data).hexdigest()


def read_catalog_file(path: Path) -> list[DocumentMeta]:
    if not path.is_file():
        return []
    return [
        DocumentMeta.from_line(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
