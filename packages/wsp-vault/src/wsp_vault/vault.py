"""``FileVault``: the reference :class:`wsp_core.ports.Vault` on a plain directory tree (PLAN §7).

Layout under ``root``::

    library/<domain>/<source_id>/CURRENT.txt          pointer file (generation id + LF)
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

Rules: all writes go through :mod:`wsp_vault.atomic`; nothing is ever deleted except our own
``*.partial`` temporaries and the staging record; sealed files are chmod read-only (a no-op on SMB
shares that ignore modes, which is tolerated). ``prune`` (retention) moves a generation under
``pruned/``; emptying that folder is a deliberate owner action, never automatic.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import blake3

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
from wsp_core.ports import Ledger, ParityProvider, Signer
from wsp_vault.atomic import (
    DEFAULT_RETRY,
    PARTIAL_SUFFIX,
    IntegrityError,
    RetryPolicy,
    atomic_copy,
    atomic_write_bytes,
    fsync_dir,
    readback,
)
from wsp_vault.hashing import Digests, hash_file, supported_extra
from wsp_vault.manifest import (
    DEGRADED_NAME,
    MANIFEST_NAME,
    PARITY_DIR,
    ROLE_PARITY,
    SIGNATURE_NAME,
    STAGING_RECORD,
    Manifest,
    ManifestEntry,
    ManifestError,
    check_relpath,
)
from wsp_vault.signing import PublishableSigner

LIBRARY_DIR = "library"
STAGING_DIR = "staging"
QUARANTINE_DIR = "quarantine"
PRUNED_DIR = "pruned"
KEYS_DIR = "keys"
CURRENT_NAME = "CURRENT.txt"
READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_GENERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class VaultError(Exception):
    pass


class SignatureError(VaultError):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def domains_from_specs(specs: Iterable[SourceSpec]) -> dict[str, str]:
    """``source_id -> domain`` from the registry (:attr:`SourceSpec.vault_domain`, ADR-0005)."""
    return {s.id: s.vault_domain for s in specs}


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
    """Raise :class:`IntegrityError` if any upstream checksum we could compute disagrees."""
    ours = {"sha256": digests.sha256, "blake3": digests.blake3, **digests.extra}
    for c in upstream:
        got = ours.get(c.algorithm.lower())
        if got is not None and got != c.hexdigest.lower():
            raise IntegrityError(f"upstream {c.algorithm} {c.hexdigest} != computed {got}")


def make_read_only(path: Path) -> None:
    with suppress(OSError):
        os.chmod(path, READ_ONLY)


class FileVault:
    """See the module docstring for layout and guarantees."""

    def __init__(
        self,
        root: Path,
        signer: Signer,
        *,
        domains: Mapping[str, str] | None = None,
        default_domain: str = "misc",
        ledger: Ledger | None = None,
        parity: ParityProvider | None = None,
        retry: RetryPolicy = DEFAULT_RETRY,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._root = root
        self._signer = signer
        self._domains = dict(domains or {})
        self._default_domain = default_domain
        self._ledger = ledger
        self._parity = parity
        self._retry = retry
        self._clock = clock
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
        """Copy ``local`` into staging (hash, fsync, read back, rename). Idempotent for identical bytes.

        ``upstream`` checksums are verified when hashlib can compute them and recorded either way.
        ``expected`` (the fetcher's digests) must match the read-back; it also spares re-hashing
        ``local`` when the file turns out to be staged already.
        """
        rel = check_relpath(relpath)
        staging = self.stage(source_id, generation_id)
        _require_regular(local)
        dest = staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        extra = supported_extra(c.algorithm for c in upstream)

        def validate(d: Digests) -> None:
            check_expected(d, expected)
            check_upstream(d, upstream)

        if dest.exists():
            have = self._retry.run(lambda: readback(dest, extra=extra))
            if expected is None:
                new = self._retry.run(lambda: hash_file(local, extra=extra))
                if have != new:
                    raise VaultError(f"{relpath} is already staged with different content")
            else:
                try:
                    check_expected(have, expected)
                except IntegrityError as exc:
                    raise VaultError(f"{relpath} is already staged with different content") from exc
            check_upstream(have, upstream)
            digests = have
        else:
            digests = atomic_copy(local, dest, extra=extra, validate=validate, retry=self._retry)
        return self._record(source_id, generation_id, staging, rel.as_posix(), digests, upstream, "ingest")

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

        The file is hashed in place and checked against ``expected`` and ``upstream``; only then is
        it renamed, so no bytes are copied. Resumes cleanly if a crash happened after the rename.
        """
        staging = self.stage(source_id, generation_id)
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
        """Hash ``path`` in place, check it, rename it to ``staging/relpath``. No staging record."""
        rel = check_relpath(relpath)
        dest = staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        extra = supported_extra(c.algorithm for c in upstream)

        def validate(d: Digests) -> None:
            if expected is not None and d != expected:
                raise IntegrityError(f"{path}: {d.sha256} does not match expected {expected.sha256}")
            check_upstream(d, upstream)

        if not path.exists() and dest.exists():
            digests = self._retry.run(lambda: readback(dest, extra=extra))
            validate(digests)
        else:
            _require_regular(path)
            if dest.exists():
                raise VaultError(f"{relpath} is already staged; refusing to replace it")
            if os.stat(dest.parent).st_dev != os.stat(path).st_dev:
                raise VaultError(f"{path} is on a different filesystem; use ingest() to copy it")
            digests = self._retry.run(lambda: readback(path, extra=extra))
            validate(digests)
            os.rename(path, dest)
            fsync_dir(dest.parent)
            fsync_dir(path.parent)
            if dest.stat().st_size != digests.size:
                raise VaultError(f"{dest}: size changed during adoption")
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
        file, which is quadratic), with one ``vault.adopt_batch`` ledger event per flush. A crash
        between a rename and the next flush is recovered on re-run: the staged file is re-hashed.
        Returns the number of files adopted. Stops at the first failing file (already adopted
        files stay recorded).
        """
        staging = self.stage(source_id, generation_id)
        records = _read_staging(staging)
        pending: list[tuple[str, Digests, Sequence[Checksum]]] = []
        count = 0

        def flush() -> None:
            if not pending:
                return
            for rel, digests, upstream in pending:
                records[rel] = ManifestEntry.from_digests(rel, digests, upstream=upstream)
            payload = {p: e.to_json() for p, e in sorted(records.items())}
            atomic_write_bytes(
                staging / STAGING_RECORD,
                json.dumps(payload, sort_keys=True, indent=1).encode(),
                overwrite=True,
                retry=self._retry,
            )
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
        records = _read_staging(staging)
        previous = records.get(relpath)
        if previous is not None and previous.digests == digests:
            upstream = [*previous.upstream, *upstream]
        entry = ManifestEntry.from_digests(relpath, digests, upstream=upstream)
        records[relpath] = entry
        payload = {p: e.to_json() for p, e in sorted(records.items())}
        atomic_write_bytes(
            staging / STAGING_RECORD,
            json.dumps(payload, sort_keys=True, indent=1).encode(),
            overwrite=True,
            retry=self._retry,
        )
        self._log(
            f"vault.{how}",
            f"{source_id}/{generation_id}",
            {"path": relpath, "size": digests.size, "sha256": digests.sha256, "blake3": digests.blake3},
        )
        return entry.to_artifact(source_id, generation_id)

    # --- sealing and promotion -------------------------------------------------------------------

    def seal(self, source_id: str, generation_id: str, *, parity_percent: int | None = None) -> Generation:
        """Write and sign ``MANIFEST.json``, move staging into ``library/``, make files read-only.

        Idempotent: re-running after a crash at any step completes the seal. With ``parity_percent``
        and a configured parity provider, parity is created first and listed in the manifest.
        """
        lib = self.library_path(source_id, generation_id)
        stg = self.staging_path(source_id, generation_id)
        if not (lib / MANIFEST_NAME).exists():
            if not stg.is_dir():
                raise VaultError(f"{source_id}/{generation_id}: nothing staged")
            if not self._staged_manifest_ok(stg):
                self._write_manifest(source_id, generation_id, stg, parity_percent)
            lib.parent.mkdir(parents=True, exist_ok=True)
            if lib.exists():
                raise VaultError(f"{lib} exists without a manifest; inspect it by hand")
            _rename_with_retry(stg, lib)
            fsync_dir(lib.parent)
            for parent in (stg.parent, stg.parent.parent):
                with suppress(OSError):
                    parent.rmdir()
        manifest = self.manifest(source_id, generation_id)
        for path in [lib / MANIFEST_NAME, lib / SIGNATURE_NAME, *(lib / e.path for e in manifest.entries)]:
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

    def _staged_manifest_ok(self, stg: Path) -> bool:
        m, s = stg / MANIFEST_NAME, stg / SIGNATURE_NAME
        return m.exists() and s.exists() and self._signer.verify(m.read_bytes(), s.read_bytes())

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
                path.unlink()
                continue
            on_disk.add(rel)
        if not records:
            raise VaultError(f"{source_id}/{generation_id}: nothing staged")
        if on_disk != set(records):
            raise VaultError(
                f"staging does not match ingest record: unrecorded={sorted(on_disk - set(records))} "
                f"missing={sorted(set(records) - on_disk)}"
            )
        for rel, entry in records.items():
            if (stg / rel).stat().st_size != entry.size:
                raise VaultError(f"{rel}: size changed since ingest")
        entries = list(records.values())
        if parity_percent and self._parity is not None:
            out = stg / PARITY_DIR
            out.mkdir(exist_ok=True)
            created = self._parity.create([stg / e.path for e in entries], out, parity_percent)
            for path in created:
                rel = path.relative_to(stg).as_posix()
                digests = self._retry.run(lambda p=path: readback(p))
                entries.append(ManifestEntry.from_digests(rel, digests, role=ROLE_PARITY))
        manifest = Manifest(source_id, generation_id, self._clock(), tuple(entries))
        data = manifest.to_bytes()
        for name in (MANIFEST_NAME, SIGNATURE_NAME):
            with suppress(FileNotFoundError):
                (stg / name).unlink()
        atomic_write_bytes(stg / MANIFEST_NAME, data, retry=self._retry)
        atomic_write_bytes(stg / SIGNATURE_NAME, self._signer.sign(data), retry=self._retry)
        (stg / STAGING_RECORD).unlink()

    def promote(self, source_id: str, generation_id: str) -> None:
        """Atomically rewrite ``CURRENT.txt``. Only a sealed, validly signed, non-degraded generation."""
        lib = self.library_path(source_id, generation_id)
        self.manifest(source_id, generation_id)
        if (lib / DEGRADED_NAME).exists():
            raise VaultError(f"{source_id}/{generation_id} is DEGRADED")
        previous = self._read_current(source_id)
        atomic_write_bytes(
            self.source_dir(source_id) / CURRENT_NAME,
            f"{generation_id}\n".encode(),
            overwrite=True,
            retry=self._retry,
        )
        self._log("vault.promote", f"{source_id}/{generation_id}", {"previous": previous})

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

    def manifest(self, source_id: str, generation_id: str) -> Manifest:
        """The sealed manifest, after checking its signature. Raises :class:`SignatureError`."""
        lib = self.library_path(source_id, generation_id)
        try:
            data = (lib / MANIFEST_NAME).read_bytes()
            sig = (lib / SIGNATURE_NAME).read_bytes()
        except FileNotFoundError as exc:
            raise SignatureError(f"{source_id}/{generation_id}: {exc.filename} missing") from exc
        if not self._signer.verify(data, sig):
            raise SignatureError(f"{source_id}/{generation_id}: manifest signature invalid")
        try:
            manifest = Manifest.from_bytes(data)
        except ManifestError as exc:
            raise SignatureError(f"{source_id}/{generation_id}: {exc}") from exc
        if (manifest.source_id, manifest.generation_id) != (source_id, generation_id):
            raise SignatureError(f"{source_id}/{generation_id}: manifest names another generation")
        return manifest

    def verify_manifest(self, source_id: str, generation_id: str) -> Verdict:
        try:
            self.manifest(source_id, generation_id)
        except SignatureError as exc:
            return Verdict(VerdictStatus.MISMATCH, str(exc))
        return Verdict(VerdictStatus.OK)

    def _generation(
        self, source_id: str, generation_id: str, lib: Path, manifest: Manifest | None = None
    ) -> Generation:
        """Unverified view of a sealed generation. An unparseable manifest is reported as DEGRADED."""
        if manifest is None:
            try:
                manifest = Manifest.from_bytes((lib / MANIFEST_NAME).read_bytes())
            except ManifestError:
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

    def prune(self, source_id: str, generation_id: str) -> Path:
        """Retire a sealed generation (retention, PLAN §6.4): move it under ``pruned/``. Never deletes.

        Refuses the current generation and the newest sealed one, so a source always keeps data.
        """
        subject = f"{source_id}/{generation_id}"
        lib = self.library_path(source_id, generation_id)
        if not (lib / MANIFEST_NAME).exists():
            raise VaultError(f"{subject} is not a sealed generation")
        if self._read_current(source_id) == generation_id:
            raise VaultError(f"refusing to prune the current generation {subject}")
        sealed = [
            g.generation_id for g in self.generations(source_id) if g.status is not GenerationStatus.STAGING
        ]
        if sealed and sealed[-1] == generation_id:
            raise VaultError(f"refusing to prune the newest sealed generation {subject}")
        stamp = self._clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        dest = self._root / PRUNED_DIR / self.domain_of(source_id) / source_id / f"{generation_id}.{stamp}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(lib, dest)
        fsync_dir(lib.parent)
        fsync_dir(dest.parent)
        self._log("vault.prune", subject, {"moved_to": dest.relative_to(self._root).as_posix()})
        return dest

    def quarantine(self, path: Path) -> Path:
        """Move a damaged file under ``quarantine/`` (same filesystem rename; never deletes)."""
        stamp = self._clock().astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        rel = path.relative_to(self._root)
        dest = self._root / QUARANTINE_DIR / rel.parent / f"{rel.name}.{stamp}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(path, dest)
        fsync_dir(path.parent)
        return dest

    def restore_file(self, source_id: str, generation_id: str, entry: ManifestEntry, src: Path) -> None:
        """Replace one file of a sealed generation with a verified copy of ``src``.

        The copy is verified against the manifest before the damaged file (if any) is quarantined.
        """
        lib = self.library_path(source_id, generation_id)
        dest = lib / entry.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        staged = dest.with_name(dest.name + ".repair")
        with suppress(FileNotFoundError):
            staged.unlink()
        atomic_copy(src, staged, expected=entry.digests, retry=self._retry)
        if dest.exists():
            self.quarantine(dest)
        os.rename(staged, dest)
        fsync_dir(dest.parent)
        make_read_only(dest)

    def publish_key(self) -> Path | None:
        """Write the signer's public key to ``keys/<key_id>-<fingerprint>.pub`` if not already there."""
        if not isinstance(self._signer, PublishableSigner):
            return None
        text = self._signer.public_key_text()
        fingerprint = blake3.blake3(text.encode()).hexdigest()[:12]
        path = self._root / KEYS_DIR / f"{self._signer.key_id}-{fingerprint}.pub"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(path, text.encode(), retry=self._retry)
        return path

    def _mhash(self, lib: Path) -> str:
        return blake3.blake3((lib / MANIFEST_NAME).read_bytes()).hexdigest()

    def _log(self, kind: str, subject: str, data: Mapping[str, object]) -> None:
        if self._ledger is not None:
            self._ledger.append(kind, subject, data)


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
    if not path.exists():
        return {}
    raw = cast(dict[str, Any], json.loads(path.read_bytes()))
    return {rel: ManifestEntry.from_json(cast(dict[str, Any], obj)) for rel, obj in raw.items()}


def _rename_with_retry(src: Path, dst: Path, *, attempts: int = 6, first_delay: float = 2.0) -> None:
    """``os.rename`` that waits out transient sharing locks.

    On SMB a directory cannot be renamed while any client holds a file inside it open (another
    reader, an indexer or virus scanner on the NAS). Such locks are usually short, so retry with
    exponential backoff (about two minutes in total) before giving up; the seal is idempotent and
    the next run retries anyway.
    """
    import time

    delay = first_delay
    for attempt in range(attempts):
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2
