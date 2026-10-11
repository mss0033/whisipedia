"""Signed attestations stored next to a sealed generation's manifest (ADR-0009).

A sealed ``MANIFEST.json`` is never rewritten, so facts learned around sealing carry their own
signed documents, exactly like the parity sidecar (:mod:`wsp_vault.paritymeta`)::

    <generation>/PROVENANCE.json       where every byte came from: URLs, redirects, TLS peers,
    <generation>/PROVENANCE.json.sig   HTTP validators, mirrors and byte ranges, checksum channels
    <generation>/PROMOTION.json        the pre-promotion report: sanity and content-anomaly checks,
    <generation>/PROMOTION.json.sig    verdict, and the owner's decision when one was needed

Each document is canonical JSON (sorted keys), names its generation and binds itself to the exact
manifest bytes by BLAKE3 (``manifest_blake3``), and is signed by the vault's manifest signer. The
scrubber verifies the signatures; a document that fails is reported, never re-signed (re-signing
would launder tampering).

``PROVENANCE.json`` is write-once. ``PROMOTION.json`` may be replaced (a held release that the
owner approves gets a new report recording the decision); every write is a ledger event.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

import blake3

from wsp_vault.atomic import atomic_write_bytes
from wsp_vault.paritymeta import manifest_blake3
from wsp_vault.vault import FileVault, SignatureError, VaultError, make_read_only

PROVENANCE_NAME = "PROVENANCE.json"
PROMOTION_NAME = "PROMOTION.json"
SIG_SUFFIX = ".sig"
FORMATS: Mapping[str, str] = {
    PROVENANCE_NAME: "whisipedia-provenance/1",
    PROMOTION_NAME: "whisipedia-promotion/1",
}
REPLACEABLE = frozenset({PROMOTION_NAME})
_RESERVED_KEYS = frozenset({"format", "source_id", "generation_id", "manifest_blake3"})


def attestation_names() -> frozenset[str]:
    """Every file name an attestation may use inside a generation directory."""
    return frozenset({*FORMATS, *(n + SIG_SUFFIX for n in FORMATS)})


def is_attestation_path(relpath: str | None) -> bool:
    return relpath is not None and relpath in attestation_names()


def _encode(doc: Mapping[str, Any]) -> bytes:
    return (json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False, default=str) + "\n").encode()


def write_attestation(
    vault: FileVault, source_id: str, generation_id: str, name: str, body: Mapping[str, Any]
) -> str:
    """Sign and write ``name`` into a sealed generation. Returns the document's BLAKE3.

    Idempotent: writing the same bytes again is a no-op. A write-once document that already holds
    different, validly signed content raises :class:`VaultError`.
    """
    if name not in FORMATS:
        raise VaultError(f"unknown attestation {name!r}")
    clash = _RESERVED_KEYS & set(body)
    if clash:
        raise VaultError(f"attestation body may not set {sorted(clash)}")
    lib = vault.library_path(source_id, generation_id)
    vault.manifest(source_id, generation_id)  # sealed and validly signed, or SignatureError
    doc = {
        **body,
        "format": FORMATS[name],
        "source_id": source_id,
        "generation_id": generation_id,
        "manifest_blake3": manifest_blake3(vault, source_id, generation_id),
    }
    data = _encode(doc)
    digest = blake3.blake3(data).hexdigest()
    path, sig = lib / name, lib / (name + SIG_SUFFIX)
    if path.exists():
        try:
            existing = read_attestation(vault, source_id, generation_id, name)
        except SignatureError:
            existing = None  # torn write (data without a matching signature): write it again
        if existing is not None:
            if path.read_bytes() == data:
                return digest
            if name not in REPLACEABLE:
                raise VaultError(f"{source_id}/{generation_id}: {name} exists with other content")
    atomic_write_bytes(path, data, overwrite=True)
    atomic_write_bytes(sig, vault.signer.sign(data), overwrite=True)
    make_read_only(path)
    make_read_only(sig)
    ledger = vault.ledger
    if ledger is not None:
        ledger.append("vault.attest", f"{source_id}/{generation_id}", {"name": name, "blake3": digest})
    return digest


def read_attestation(
    vault: FileVault, source_id: str, generation_id: str, name: str
) -> dict[str, Any] | None:
    """The verified document, ``None`` if absent. Raises :class:`SignatureError` if it is not valid."""
    if name not in FORMATS:
        raise VaultError(f"unknown attestation {name!r}")
    lib = vault.library_path(source_id, generation_id)
    subject = f"{source_id}/{generation_id}"
    path = lib / name
    if not path.exists():
        return None
    try:
        data = path.read_bytes()
        sig = (lib / (name + SIG_SUFFIX)).read_bytes()
    except FileNotFoundError as exc:
        raise SignatureError(f"{subject}: {exc.filename} missing") from exc
    if not vault.signer.verify(data, sig):
        raise SignatureError(f"{subject}: {name} signature invalid")
    try:
        doc = cast(dict[str, Any], json.loads(data))
    except ValueError as exc:
        raise SignatureError(f"{subject}: {name} is not JSON: {exc}") from exc
    if doc.get("format") != FORMATS[name]:
        raise SignatureError(f"{subject}: {name} has format {doc.get('format')!r}")
    if (doc.get("source_id"), doc.get("generation_id")) != (source_id, generation_id):
        raise SignatureError(f"{subject}: {name} names another generation")
    if doc.get("manifest_blake3") != manifest_blake3(vault, source_id, generation_id):
        raise SignatureError(f"{subject}: {name} was made for another manifest")
    return doc


def attestation_state(vault: FileVault, source_id: str, generation_id: str, name: str) -> str:
    """``present`` (valid), ``missing`` or ``invalid``: for health reporting."""
    try:
        return "present" if read_attestation(vault, source_id, generation_id, name) is not None else "missing"
    except (SignatureError, OSError):
        return "invalid"
