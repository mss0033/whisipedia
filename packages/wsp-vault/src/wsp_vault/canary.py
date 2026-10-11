"""Mount canary: is the vault's share answering right now? (vault-fetch audit #2)

On a ``soft`` CIFS mount a server outage turns every read into ``EIO``/``EHOSTDOWN`` after the
mount's timeout. Without a canary a scrub slice running during an outage reported every file it
touched as damaged, and the repair ladder degraded generations and rolled ``CURRENT.txt`` back. The
scrubber now asks the canary before each slice and again whenever a read fails: if the canary fails
too, the slice stops and records nothing.

The probe is cheap and touches the server, not the client's caches: ``statvfs`` of the root (an SMB
QUERY_FS_INFO), a directory listing of the root, and, when present, a fresh read (page cache dropped)
of one small published key under ``keys/``, which must read back identically each time.
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

from wsp_vault.atomic import RetryPolicy, is_transient


class MountUnavailableError(OSError):
    """The vault's storage is not answering (or answers inconsistently); try again later."""


class MountCanary:
    def __init__(self, root: Path, *, retry: RetryPolicy | None = None) -> None:
        self._root = root
        self._retry = retry or RetryPolicy(attempts=3, base_delay=2.0, max_delay=10.0)
        self._key: Path | None = None
        self._key_bytes: bytes | None = None

    def _probe(self) -> None:
        os.statvfs(self._root)
        names = os.listdir(self._root)
        if self._key is None and "keys" in names:
            keys = sorted(p for p in (self._root / "keys").glob("*.pub") if p.is_file())
            self._key = keys[0] if keys else None
        if self._key is not None:
            fd = os.open(self._key, os.O_RDONLY | os.O_CLOEXEC)
            try:
                with suppress(OSError, AttributeError):
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                data = os.pread(fd, 1 << 16, 0)
            finally:
                os.close(fd)
            if self._key_bytes is None:
                self._key_bytes = data
            elif data != self._key_bytes:
                raise MountUnavailableError(f"canary {self._key} reads back differently")

    def __call__(self) -> None:
        """Raise :class:`MountUnavailableError` if the share does not answer after a few retries."""
        try:
            self._retry.run(self._probe)
        except MountUnavailableError:
            raise
        except OSError as exc:
            kind = "transient" if is_transient(exc) else "error"
            raise MountUnavailableError(exc.errno, f"vault storage unavailable ({kind}): {exc}") from exc
