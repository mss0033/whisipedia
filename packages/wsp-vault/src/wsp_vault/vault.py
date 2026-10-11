"""``FileVault``: the reference :class:`wsp_core.ports.Vault` on a plain directory tree (PLAN §7).

Layout under ``root``::

    library/<domain>/<source_id>/CURRENT.txt          pointer file (generation id + LF)
    library/<domain>/<source_id>/CURRENT.d/<p>.txt    partitioned sources: current generation of
                                                      partition <p> (ADR-0010)
    library/<domain>/<source_id>/<generation_id>/     sealed, read-only generation
        upstream/...  MANIFEST.json  MANIFEST.json.sig  parity/  [DEGRADED.txt]
    staging/<domain>/<source_id>/<generation_id>/     generation being assembled
    quarantine/...                                    damaged files moved aside, never deleted
    pruned/<domain>/<source_id>/<generation_id>.<ts>/ generations retired by retention (owner empties)
    keys/<key_id>-<fingerprint>.pub                   public verification keys only

**Why a separate ``staging/`` tree rather than a ``.staging`` suffix:** everything under
``library/`` is sealed, so readers, the scrubber and the catalog can walk it without filtering;
staging is on the same filesystem, so sealing is one directory ``rename`` that publishes the whole
generation at once; and abandoned staging can be inspected or cleaned without touching the library.

``<domain>`` is not part of the port. It is resolved from an existing directory first (so data is
never split if the registry changes), then the injected ``domains`` mapping (built from the registry
with :func:`domains_from_specs`, i.e. :attr:`SourceSpec.vault_domain`), then ``default_domain``.

**Rules.** All writes go through :mod:`wsp_vault.atomic` (unique temporaries, read-back, no-replace
publish); nothing is ever deleted except our own temporaries and the staging record. Sealed files are
chmod read-only; on the SMB share that only sets the DOS READONLY attribute, which stops accidents,
not a determined writer (it is not immutability). ``prune`` (retention) moves a generation under
``pruned/``; emptying that folder is a deliberate owner action, never automatic.

**Locking.** Every mutation takes the vault's local ``flock`` locks (:mod:`wsp_vault.locks`), so the
orchestrator, ``wsp ingest``, ``wsp scrub`` and its repair ladder can run at the same time without
losing staging records or interleaving pointer writes. ``lock_dir`` must be on local disk.

**Key pinning.** The first seal publishes the signer's public key under ``keys/``. From then on the
vault refuses (:class:`KeyPinError`) any signer whose public key is not already there, and a vault
with signed content but an empty ``keys/`` only accepts a signer that verifies its manifests. A
lost or regenerated key therefore stops every writer with a clear error instead of signing new
manifests that make all the old ones look forged (vault-fetch audit #1).

**Reading.** Manifests handed to readers are signature-checked; the check is cached per
``(mtime, size)`` of the manifest and its signature, so a sealed manifest is verified once per
process. The scrubber asks for a fresh verification (``manifest(..., fresh=True)``).

**Partitioned sources** (ADR-0010, ``access.partitioned``): generation ids are ``<cycle>.<partition>``.
``promote`` writes the partition's pointer under ``CURRENT.d/`` and then ``CURRENT.txt`` (which keeps
naming the most recently promoted generation, so older readers still work); :meth:`FileVault.current_set`
is the current generation of every partition; ``prune`` refuses all of them and the newest sealed
generation of each partition. A source directory with a ``CURRENT.d/`` is read as partitioned
whatever the registry says, so the pointers are never ignored.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import blake3

from wsp_core.checksums import normalize_algorithm
from wsp_core.model import (
    Artifact,
    Checksum,
    FetchResult,
    Generation,
    GenerationStatus,
    SourceSpec,
    Verdict,
    VerdictStatus,
)
from wsp_core.partitions import current_set_ids as set_ids_from_pointers
from wsp_core.partitions import partition_of
from wsp_core.ports import Ledger, ParityProvider, Signer
from wsp_vault.atomic import (
    DEFAULT_RETRY,
    PARTIAL_SUFFIX,
    IntegrityError,
    RetryPolicy,
    atomic_copy,
    atomic_write_bytes,
    fsync_dir,
    publish_noreplace,
    readback,
    rename_with_retry,
    temp_path,
)
from wsp_vault.hashing import Digests, supported_extra
from wsp_vault.locks import VaultLocks, default_lock_dir
from wsp_vault.manifest import (
    DEGRADED_NAME,
    MANIFEST_NAME,
    PARITY_DIR,
    ROLE_DATA,
    ROLE_PARITY,
    SIGNATURE_NAME,
    STAGING_RECORD,
    Manifest,
    ManifestEntry,
    ManifestError,
    casefold_collisions,
    check_relpath,
)
from wsp_vault.parity import DescribesParity, ParityError, check_parity_binding
from wsp_vault.signing import PublishableSigner

LIBRARY_DIR = "library"
STAGING_DIR = "staging"
QUARANTINE_DIR = "quarantine"
PRUNED_DIR = "pruned"
KEYS_DIR = "keys"
CURRENT_NAME = "CURRENT.txt"
PARTITIONS_DIR = "CURRENT.d"  # partitioned sources: <partition>.txt per partition (ADR-0010)
ADOPTING_SUFFIX = ".adopting"  # a file moved out of _incoming, being hashed before it is published
READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
PAR2_MAX_FILES = 32_000  # par2 needs one source block per file and allows 32768 blocks

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_MANIFEST_CACHE_BYTES = 64 * 1024 * 1024  # of manifest JSON; the whole vault holds ~25 MB today
_GENERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PIN_SAMPLES = 5  # manifests tried when keys/ is empty


class VaultError(Exception):
    pass


class SignatureError(VaultError):
    pass


class KeyPinError(VaultError):
    """The signer's public key is not one of the vault's pinned keys (``keys/``). Nothing was written."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def domains_from_specs(specs: Iterable[SourceSpec]) -> dict[str, str]:
    """``source_id -> domain`` from the registry (:attr:`SourceSpec.vault_domain`, ADR-0005)."""
    return {s.id: s.vault_domain for s in specs}


def partitioned_from_specs(specs: Iterable[SourceSpec]) -> frozenset[str]:
    """Ids of the registry's partitioned sources (:attr:`SourceSpec.partitioned`, ADR-0010)."""
    return frozenset(s.id for s in specs if s.partitioned)


def check_expected(digests: Digests, expected: FetchResult | None) -> None:
    """Raise :class:`IntegrityError` if a read-back disagrees with what the fetcher computed."""
    if expected is None:
        return
    if (digests.size, digests.sha256, digests.blake3) != (expected.size, expected.sha256, expected.blake3):
        raise IntegrityError(
            f"read-back {digests.size} bytes sha256 {digests.sha256} != fetched {expected.size} bytes "
            f"sha256 {expected.sha256}"
        )


def check_upstream(digests: Digests, upstream: Sequence[Checksum]) -> None:
    """Raise :class:`IntegrityError` if any upstream checksum we could compute disagrees.

    Algorithm names are normalized (``SHA-256`` is ``sha256``), so an upstream digest is never
    recorded under one spelling and skipped under another. Algorithms hashlib cannot compute are
    recorded in the manifest but cannot be checked here.
    """
    ours = {"sha256": digests.sha256, "blake3": digests.blake3, **digests.extra}
    for c in upstream:
        got = ours.get(normalize_algorithm(c.algorithm))
        if got is not None and got != c.hexdigest.strip().lower():
            raise IntegrityError(f"upstream {c.algorithm} {c.hexdigest} != computed {got}")


def make_read_only(path: Path) -> None:
    """chmod 0444. On SMB this sets the DOS READONLY attribute: a guard against accidents only."""
    with suppress(OSError):
        os.chmod(path, READ_ONLY)


# --- key pinning helpers (also used by the CLI before a vault exists) ---------------------------


def key_material(public_key_text: str) -> tuple[str, ...]:
    """``(type, base64)`` of an OpenSSH public key line; the comment is ignored."""
    return tuple(public_key_text.split()[:2])


def key_label(public_key_text: str, key_id: str) -> str:
    """``<key_id>-<fingerprint>``: the name the key has (or would have) under ``keys/``."""
    return f"{key_id}-{blake3.blake3(public_key_text.encode()).hexdigest()[:12]}"


def pinned_keys(root: Path) -> list[tuple[str, str]]:
    """``(label, public key text)`` of every key published under ``<root>/keys/``, sorted by label."""
    keys = root / KEYS_DIR
    if not keys.is_dir():
        return []
    return [(p.stem, p.read_text(encoding="utf-8")) for p in sorted(keys.glob("*.pub")) if p.is_file()]


def signed_manifests(root: Path, limit: int | None = None) -> list[Path]:
    """Sealed ``MANIFEST.json`` files that have a signature next to them, in path order (at most
    ``limit``; the walk stops as soon as it has enough)."""
    lib = root / LIBRARY_DIR
    out: list[Path] = []
    if not lib.is_dir():
        return out
    for domain in sorted(lib.iterdir()):
        for source in sorted(domain.iterdir()) if domain.is_dir() else ():
            for gen in sorted(source.iterdir()) if source.is_dir() else ():
                if (gen / MANIFEST_NAME).is_file() and (gen / SIGNATURE_NAME).is_file():
                    out.append(gen / MANIFEST_NAME)
                    if limit is not None and len(out) >= limit:
                        return out
    return out


def first_signed_manifest(root: Path) -> Path | None:
    """Any sealed ``MANIFEST.json`` with a signature next to it (stops at the first one found)."""
    found = signed_manifests(root, 1)
    return found[0] if found else None


def has_signed_content(root: Path) -> bool:
    """The vault has published keys or at least one signed manifest (a signer is already bound)."""
    return bool(pinned_keys(root)) or first_signed_manifest(root) is not None


def _unique(path: Path) -> Path:
    """``path`` if free, else ``path`` with a random suffix (quarantine and prune never overwrite)."""
    return path if not path.exists() else temp_path(path).with_suffix("")


class FileVault:
    """See the module docstring for layout and guarantees."""

    def __init__(
        self,
        root: Path,
        signer: Signer,
        *,
        domains: Mapping[str, str] | None = None,
        default_domain: str = "misc",
        partitioned: Iterable[str] = (),
        ledger: Ledger | None = None,
        parity: ParityProvider | None = None,
        retry: RetryPolicy = DEFAULT_RETRY,
        clock: Callable[[], datetime] = utcnow,
        lock_dir: Path | None = None,
    ) -> None:
        """``lock_dir`` holds the mutation locks and must be on local disk (never inside the vault);
        every process that writes this vault must use the same one. ``None``: a per-user directory
        derived from the vault path (:func:`wsp_vault.locks.default_lock_dir`)."""
        self._root = root
        self._signer = signer
        self._domains = dict(domains or {})
        self._default_domain = default_domain
        self._partitioned = frozenset(partitioned)
        self._ledger = ledger
        self._parity = parity
        self._retry = retry
        self._clock = clock
        locks = lock_dir if lock_dir is not None else default_lock_dir(root)
        if locks.resolve().is_relative_to(root.resolve()):
            raise ValueError("vault locks must live on local disk, not inside the vault (ADR-0002)")
        self._locks = VaultLocks(locks)
        self._pin_ok = False
        # Verified manifests by path, revalidated by (mtime_ns, size) of the manifest and its
        # signature: sealed manifests never change, and re-reading megabytes of them over SMB on
        # every read made the API's source list take seconds.
        self._manifests: OrderedDict[Path, tuple[tuple[int, ...], Manifest]] = OrderedDict()
        self._manifests_lock = threading.Lock()
        for domain in [*self._domains.values(), default_domain]:
            if not _NAME_RE.match(domain):
                raise VaultError(f"invalid domain name: {domain!r}")

    @property
    def root(self) -> Path:
        return self._root

    @property
    def signer(self) -> Signer:
        return self._signer

    @property
    def ledger(self) -> Ledger | None:
        return self._ledger

    @property
    def locks(self) -> VaultLocks:
        """The vault's local locks, for other mutators of sealed generations (repair, parity, attest)."""
        return self._locks

    # --- key pinning -------------------------------------------------------------------------------

    def check_signer(self) -> None:
        """Raise :class:`KeyPinError` unless the signer belongs to this vault (see the module doc).

        Checked once per vault object before its first mutation. A signer that cannot show its
        public key (no ``public_key_text``, e.g. a verify-only keyring built from ``keys/``) cannot
        be compared and is accepted.
        """
        if self._pin_ok:
            return
        signer = self._signer
        if isinstance(signer, PublishableSigner):
            text = signer.public_key_text()
            mine = key_label(text, signer.key_id)
            pinned = pinned_keys(self._root)
            if pinned:
                if key_material(text) not in {key_material(t) for _, t in pinned}:
                    raise KeyPinError(
                        f"refusing to use signing key {mine} with the vault at {self._root}: it is not "
                        f"one of the pinned keys in {self._root / KEYS_DIR} "
                        f"({', '.join(label for label, _ in pinned)}). The configured private key is "
                        "probably new or regenerated; signing with it would make every existing "
                        "manifest look forged. Restore the original private key from your backup "
                        "(vault.signing_key; default $XDG_CONFIG_HOME/whisipedia/"
                        "vault-signing-ed25519.pem). Key rotation is never automatic."
                    )
            else:
                # keys/ lost but signed content exists: the signer must verify one of a few manifests
                # (several, so a single damaged signature does not make the right key look foreign)
                samples = signed_manifests(self._root, _PIN_SAMPLES)
                if samples and not any(
                    signer.verify(m.read_bytes(), m.with_name(SIGNATURE_NAME).read_bytes()) for m in samples
                ):
                    raise KeyPinError(
                        f"refusing to use signing key {mine} with the vault at {self._root}: keys/ is "
                        f"empty but the vault has signed manifests and none of {len(samples)} sampled "
                        f"({samples[0]}, ...) verifies with this key. Restore the original private key, "
                        "or put its public key back under keys/ if only that directory was lost."
                    )
        self._pin_ok = True

    # --- layout ----------------------------------------------------------------------------------

    def domain_of(self, source_id: str) -> str:
        _check_source(source_id)
        for tree in (LIBRARY_DIR, STAGING_DIR):
            base = self._root / tree
            if base.is_dir():
                for domain_dir in sorted(base.iterdir()):
                    if (domain_dir / source_id).is_dir():
                        return domain_dir.name
        return self._domains.get(source_id, self._default_domain)

    def source_dir(self, source_id: str) -> Path:
        return self._root / LIBRARY_DIR / self.domain_of(source_id) / source_id

    def staging_path(self, source_id: str, generation_id: str) -> Path:
        _check_generation(generation_id)
        return self._root / STAGING_DIR / self.domain_of(source_id) / source_id / generation_id

    def library_path(self, source_id: str, generation_id: str) -> Path:
        _check_generation(generation_id)
        return self.source_dir(source_id) / generation_id

    def generation_path(self, source_id: str, generation_id: str) -> Path:
        """The sealed directory if it exists, else the staging directory if that exists, else the
        location the sealed generation will have."""
        lib = self.library_path(source_id, generation_id)
        if lib.is_dir():
            return lib
        stg = self.staging_path(source_id, generation_id)
        return stg if stg.is_dir() else lib

    def sources(self) -> list[str]:
        found: set[str] = set()
        for tree in (LIBRARY_DIR, STAGING_DIR):
            base = self._root / tree
            if base.is_dir():
                found.update(p.name for d in base.iterdir() if d.is_dir() for p in d.iterdir() if p.is_dir())
        return sorted(found)

    # --- staging ---------------------------------------------------------------------------------

    def stage(self, source_id: str, generation_id: str) -> Path:
        self.check_signer()
        with self._locks.generation(source_id, generation_id, exclusive=False):
            if (self.library_path(source_id, generation_id) / MANIFEST_NAME).exists():
                raise VaultError(f"{source_id}/{generation_id} is already sealed")
            path = self.staging_path(source_id, generation_id)
            path.mkdir(parents=True, exist_ok=True)
            return path

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
        """Copy ``local`` into staging (hash, fsync, read back, publish). Idempotent for identical bytes.

        ``upstream`` checksums are verified when hashlib can compute them and recorded either way.
        ``expected`` (the fetcher's digests) must match the read-back; it also spares re-hashing
        ``local`` when the file turns out to be staged already. Many ingests into one generation may
        run at once (threads or processes); each file is published with a no-replace rename and the
        staging record is updated under the generation's record lock.
        """
        rel = check_relpath(relpath)
        with self._locks.generation(source_id, generation_id, exclusive=False):
            staging = self.stage(source_id, generation_id)
            self._check_case(source_id, generation_id, staging, rel.as_posix())
            _require_regular(local)
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            extra = supported_extra(c.algorithm for c in upstream)

            def validate(d: Digests) -> None:
                check_expected(d, expected)
                check_upstream(d, upstream)

            digests: Digests | None = None
            if not dest.exists():
                with suppress(FileExistsError):  # another writer published the same path meanwhile
                    digests = atomic_copy(local, dest, extra=extra, validate=validate, retry=self._retry)
            if digests is None:
                have = readback(dest, extra=extra)
                if expected is None:
                    new = readback(local, extra=extra)
                    if have != new:
                        raise VaultError(f"{relpath} is already staged with different content")
                else:
                    try:
                        check_expected(have, expected)
                    except IntegrityError as exc:
                        raise VaultError(f"{relpath} is already staged with different content") from exc
                check_upstream(have, upstream)
                digests = have
            return self._record(
                source_id, generation_id, staging, rel.as_posix(), digests, upstream, "ingest"
            )

    def adopt(
        self,
        source_id: str,
        generation_id: str,
        path: Path,
        relpath: str,
        *,
        upstream: Sequence[Checksum] = (),
        expected: Digests | None = None,
    ) -> Artifact:
        """Move a file already on the vault filesystem (e.g. ``<root>/_incoming/...``) into staging.

        No bytes are copied. The file is first renamed to ``<relpath>.adopting`` inside staging (so
        nobody can open it by its old name and change it), hashed there, checked against
        ``expected`` and ``upstream``, checked unchanged (size, mtime, inode) across the hash, and
        only then published under ``relpath``. A failed check renames it back. Resumes cleanly after
        a crash at any step.
        """
        with self._locks.generation(source_id, generation_id, exclusive=False):
            staging = self.stage(source_id, generation_id)
            self._check_case(source_id, generation_id, staging, relpath)
            rel, digests = self._adopt_file(staging, path, relpath, upstream, expected)
            return self._record(source_id, generation_id, staging, rel, digests, upstream, "adopt")

    def _adopt_file(
        self,
        staging: Path,
        path: Path,
        relpath: str,
        upstream: Sequence[Checksum],
        expected: Digests | None = None,
    ) -> tuple[str, Digests]:
        """Move ``path`` aside into staging, hash it, check it, publish it as ``staging/relpath``.

        No staging record (the caller records). The caller holds the generation lock (shared).
        """
        rel = check_relpath(relpath)
        dest = staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        moving = dest.with_name(dest.name + ADOPTING_SUFFIX)
        extra = supported_extra(c.algorithm for c in upstream)

        def validate(d: Digests) -> None:
            if expected is not None and d != expected:
                raise IntegrityError(f"{path}: {d.sha256} does not match expected {expected.sha256}")
            check_upstream(d, upstream)

        if not path.exists() and not moving.exists() and dest.exists():  # adopted before a crash
            digests = readback(dest, extra=extra)
            validate(digests)
            return rel.as_posix(), digests
        if path.exists():
            _require_regular(path)
            if dest.exists():
                raise VaultError(f"{relpath} is already staged; refusing to replace it")
            if moving.exists():
                raise VaultError(f"an interrupted adoption left {moving}; inspect it and {path} by hand")
            if os.stat(dest.parent).st_dev != os.stat(path).st_dev:
                raise VaultError(f"{path} is on a different filesystem; use ingest() to copy it")
            os.rename(path, moving)
            fsync_dir(path.parent)
            fsync_dir(moving.parent)
        elif not moving.exists():
            raise FileNotFoundError(path)
        try:
            before = os.stat(moving)
            digests = readback(moving, extra=extra)
            after = os.stat(moving)
            stamp = (before.st_size, before.st_mtime_ns, before.st_ino)
            if stamp != (after.st_size, after.st_mtime_ns, after.st_ino) or after.st_size != digests.size:
                raise VaultError(
                    f"{path} changed while it was being hashed; adopt it again when it is complete"
                )
            validate(digests)
        except BaseException:
            with suppress(OSError):
                if not path.exists():  # give the file back where it came from
                    os.rename(moving, path)
            raise
        publish_noreplace(moving, dest)
        fsync_dir(dest.parent)
        return rel.as_posix(), digests

    def adopt_many(
        self,
        source_id: str,
        generation_id: str,
        items: Iterable[tuple[Path, str, Sequence[Checksum]]],
        *,
        workers: int = 4,
        flush_every: int = 1000,
    ) -> int:
        """:meth:`adopt` for many files (mirror trees with thousands of files per generation).

        Same checks and the same crash recovery per file, but files are hashed on ``workers``
        threads and the staging record is rewritten once per ``flush_every`` files (not once per
        file, which is quadratic), with one ``vault.adopt_batch`` ledger event per flush. Each flush
        re-reads the record under its lock and merges, so concurrent ingests are never lost. A crash
        between a publish and the next flush is recovered on re-run: the staged file is re-hashed.
        Returns the number of files adopted. Stops at the first failing file (already adopted
        files stay recorded).
        """
        with self._locks.generation(source_id, generation_id, exclusive=False):
            staging = self.stage(source_id, generation_id)
            pending: list[tuple[str, Digests, Sequence[Checksum]]] = []
            count = 0

            def flush() -> None:
                if not pending:
                    return
                with self._locks.record(source_id, generation_id):
                    records = _read_staging(staging)
                    for rel, digests, upstream in pending:
                        records[rel] = ManifestEntry.from_digests(rel, digests, upstream=upstream)
                    self._write_record(staging, records)
                self._log(
                    "vault.adopt_batch",
                    f"{source_id}/{generation_id}",
                    {"files": len(pending), "bytes": sum(d.size for _, d, _ in pending)},
                )
                pending.clear()

            def one(item: tuple[Path, str, Sequence[Checksum]]) -> tuple[str, Digests, Sequence[Checksum]]:
                path, relpath, upstream = item
                rel, digests = self._adopt_file(staging, path, relpath, upstream)
                return rel, digests, upstream

            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                window: deque[Future[tuple[str, Digests, Sequence[Checksum]]]] = deque()
                try:
                    for item in items:
                        window.append(pool.submit(one, item))
                        if len(window) >= 2 * max(1, workers):
                            pending.append(window.popleft().result())
                            count += 1
                            if len(pending) >= flush_every:
                                flush()
                    while window:
                        pending.append(window.popleft().result())
                        count += 1
                finally:
                    for fut in window:  # record whatever finished before a failure
                        if not fut.cancel() and fut.exception() is None:
                            pending.append(fut.result())
                            count += 1
                    flush()
            return count

    def _check_case(self, source_id: str, generation_id: str, staging: Path, relpath: str) -> None:
        """Refuse a path that differs only in case from a recorded one (the share is case-insensitive)."""
        with self._locks.record(source_id, generation_id):
            for other in _read_staging(staging):
                if other != relpath and other.casefold() == relpath.casefold():
                    raise VaultError(f"{relpath} collides with {other} on a case-insensitive share")

    def _record(
        self,
        source_id: str,
        generation_id: str,
        staging: Path,
        relpath: str,
        digests: Digests,
        upstream: Sequence[Checksum],
        how: str,
    ) -> Artifact:
        with self._locks.record(source_id, generation_id):
            records = _read_staging(staging)
            clash = [p for p in records if p != relpath and p.casefold() == relpath.casefold()]
            if clash:
                raise VaultError(f"{relpath} collides with {clash[0]} on a case-insensitive share")
            previous = records.get(relpath)
            if previous is not None and previous.digests == digests:
                upstream = [*previous.upstream, *upstream]
            entry = ManifestEntry.from_digests(relpath, digests, upstream=upstream)
            records[relpath] = entry
            self._write_record(staging, records)
        self._log(
            f"vault.{how}",
            f"{source_id}/{generation_id}",
            {"path": relpath, "size": digests.size, "sha256": digests.sha256, "blake3": digests.blake3},
        )
        return entry.to_artifact(source_id, generation_id)

    def _write_record(self, staging: Path, records: Mapping[str, ManifestEntry]) -> None:
        payload = {p: e.to_json() for p, e in sorted(records.items())}
        atomic_write_bytes(
            staging / STAGING_RECORD,
            json.dumps(payload, sort_keys=True, indent=1).encode(),
            overwrite=True,
            retry=self._retry,
        )

    # --- sealing and promotion -------------------------------------------------------------------

    def seal(self, source_id: str, generation_id: str, *, parity_percent: int | None = None) -> Generation:
        """Write and sign ``MANIFEST.json``, move staging into ``library/``, make files read-only.

        Idempotent: re-running after a crash at any step completes the seal. With ``parity_percent``
        and a configured parity provider, every data file is re-hashed (and its MD5 taken in the same
        pass), parity is created from the staged files and bound to those verified bytes (par2's
        own MD5 and length per file must match, see :mod:`wsp_vault.parity`), and listed in the
        manifest. Without parity, files are checked by size here; their content was read back and
        verified when they were ingested, and the scrub re-hashes them on its cycle.
        """
        self.check_signer()
        with self._locks.generation(source_id, generation_id, exclusive=True):
            lib = self.library_path(source_id, generation_id)
            stg = self.staging_path(source_id, generation_id)
            if not (lib / MANIFEST_NAME).exists():
                if not stg.is_dir():
                    raise VaultError(f"{source_id}/{generation_id}: nothing staged")
                interrupted = sorted(p for p in stg.rglob(f"*{ADOPTING_SUFFIX}") if p.is_file())
                if interrupted:
                    raise VaultError(
                        f"interrupted adoption(s) in staging, re-run adopt first: {interrupted[:3]}"
                    )
                if not self._staged_manifest_current(stg):
                    self._write_manifest(source_id, generation_id, stg, parity_percent)
                lib.parent.mkdir(parents=True, exist_ok=True)
                if lib.exists():
                    raise VaultError(f"{lib} exists without a manifest; inspect it by hand")
                rename_with_retry(stg, lib)
                fsync_dir(lib.parent)
                for parent in (stg.parent, stg.parent.parent):
                    with suppress(OSError):
                        parent.rmdir()
            manifest = self.manifest(source_id, generation_id, fresh=True)
            for path in [
                lib / MANIFEST_NAME,
                lib / SIGNATURE_NAME,
                *(lib / e.path for e in manifest.entries),
            ]:
                make_read_only(path)
        self.publish_key()
        self._log(
            "vault.seal",
            f"{source_id}/{generation_id}",
            {
                "files": len(manifest.entries),
                "bytes": manifest.total_bytes,
                "manifest_blake3": self._mhash(lib),
            },
        )
        return self._generation(source_id, generation_id, lib, manifest)

    def _staged_manifest_current(self, stg: Path) -> bool:
        """A signed manifest in staging that still describes what is staged (crash recovery).

        With a staging record present the manifest must list exactly the recorded data files with
        the recorded digests; the record is then removed (it would otherwise be published inside the
        sealed generation). Without a record, a validly signed manifest is the authority.
        """
        m, s = stg / MANIFEST_NAME, stg / SIGNATURE_NAME
        if not (m.exists() and s.exists()):
            return False
        data = m.read_bytes()
        if not self._signer.verify(data, s.read_bytes()):
            return False
        record = stg / STAGING_RECORD
        if record.exists():
            try:
                manifest = Manifest.from_bytes(data)
            except ManifestError:
                return False
            listed = {e.path: e.digests for e in manifest.data}
            if listed != {p: e.digests for p, e in _read_staging(stg).items()}:
                return False  # something was ingested after the manifest was written
            record.unlink()
        return True

    def _write_manifest(
        self, source_id: str, generation_id: str, stg: Path, parity_percent: int | None
    ) -> None:
        records = _read_staging(stg)
        on_disk: set[str] = set()
        for path in stg.rglob("*"):
            rel = path.relative_to(stg).as_posix()
            if path.is_symlink():
                raise VaultError(f"symlink in staging: {rel}")
            if not path.is_file() or rel == STAGING_RECORD or rel.startswith(f"{PARITY_DIR}/"):
                continue
            if path.name.endswith(PARTIAL_SUFFIX):
                path.unlink()  # our own temporary; the exclusive generation lock rules out a live writer
                continue
            if rel in (MANIFEST_NAME, SIGNATURE_NAME):
                continue
            on_disk.add(rel)
        if not records:
            raise VaultError(f"{source_id}/{generation_id}: nothing staged")
        if on_disk != set(records):
            raise VaultError(
                f"staging does not match ingest record: unrecorded={sorted(on_disk - set(records))} "
                f"missing={sorted(set(records) - on_disk)}"
            )
        clashes = casefold_collisions(records)
        if clashes:
            raise VaultError(f"paths that collide on a case-insensitive share: {clashes[:3]}")
        for rel, entry in records.items():
            if (stg / rel).stat().st_size != entry.size:
                raise VaultError(f"{rel}: size changed since ingest")
        entries = list(records.values())
        for name in (MANIFEST_NAME, SIGNATURE_NAME):
            with suppress(FileNotFoundError):
                (stg / name).unlink()
        if parity_percent and self._parity is not None:
            entries += self._seal_parity(source_id, generation_id, stg, entries, parity_percent)
        manifest = Manifest(source_id, generation_id, self._clock(), tuple(entries))
        data = manifest.to_bytes()
        atomic_write_bytes(stg / MANIFEST_NAME, data, retry=self._retry)
        atomic_write_bytes(stg / SIGNATURE_NAME, self._signer.sign(data), retry=self._retry)
        (stg / STAGING_RECORD).unlink()

    def _seal_parity(
        self,
        source_id: str,
        generation_id: str,
        stg: Path,
        entries: Sequence[ManifestEntry],
        percent: int,
    ) -> list[ManifestEntry]:
        """Parity over the staged data, bound to bytes verified in the same pass (audit #7)."""
        assert self._parity is not None
        out = stg / PARITY_DIR
        subject = f"{source_id}/{generation_id}"
        if len(entries) > PAR2_MAX_FILES:  # par2 cannot hold one block per file; the keeper splits sets
            self._log("vault.parity_deferred", subject, {"files": len(entries), "limit": PAR2_MAX_FILES})
            return []
        verified: dict[Path, tuple[str, int]] = {}
        for e in entries:
            got = readback(stg / e.path, extra=("md5",))
            if got != e.digests:
                raise IntegrityError(f"{subject} {e.path}: staged bytes no longer match the ingest record")
            verified[stg / e.path] = (got.extra["md5"], got.size)
        if out.exists():  # left by a crashed seal: our own temporary output, never sealed
            shutil.rmtree(out)
        out.mkdir()
        files = [stg / e.path for e in entries]
        created = self._parity.create(files, out, percent)
        if isinstance(self._parity, DescribesParity):
            try:
                check_parity_binding(self._parity.describe(created, files), verified)
            except ParityError:
                shutil.rmtree(out, ignore_errors=True)
                raise
        parity_entries: list[ManifestEntry] = []
        for path in created:
            rel = path.relative_to(stg).as_posix()
            parity_entries.append(ManifestEntry.from_digests(rel, readback(path), role=ROLE_PARITY))
        return parity_entries

    def promote(self, source_id: str, generation_id: str) -> None:
        """Atomically rewrite ``CURRENT.txt``. Only a sealed, validly signed, non-degraded generation.

        For a partitioned source the partition's pointer ``CURRENT.d/<partition>.txt`` is written
        first (the other partitions keep theirs), then ``CURRENT.txt``. Holds the source's pointer
        lock and the generation's lock (shared), so a concurrent ``mark_degraded`` cannot slip in
        between the DEGRADED check and the pointer write.
        """
        self.check_signer()
        with (
            self._locks.source(source_id),
            self._locks.generation(source_id, generation_id, exclusive=False),
        ):
            lib = self.library_path(source_id, generation_id)
            self.manifest(source_id, generation_id, fresh=True)
            if (lib / DEGRADED_NAME).exists():
                raise VaultError(f"{source_id}/{generation_id} is DEGRADED")
            previous = self._read_current(source_id)
            data: dict[str, object] = {"previous": previous}
            partition = partition_of(generation_id) if self.is_partitioned(source_id) else None
            if partition is not None:
                pointers = self._read_pointers(source_id)
                data.update(partition=partition, previous_in_partition=pointers.get(partition))
                (self.source_dir(source_id) / PARTITIONS_DIR).mkdir(exist_ok=True)
                atomic_write_bytes(
                    self.source_dir(source_id) / PARTITIONS_DIR / f"{partition}.txt",
                    f"{generation_id}\n".encode(),
                    overwrite=True,
                    retry=self._retry,
                )
            atomic_write_bytes(
                self.source_dir(source_id) / CURRENT_NAME,
                f"{generation_id}\n".encode(),
                overwrite=True,
                retry=self._retry,
            )
            self._log("vault.promote", f"{source_id}/{generation_id}", data)

    def is_partitioned(self, source_id: str) -> bool:
        """Configured as partitioned, or already has per-partition pointers on disk."""
        return source_id in self._partitioned or (self.source_dir(source_id) / PARTITIONS_DIR).is_dir()

    def _read_pointers(self, source_id: str) -> dict[str, str]:
        """``partition -> generation id`` from ``CURRENT.d/`` (empty for ordinary sources)."""
        base = self.source_dir(source_id) / PARTITIONS_DIR
        out: dict[str, str] = {}
        if not base.is_dir():
            return out
        for path in sorted(base.glob("*.txt")):
            value = path.read_text(encoding="utf-8").strip()
            if value and partition_of(value) == path.stem:
                out[path.stem] = value
        return out

    def current_set_ids(self, source_id: str) -> list[str]:
        """Ids of :meth:`current_set` (no manifest reads)."""
        current = self._read_current(source_id)
        if not self.is_partitioned(source_id):
            return [] if current is None else [current]
        return set_ids_from_pointers(self._read_pointers(source_id), current)

    def _read_current(self, source_id: str) -> str | None:
        path = self.source_dir(source_id) / CURRENT_NAME
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    # --- reading ---------------------------------------------------------------------------------

    def current(self, source_id: str) -> Generation | None:
        generation_id = self._read_current(source_id)
        if generation_id is None:
            return None
        lib = self.library_path(source_id, generation_id)
        if not (lib / MANIFEST_NAME).exists():
            raise VaultError(f"{CURRENT_NAME} of {source_id} points at missing generation {generation_id}")
        return self._generation(source_id, generation_id, lib)

    def current_set(self, source_id: str) -> Sequence[Generation]:
        """The current generation of every partition (ADR-0010); ``[current]`` for ordinary sources."""
        out: list[Generation] = []
        for generation_id in self.current_set_ids(source_id):
            lib = self.library_path(source_id, generation_id)
            if not (lib / MANIFEST_NAME).exists():
                raise VaultError(f"a pointer of {source_id} names missing generation {generation_id}")
            out.append(self._generation(source_id, generation_id, lib))
        return out

    def generations(self, source_id: str) -> Sequence[Generation]:
        out: list[Generation] = []
        src = self.source_dir(source_id)
        if src.is_dir():
            for path in sorted(src.iterdir()):
                if (path / MANIFEST_NAME).is_file():
                    out.append(self._generation(source_id, path.name, path))
        stg_src = self._root / STAGING_DIR / self.domain_of(source_id) / source_id
        if stg_src.is_dir():
            for path in sorted(stg_src.iterdir()):
                if path.is_dir() and not (src / path.name).exists():
                    out.append(self._staging_generation(source_id, path))
        return sorted(out, key=lambda g: g.generation_id)

    def manifest(self, source_id: str, generation_id: str, *, fresh: bool = False) -> Manifest:
        """The sealed manifest, after checking its signature. Raises :class:`SignatureError`.

        The verified result is cached per ``(mtime, size)`` of the manifest and its signature;
        ``fresh=True`` (scrub, promote, seal) reads and verifies again regardless.
        """
        lib = self.library_path(source_id, generation_id)
        manifest = self._verified_manifest(lib, fresh=fresh)
        if (manifest.source_id, manifest.generation_id) != (source_id, generation_id):
            raise SignatureError(f"{source_id}/{generation_id}: manifest names another generation")
        return manifest

    def verify_manifest(self, source_id: str, generation_id: str) -> Verdict:
        try:
            self.manifest(source_id, generation_id, fresh=True)
        except SignatureError as exc:
            return Verdict(VerdictStatus.MISMATCH, str(exc))
        except OSError as exc:
            return Verdict(VerdictStatus.UNREADABLE, str(exc))
        return Verdict(VerdictStatus.OK)

    def _verified_manifest(self, lib: Path, *, fresh: bool) -> Manifest:
        subject = f"{lib.parent.name}/{lib.name}"
        mpath, spath = lib / MANIFEST_NAME, lib / SIGNATURE_NAME
        try:
            mst, sst = mpath.stat(), spath.stat()
        except FileNotFoundError as exc:
            raise SignatureError(f"{subject}: {exc.filename} missing") from exc
        stamp = (mst.st_mtime_ns, mst.st_size, sst.st_mtime_ns, sst.st_size)
        if not fresh:
            with self._manifests_lock:
                hit = self._manifests.get(mpath)
                if hit is not None and hit[0] == stamp:
                    self._manifests.move_to_end(mpath)
                    return hit[1]
        try:
            data, sig = mpath.read_bytes(), spath.read_bytes()
        except FileNotFoundError as exc:
            raise SignatureError(f"{subject}: {exc.filename} missing") from exc
        if not self._signer.verify(data, sig):
            raise SignatureError(f"{subject}: manifest signature invalid")
        try:
            manifest = Manifest.from_bytes(data)
        except ManifestError as exc:
            raise SignatureError(f"{subject}: {exc}") from exc
        stamp = (mst.st_mtime_ns, len(data), sst.st_mtime_ns, len(sig))
        with self._manifests_lock:
            self._manifests[mpath] = (stamp, manifest)
            self._manifests.move_to_end(mpath)
            while (
                sum(v[0][1] for v in self._manifests.values()) > _MANIFEST_CACHE_BYTES
                and len(self._manifests) > 1
            ):
                self._manifests.popitem(last=False)
        return manifest

    def _generation(
        self, source_id: str, generation_id: str, lib: Path, manifest: Manifest | None = None
    ) -> Generation:
        """View of a sealed generation. A manifest that does not verify is reported as DEGRADED."""
        if manifest is None:
            try:
                manifest = self._verified_manifest(lib, fresh=False)
            except SignatureError:
                return Generation(source_id, generation_id, GenerationStatus.DEGRADED)
        degraded = (lib / DEGRADED_NAME).exists()
        return Generation(
            source_id=source_id,
            generation_id=generation_id,
            status=GenerationStatus.DEGRADED if degraded else GenerationStatus.SEALED,
            artifacts=manifest.artifacts(),
            sealed_at=manifest.sealed_at,
        )

    def _staging_generation(self, source_id: str, stg: Path) -> Generation:
        records = _read_staging(stg)
        artifacts = tuple(e.to_artifact(source_id, stg.name) for _, e in sorted(records.items()))
        return Generation(source_id, stg.name, GenerationStatus.STAGING, artifacts)

    # --- warden support --------------------------------------------------------------------------

    def mark_degraded(self, source_id: str, generation_id: str, reason: str, files: Sequence[str]) -> Path:
        """Write ``DEGRADED.txt`` (PLAN §8.4 rung 4) and a ledger event. Data is left untouched."""
        self.check_signer()
        with self._locks.generation(source_id, generation_id, exclusive=True):
            lib = self.library_path(source_id, generation_id)
            now = self._clock().astimezone(UTC).isoformat(timespec="seconds")
            lines = [
                f"This generation ({source_id}/{generation_id}) is DEGRADED.",
                "Some files no longer match MANIFEST.json and could not be repaired automatically.",
                "Prefer the previous good generation. Runbook: recover a DEGRADED generation.",
                "",
                f"Detected: {now}",
                f"Reason: {reason}",
                "Files:",
                *(f"  {f}" for f in sorted(files)),
                "",
            ]
            path = lib / DEGRADED_NAME
            atomic_write_bytes(path, "\n".join(lines).encode(), overwrite=True, retry=self._retry)
            self._log(
                "vault.degraded", f"{source_id}/{generation_id}", {"reason": reason, "files": sorted(files)}
            )
            return path

    def _clear_degraded(self, source_id: str, generation_id: str, evidence: Mapping[str, object]) -> Path:
        """Move ``DEGRADED.txt`` into ``quarantine/`` (kept as history) and log ``vault.undegraded``.

        Only :func:`wsp_vault.undegrade.reverify_and_clear` calls this, after a full re-scrub of the
        generation passed under the same exclusive lock.
        """
        self.check_signer()
        with self._locks.generation(source_id, generation_id, exclusive=True):
            marker = self.library_path(source_id, generation_id) / DEGRADED_NAME
            moved = self.quarantine(marker)
            self._log(
                "vault.undegraded",
                f"{source_id}/{generation_id}",
                {**evidence, "degraded_record": moved.relative_to(self._root).as_posix()},
            )
            return moved

    def prune(self, source_id: str, generation_id: str) -> Path:
        """Retire a sealed generation (retention, PLAN §6.4): move it under ``pruned/``. Never deletes.

        Refuses the current generation and the newest sealed one, so a source always keeps data. For
        a partitioned source that holds per partition: no generation of the current set, and not the
        newest sealed generation of its partition (a partition is never superseded by another one).
        """
        self.check_signer()
        subject = f"{source_id}/{generation_id}"
        with (
            self._locks.source(source_id),
            self._locks.generation(source_id, generation_id, exclusive=True),
        ):
            lib = self.library_path(source_id, generation_id)
            if not (lib / MANIFEST_NAME).exists():
                raise VaultError(f"{subject} is not a sealed generation")
            if self._read_current(source_id) == generation_id or generation_id in self.current_set_ids(
                source_id
            ):
                raise VaultError(f"refusing to prune the current generation {subject}")
            sealed = [
                g.generation_id
                for g in self.generations(source_id)
                if g.status is not GenerationStatus.STAGING
            ]
            if self.is_partitioned(source_id):
                sealed = [g for g in sealed if partition_of(g) == partition_of(generation_id)]
            if sealed and sealed[-1] == generation_id:
                raise VaultError(f"refusing to prune the newest sealed generation {subject}")
            stamp = self._clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
            dest = _unique(
                self._root / PRUNED_DIR / self.domain_of(source_id) / source_id / f"{generation_id}.{stamp}"
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            rename_with_retry(lib, dest)
            fsync_dir(lib.parent)
            fsync_dir(dest.parent)
            self._log("vault.prune", subject, {"moved_to": dest.relative_to(self._root).as_posix()})
            return dest

    def quarantine(self, path: Path) -> Path:
        """Move a damaged file or directory under ``quarantine/`` (same filesystem; never deletes).

        The caller holds the lock of the generation ``path`` belongs to.
        """
        stamp = self._clock().astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        rel = path.relative_to(self._root)
        dest = _unique(self._root / QUARANTINE_DIR / rel.parent / f"{rel.name}.{stamp}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        rename_with_retry(path, dest)
        fsync_dir(path.parent)
        return dest

    def restore_file(self, source_id: str, generation_id: str, entry: ManifestEntry, src: Path) -> None:
        """Replace one file of a sealed generation with a verified copy of ``src``.

        The copy is verified against the manifest (into a temporary name of its own) before the
        damaged file, if any, is quarantined; the copy is then published without replacing anything.
        """
        self.check_signer()
        with self._locks.generation(source_id, generation_id, exclusive=True):
            lib = self.library_path(source_id, generation_id)
            dest = lib / entry.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            staged = temp_path(dest, "repair")
            try:
                atomic_copy(src, staged, expected=entry.digests, retry=self._retry)
                if dest.exists():
                    self.quarantine(dest)
                publish_noreplace(staged, dest)
            except BaseException:
                with suppress(OSError):
                    staged.unlink()
                raise
            fsync_dir(dest.parent)
            make_read_only(dest)

    def publish_key(self) -> Path | None:
        """Write the signer's public key to ``keys/<key_id>-<fingerprint>.pub`` if not already there.

        Pinning (:meth:`check_signer`) runs first, so this only ever publishes the first key of a new
        vault, or re-publishes the key that verifies the vault's manifests.
        """
        self.check_signer()
        if not isinstance(self._signer, PublishableSigner):
            return None
        text = self._signer.public_key_text()
        path = self._root / KEYS_DIR / f"{key_label(text, self._signer.key_id)}.pub"
        for label, existing in pinned_keys(self._root):
            if key_material(existing) == key_material(text):
                return self._root / KEYS_DIR / f"{label}.pub"
        path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(FileExistsError):  # a concurrent seal published the same key
            atomic_write_bytes(path, text.encode(), retry=self._retry)
        return path

    def _mhash(self, lib: Path) -> str:
        return blake3.blake3((lib / MANIFEST_NAME).read_bytes()).hexdigest()

    def _log(self, kind: str, subject: str, data: Mapping[str, object]) -> None:
        if self._ledger is not None:
            self._ledger.append(kind, subject, data)

    @contextlib.contextmanager
    def exclusive_generation(self, source_id: str, generation_id: str) -> Generator[Path]:
        """Hold a generation's exclusive lock (after the key pin check); yields its library path.

        For mutators of sealed generations outside this class: the repair ladder, parity, attestations.
        """
        self.check_signer()
        with self._locks.generation(source_id, generation_id, exclusive=True):
            yield self.library_path(source_id, generation_id)


def _check_source(source_id: str) -> None:
    if not _NAME_RE.match(source_id):
        raise VaultError(f"invalid source id: {source_id!r}")


def _check_generation(generation_id: str) -> None:
    if not _GENERATION_RE.match(generation_id):
        raise VaultError(f"invalid generation id: {generation_id!r}")


def _require_regular(path: Path) -> None:
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise VaultError(f"{path} is not a regular file (symlinks are not followed)")


def _read_staging(staging: Path) -> dict[str, ManifestEntry]:
    path = staging / STAGING_RECORD
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}
    raw = cast(dict[str, Any], json.loads(data))
    out = {rel: ManifestEntry.from_json(cast(dict[str, Any], obj)) for rel, obj in raw.items()}
    for rel, entry in out.items():
        if rel != entry.path or entry.role != ROLE_DATA:
            raise VaultError(f"{path}: malformed staging record entry {rel!r}")
    return out
