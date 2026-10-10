"""In-memory fakes for the read-side ports.

They are small, deterministic and dependency-free so that any package's tests (and the CLI's
placeholder composition root) can build a working :class:`wsp_api.services.Services`. They implement
the port semantics faithfully enough to be swapped for the real implementations without changing
callers, but they are not optimized and keep everything in RAM.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import blake3

from wsp_api.services import Services
from wsp_core.errors import InvalidCursorError
from wsp_core.ids import DocId, content_hash, make_doc_id
from wsp_core.model import (
    Artifact,
    ChangeKind,
    ChangeRecord,
    Checksum,
    DocRecord,
    FetchResult,
    Generation,
    GenerationStatus,
    LedgerEvent,
    SearchHit,
    SourceSpec,
    SourceStatus,
    Tier,
    UpdateStrategy,
)

_WORD = re.compile(r"\w+", re.UNICODE)
_GENESIS = "0" * 64


def _tokens(text: str) -> list[str]:
    return [t.casefold() for t in _WORD.findall(text)]


__all__ = [
    "EchoGenerator",
    "InMemoryChangeFeed",
    "InMemoryDocumentStore",
    "InMemoryLedger",
    "InMemoryLexicalIndex",
    "InMemoryVault",
    "InvalidCursorError",
    "sample_services",
    "seed_sample_library",
]


# --- LexicalIndex --------------------------------------------------------------------------------


class InMemoryLexicalIndex:
    """Term-count scoring with a title boost. Good enough to exercise paging and filtering."""

    def __init__(self) -> None:
        self._docs: dict[DocId, DocRecord] = {}
        self._pending: dict[DocId, DocRecord | None] = {}

    def upsert(self, docs: Iterable[DocRecord]) -> int:
        n = 0
        for doc in docs:
            self._pending[doc.doc_id] = doc
            n += 1
        return n

    def delete(self, doc_ids: Iterable[DocId]) -> int:
        n = 0
        for doc_id in doc_ids:
            self._pending[doc_id] = None
            n += 1
        return n

    def commit(self) -> None:
        for doc_id, doc in self._pending.items():
            if doc is None:
                self._docs.pop(doc_id, None)
            else:
                self._docs[doc_id] = doc
        self._pending.clear()

    def search(
        self, query: str, *, limit: int = 10, offset: int = 0, sources: Sequence[str] | None = None
    ) -> Sequence[SearchHit]:
        terms = set(_tokens(query))
        if not terms:
            return ()
        if limit <= 0:
            return ()
        wanted = None if sources is None else set(sources)
        scored: list[SearchHit] = []
        for doc in self._docs.values():
            if wanted is not None and doc.source_id not in wanted:
                continue
            title, body = _tokens(doc.title), _tokens(doc.text)
            score = sum(3.0 * title.count(t) + body.count(t) for t in terms)
            if score > 0:
                scored.append(
                    SearchHit(
                        doc_id=doc.doc_id,
                        score=score,
                        title=doc.title,
                        snippet=_snippet(doc.text, terms),
                        source_id=doc.source_id,
                    )
                )
        scored.sort(key=lambda h: (-h.score, h.doc_id))
        return tuple(scored[offset : offset + limit])

    def suggest(self, prefix: str, *, limit: int = 10) -> Sequence[str]:
        p = prefix.casefold().strip()
        if not p:
            return ()
        names = {n for d in self._docs.values() for n in (d.title, *d.aliases) if n.casefold().startswith(p)}
        return tuple(sorted(names)[:limit])

    def close(self) -> None:
        self._pending.clear()


def _snippet(text: str, terms: set[str], width: int = 160) -> str:
    lowered = text.casefold()
    positions = [i for t in terms if (i := lowered.find(t)) >= 0]
    start = max(0, min(positions) - width // 4) if positions else 0
    out = text[start : start + width].strip()
    return ("…" if start > 0 else "") + out + ("…" if start + width < len(text) else "")


# --- DocumentStore -------------------------------------------------------------------------------


class InMemoryDocumentStore:
    """Versions per document, keyed by generation id (which sorts chronologically).

    ``put`` records content as of a generation; ``remove`` records a tombstone. A generation that is
    not mentioned for a document inherits its previous state (diff semantics).
    """

    def __init__(self) -> None:
        self._versions: dict[DocId, list[tuple[str, DocRecord | None]]] = {}

    def _set(self, generation_id: str, doc_id: DocId, doc: DocRecord | None) -> None:
        versions = self._versions.setdefault(doc_id, [])
        versions[:] = [v for v in versions if v[0] != generation_id]
        versions.append((generation_id, doc))
        versions.sort(key=lambda v: v[0])

    def put(self, generation_id: str, doc: DocRecord) -> None:
        if not doc.content_hash:
            doc = replace(doc, content_hash=content_hash(doc.text))
        self._set(generation_id, doc.doc_id, doc)

    def remove(self, generation_id: str, doc_id: DocId) -> None:
        """Tombstone ``doc_id`` as of ``generation_id`` (removed upstream; history is kept)."""
        self._set(generation_id, doc_id, None)

    def get(self, doc_id: DocId, *, at_generation: str | None = None) -> DocRecord | None:
        versions = self._versions.get(doc_id, [])
        if at_generation is not None:
            versions = [v for v in versions if v[0] <= at_generation]
        return versions[-1][1] if versions else None

    def history(self, doc_id: DocId) -> Sequence[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        previous: str | None = None
        for gen, doc in self._versions.get(doc_id, []):
            current = None if doc is None else doc.content_hash
            if current is not None and current != previous:
                out.append((gen, current))
            previous = current
        return tuple(out)

    def all_current(self) -> Iterator[DocRecord]:
        for versions in self._versions.values():
            doc = versions[-1][1]
            if doc is not None:
                yield doc


# --- ChangeFeed ----------------------------------------------------------------------------------


class InMemoryChangeFeed:
    """Cursor is the decimal count of records consumed; ``None`` means "from the beginning"."""

    def __init__(self) -> None:
        self._log: list[ChangeRecord] = []

    def publish(self, changes: Iterable[ChangeRecord]) -> str:
        self._log.extend(changes)
        return str(len(self._log))

    def since(self, cursor: str | None, *, limit: int = 1000) -> tuple[Sequence[ChangeRecord], str]:
        start = 0 if cursor is None else _parse_cursor(cursor, len(self._log))
        batch = tuple(self._log[start : start + limit])
        return batch, str(start + len(batch))

    def close(self) -> None:
        return None


def _parse_cursor(cursor: str, size: int) -> int:
    if not cursor.isdigit() or int(cursor) > size or (cursor != "0" and cursor.startswith("0")):
        raise InvalidCursorError(f"unknown cursor: {cursor!r}")
    return int(cursor)


# --- Vault ---------------------------------------------------------------------------------------


class InMemoryVault:
    """Tracks generations in RAM. ``ingest`` hashes the local file but does not copy it anywhere."""

    def __init__(self, root: Path = Path("memory-vault")) -> None:
        self._root = root
        self._gens: dict[str, dict[str, Generation]] = {}
        self._current: dict[str, str] = {}

    def add(self, generation: Generation, *, promote: bool = False) -> None:
        self._gens.setdefault(generation.source_id, {})[generation.generation_id] = generation
        if promote:
            self.promote(generation.source_id, generation.generation_id)

    def stage(self, source_id: str, generation_id: str) -> Path:
        gens = self._gens.setdefault(source_id, {})
        gens.setdefault(generation_id, Generation(source_id, generation_id, GenerationStatus.STAGING))
        return self.generation_path(source_id, generation_id)

    def ingest(
        self,
        source_id: str,
        generation_id: str,
        local: Path,
        relpath: str,
        *,
        upstream: Sequence[Checksum] = (),
        expected: FetchResult | None = None,
    ) -> Artifact:
        gen = self._gens[source_id][generation_id]
        if gen.status is not GenerationStatus.STAGING:
            raise PermissionError(f"{source_id}/{generation_id} is {gen.status}, not staging")
        data = local.read_bytes()
        art = Artifact(
            source_id=source_id,
            version=generation_id,
            relpath=PurePosixPath(relpath),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            blake3=blake3.blake3(data).hexdigest(),
            upstream=tuple(upstream),
        )
        if expected is not None and (expected.size, expected.sha256) != (art.size, art.sha256):
            raise ValueError(f"{relpath}: bytes differ from the fetch result")
        for c in upstream:
            known = c.algorithm in hashlib.algorithms_available
            if known and hashlib.new(c.algorithm, data).hexdigest() != c.hexdigest.lower():
                raise ValueError(f"{relpath}: upstream {c.algorithm} mismatch")
        others = tuple(a for a in gen.artifacts if a.relpath != art.relpath)
        self._gens[source_id][generation_id] = replace(gen, artifacts=(*others, art))
        return art

    def adopt(
        self,
        source_id: str,
        generation_id: str,
        path: Path,
        relpath: str,
        *,
        upstream: Sequence[Checksum] = (),
    ) -> Artifact:
        """Like ``ingest`` (nothing is stored on disk, so the file is left where it is)."""
        return self.ingest(source_id, generation_id, path, relpath, upstream=upstream)

    def seal(self, source_id: str, generation_id: str) -> Generation:
        gen = replace(
            self._gens[source_id][generation_id], status=GenerationStatus.SEALED, sealed_at=datetime.now(UTC)
        )
        self._gens[source_id][generation_id] = gen
        return gen

    def promote(self, source_id: str, generation_id: str) -> None:
        gen = self._gens[source_id][generation_id]
        if gen.status is GenerationStatus.STAGING:
            raise PermissionError(f"cannot promote unsealed generation {source_id}/{generation_id}")
        self._current[source_id] = generation_id

    def current(self, source_id: str) -> Generation | None:
        gid = self._current.get(source_id)
        return None if gid is None else self._gens[source_id][gid]

    def generations(self, source_id: str) -> Sequence[Generation]:
        return tuple(sorted(self._gens.get(source_id, {}).values(), key=lambda g: g.generation_id))

    def generation_path(self, source_id: str, generation_id: str) -> Path:
        return self._root / source_id / generation_id


# --- Ledger --------------------------------------------------------------------------------------


class InMemoryLedger:
    """SHA-256 hash chain over canonical JSON, same shape as the real ledger (PLAN §8.3)."""

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._events: list[LedgerEvent] = []
        self._clock = clock

    def append(
        self, kind: str, subject: str, data: Mapping[str, object], *, idempotency_key: str | None = None
    ) -> LedgerEvent:
        if idempotency_key is not None:
            for e in self._events:
                if e.data.get("idempotency_key") == idempotency_key:
                    return e
            data = {**data, "idempotency_key": idempotency_key}
        prev = self._events[-1].hash if self._events else _GENESIS
        seq = len(self._events) + 1
        at = self._clock()
        ev = LedgerEvent(
            seq, at, kind, subject, dict(data), prev, _event_hash(seq, at, kind, subject, data, prev)
        )
        self._events.append(ev)
        return ev

    def events(self, since_seq: int = 0) -> Iterator[LedgerEvent]:
        return iter([e for e in self._events if e.seq > since_seq])

    def tail(self, n: int) -> Sequence[LedgerEvent]:
        return list(self._events[-n:]) if n > 0 else []

    def verify(self) -> bool:
        prev = _GENESIS
        for e in self._events:
            if e.prev_hash != prev or e.hash != _event_hash(e.seq, e.at, e.kind, e.subject, e.data, prev):
                return False
            prev = e.hash
        return True

    def tamper(self, seq: int, **data: object) -> None:
        """Test helper: rewrite an event's payload without fixing the chain."""
        i = seq - 1
        self._events[i] = replace(self._events[i], data=data)


def _event_hash(
    seq: int, at: datetime, kind: str, subject: str, data: Mapping[str, object], prev: str
) -> str:
    body = json.dumps(
        {"seq": seq, "at": at.isoformat(), "kind": kind, "subject": subject, "data": data, "prev": prev},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# --- Generator -----------------------------------------------------------------------------------


class EchoGenerator:
    """Deterministic stand-in for the local LLM: cites every numbered passage in the prompt."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def stream(self, prompt: str, *, max_tokens: int = 512) -> AsyncIterator[str]:
        self.prompts.append(prompt)
        markers = re.findall(r"^\[(\d+)\]", prompt, flags=re.MULTILINE)
        words = ["Summary", "of", f"{len(markers)}", "results"]
        words += [f"[{m}]" for m in markers]
        for word in words[:max_tokens]:
            yield word + " "


# --- Sample library ------------------------------------------------------------------------------

SAMPLE_SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        id="wikipedia-en-zim",
        name="Wikipedia English (sample)",
        tier=Tier.T1,
        status=SourceStatus.ACTIVE,
        license="CC-BY-SA-4.0",
        legal_basis="open-license",
        domain=("encyclopedia",),
        adapter="kiwix-opds",
        cadence="P1M",
        strategy=UpdateStrategy.SNAPSHOT_RETENTION,
    ),
    SourceSpec(
        id="rfc",
        name="IETF RFCs (sample)",
        tier=Tier.T1,
        status=SourceStatus.ACTIVE,
        license="IETF-Trust",
        legal_basis="open-license",
        domain=("standards",),
        adapter="rsync-mirror",
        cadence="P1M",
        strategy=UpdateStrategy.MIRROR,
    ),
    SourceSpec(
        id="gutenberg",
        name="Project Gutenberg (sample)",
        tier=Tier.T1,
        status=SourceStatus.CANDIDATE,
        license="Public-Domain-US",
        legal_basis="public-domain",
        domain=("books",),
        cadence="P1M",
    ),
)


def _doc(source_id: str, native_id: str, title: str, text: str, license_: str) -> DocRecord:
    return DocRecord(
        doc_id=make_doc_id(source_id, native_id),
        source_id=source_id,
        native_id=native_id,
        title=title,
        lang="en",
        license=license_,
        text=text,
        content_hash=content_hash(text),
    )


SAMPLE_DOCS_V1: tuple[DocRecord, ...] = (
    _doc(
        "wikipedia-en-zim",
        "A/Alan_Turing",
        "Alan Turing",
        "Alan Turing was an English mathematician and computer scientist. "
        "He formalised the concepts of algorithm and computation with the Turing machine.",
        "CC-BY-SA-4.0",
    ),
    _doc(
        "wikipedia-en-zim",
        "A/Vaccine",
        "Vaccine",
        "A vaccine is a biological preparation that provides active acquired immunity. "
        "Vaccines train the immune system to recognise a pathogen.",
        "CC-BY-SA-4.0",
    ),
    _doc(
        "rfc",
        "9110",
        "RFC 9110: HTTP Semantics",
        "This document describes the overall architecture of HTTP, establishes common terminology, "
        "and defines aspects of the protocol that are shared by all versions, including caching.",
        "IETF-Trust",
    ),
    _doc(
        "gutenberg",
        "84",
        "Frankenstein; Or, The Modern Prometheus",
        "Frankenstein is a novel by Mary Shelley about Victor Frankenstein, a young scientist.",
        "Public-Domain-US",
    ),
)

SAMPLE_GEN_1 = "2026-09-01"
SAMPLE_GEN_2 = "2026-10-01"


def _sample_v2() -> tuple[DocRecord, ...]:
    turing = SAMPLE_DOCS_V1[0]
    text = turing.text + " He is widely considered the father of theoretical computer science."
    return (replace(turing, text=text, content_hash=content_hash(text), revision="2"),)


def seed_sample_library(
    *,
    index: InMemoryLexicalIndex,
    documents: InMemoryDocumentStore,
    changes: InMemoryChangeFeed,
    vault: InMemoryVault,
    ledger: InMemoryLedger,
) -> None:
    """Two generations: everything added in ``SAMPLE_GEN_1``, Alan Turing modified in ``SAMPLE_GEN_2``."""
    sealed = datetime(2026, 9, 1, tzinfo=UTC)
    for spec in SAMPLE_SOURCES:
        vault.add(Generation(spec.id, SAMPLE_GEN_1, GenerationStatus.SEALED, sealed_at=sealed), promote=True)
        ledger.append("release.state", f"{spec.id}/{SAMPLE_GEN_1}", {"state": "promoted"})
    for doc in SAMPLE_DOCS_V1:
        documents.put(SAMPLE_GEN_1, doc)
    changes.publish(
        ChangeRecord(d.doc_id, ChangeKind.ADDED, SAMPLE_GEN_1, None, d.content_hash) for d in SAMPLE_DOCS_V1
    )

    wiki = SAMPLE_SOURCES[0].id
    vault.add(
        Generation(wiki, SAMPLE_GEN_2, GenerationStatus.SEALED, sealed_at=datetime(2026, 10, 1, tzinfo=UTC)),
        promote=True,
    )
    vault.add(
        Generation(
            "rfc", SAMPLE_GEN_2, GenerationStatus.DEGRADED, sealed_at=datetime(2026, 10, 1, tzinfo=UTC)
        )
    )
    ledger.append("release.state", f"{wiki}/{SAMPLE_GEN_2}", {"state": "promoted"})
    ledger.append("scrub.mismatch", f"rfc/{SAMPLE_GEN_2}", {"relpath": "upstream/rfc-index.txt"})
    old = {d.doc_id: d.content_hash for d in SAMPLE_DOCS_V1}
    for doc in _sample_v2():
        documents.put(SAMPLE_GEN_2, doc)
    changes.publish(
        ChangeRecord(d.doc_id, ChangeKind.MODIFIED, SAMPLE_GEN_2, old[d.doc_id], d.content_hash)
        for d in _sample_v2()
    )

    index.upsert(documents.all_current())
    index.commit()


def sample_services(*, sources: Sequence[SourceSpec] | None = None, with_generator: bool = True) -> Services:
    """A fully wired :class:`Services` over the sample library. ``sources`` overrides the registry."""
    index, documents, changes = InMemoryLexicalIndex(), InMemoryDocumentStore(), InMemoryChangeFeed()
    vault, ledger = InMemoryVault(), InMemoryLedger()
    seed_sample_library(index=index, documents=documents, changes=changes, vault=vault, ledger=ledger)
    return Services(
        index=index,
        documents=documents,
        changes=changes,
        vault=vault,
        ledger=ledger,
        sources=tuple(SAMPLE_SOURCES if sources is None else sources),
        generator=EchoGenerator() if with_generator else None,
    )
