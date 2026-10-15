"""The per-generation catalog ``meta/documents.jsonl`` (research doc §5.2, §5.4).

Each cycle's catalog lists the *whole* collection: documents fetched this cycle (bytes in this
generation) and every document carried over from earlier generations (``generation`` names where
the bytes are). Editions are labelled after the merge, so a new FM 5-0 makes the 2014 edition
``superseded`` without touching the sealed generation that holds it. A document the issuer no
longer lists is kept, labelled ``withdrawn-from-index`` (the issuer's index) and shown with a banner;
``rescinded`` only when the issuer says so, and only for as long as it says so.

Policy applies to carried-over documents too (2026-10-12 audit, CA-8): an owner deny, a new deny-core
pattern or a newly recognised marking in the title, or a format the source no longer takes, sets
``excluded`` to the rule, and :meth:`DocCollectionAdapter.documents` leaves the document out, so
the next index run of the generation removes it. The bytes stay in the sealed generation that holds
them (write-once vault); the owner purges them with the vault's tools if they must go.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from wsp_adapters.doccollection.model import Candidate, DocumentMeta
from wsp_adapters.doccollection.policy import (
    Currency,
    CurrencyRules,
    DocFacts,
    Edition,
    Rules,
    Verdict,
    assign_currency,
    classify_metadata,
)

RESCINDED_STATUSES = ("RESCINDED", "OBSOLETE", "WITHDRAWN")


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
    md5: str = ""  # MD5 of the bytes: compared with the issuer's listing MD5 to skip unchanged files


def _rescinded(cand: Candidate) -> bool:
    return cand.status.upper() in RESCINDED_STATUSES


def meta_for(doc: FetchedDoc) -> DocumentMeta:
    c = doc.cand
    return DocumentMeta(
        doc_key=c.key,
        file=doc.file,
        issuer=c.issuer,
        title=c.title,
        pub_number=c.pub_number,
        edition_date=c.edition_date,
        distribution=doc.distribution,
        distribution_text=doc.distribution_text,
        currency=Currency.RESCINDED.value if _rescinded(c) else Currency.CURRENT.value,
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
        md5=doc.md5,
    )


def refreshed(meta: DocumentMeta, cand: Candidate) -> DocumentMeta:
    """A carried-over entry with what the issuer lists now (title, number, edition date, status),
    even when its bytes were not fetched again (CA-13): a status the issuer dropped is dropped here."""
    return replace(
        meta,
        title=cand.title or meta.title,
        pub_number=cand.pub_number or meta.pub_number,
        edition_date=cand.edition_date or meta.edition_date,
        currency=Currency.RESCINDED.value if _rescinded(cand) else Currency.CURRENT.value,
        origin_url=cand.url or meta.origin_url,
        landing_url=cand.landing_url or meta.landing_url,
        mirrors=cand.mirrors or meta.mirrors,
        subjects=cand.subjects or meta.subjects,
        language=cand.language or meta.language,
    )


def excluded_by(meta: DocumentMeta, policy: Rules | None, formats: Sequence[str]) -> str:
    """Why current policy keeps a stored document out of the index, or ``""``."""
    suffix = meta.file.rsplit(".", 1)[-1].lower() if "." in meta.file else ""
    if formats and suffix not in formats and not meta.file.endswith(".bundle"):
        return f"format={suffix or 'none'}"
    if policy is None:
        return ""
    facts = DocFacts(key=meta.doc_key, pub_number=meta.pub_number, title=meta.title, subjects=meta.subjects)
    decision = classify_metadata(facts, policy)
    return str(decision) if decision.verdict is Verdict.DENY else ""


def _protected(key: str, protected: Collection[str]) -> bool:
    """``key`` is, or is a numbered document of (``<key>-<i>``), an item whose documents are unknown."""
    return key in protected or (key.rsplit("-", 1)[0] in protected if "-" in key else False)


def build_catalog(
    previous: Iterable[DocumentMeta],
    previous_generation: str | None,
    fetched: Sequence[FetchedDoc],
    rules: CurrencyRules,
    *,
    listed: Collection[str] | None = None,
    protected: Collection[str] = (),
    listing: Mapping[str, Candidate] | None = None,
    policy: Rules | None = None,
    formats: Sequence[str] = (),
) -> list[DocumentMeta]:
    """Merge the previous catalog with this cycle's documents and label every edition.

    ``listed``: every document key the issuer lists now, or ``None`` when that is unknown (a partial
    or sampled listing): only then may a document be labelled withdrawn, and never one of the
    ``protected`` items (resolve failed, held for review). ``listing``: this cycle's candidates by
    key, to refresh the metadata of documents carried over unfetched. ``policy`` and ``formats``:
    the source's current rules, applied to carried-over documents (``excluded``).
    """
    listing = listing or {}
    by_key: dict[str, DocumentMeta] = {}
    for m in previous:
        meta = replace(m, generation=m.generation or (previous_generation or ""))
        cand = listing.get(m.doc_key)
        by_key[m.doc_key] = refreshed(meta, cand) if cand is not None else meta
    fresh = set[str]()
    for doc in fetched:
        by_key[doc.cand.key] = meta_for(doc)
        fresh.add(doc.cand.key)
    labels = assign_currency(
        [Edition(m.doc_key, m.pub_number, m.edition_date, m.fetched_at, m.title) for m in by_key.values()],
        rules,
    )
    out: list[DocumentMeta] = []
    for key in sorted(by_key):
        m = by_key[key]
        label = labels[key]
        currency = label.currency.value
        unlisted = listed is not None and key not in listed and not _protected(key, protected)
        if m.currency == Currency.RESCINDED.value:
            currency = m.currency
        elif unlisted and label.currency in (Currency.CURRENT, Currency.HISTORICAL):
            currency = Currency.WITHDRAWN.value
        out.append(
            replace(
                m,
                currency=currency,
                historical=label.historical,
                banner=label.banner or "",
                supersedes=label.supersedes or "",
                superseded_by=label.superseded_by or "",
                excluded=excluded_by(m, policy, formats) if key not in fresh else "",
            )
        )
    return out


def catalog_bytes(docs: Iterable[DocumentMeta]) -> bytes:
    return "".join(d.to_line() + "\n" for d in docs).encode("utf-8")


def write_catalog(path: Path, docs: Iterable[DocumentMeta]) -> str:
    """Write the catalog atomically; returns its SHA-256 (the ingest plan verifies it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = catalog_bytes(docs)
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
