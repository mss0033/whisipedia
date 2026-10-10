"""Manifest and ledger signers (PLAN §7, §8.3), implementing :class:`wsp_core.ports.Signer`.

``SshSigner`` is the production path: ``ssh-keygen -Y sign/verify`` with an OpenSSH key and the
``whisipedia-manifest`` namespace, so signatures can be checked by anyone with stock OpenSSH.
``Ed25519Signer`` is a pure-Python fallback (``cryptography``) for tests and hosts without OpenSSH;
its signatures are domain-separated by the same namespace.

Both expose ``key_id`` and ``public_key_text()`` so the vault can publish the public key under
``<root>/keys/``. Private keys never go on the vault.
"""

from __future__ import annotations

import base64
import binascii
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from wsp_core.ports import Signer

NAMESPACE = "whisipedia-manifest"
_ED25519_HEADER = b"whisipedia-ed25519-signature-v1\n"


class SigningError(Exception):
    pass


@runtime_checkable
class PublishableSigner(Signer, Protocol):
    """A signer whose public key can be written to ``<root>/keys/``."""

    @property
    def key_id(self) -> str: ...

    def public_key_text(self) -> str: ...


class Ed25519Signer:
    """Pure-Python Ed25519 signer. Signature file format: a header line, then base64."""

    def __init__(
        self,
        public_key: Ed25519PublicKey,
        private_key: Ed25519PrivateKey | None = None,
        *,
        key_id: str = "whisipedia",
        namespace: str = NAMESPACE,
    ) -> None:
        self._public = public_key
        self._private = private_key
        self._key_id = key_id
        self._namespace = namespace.encode()

    @classmethod
    def generate(cls, *, key_id: str = "whisipedia", namespace: str = NAMESPACE) -> Ed25519Signer:
        private = Ed25519PrivateKey.generate()
        return cls(private.public_key(), private, key_id=key_id, namespace=namespace)

    @classmethod
    def from_private_pem(cls, pem: bytes, *, key_id: str = "whisipedia") -> Ed25519Signer:
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise SigningError("not an Ed25519 private key")
        return cls(key.public_key(), key, key_id=key_id)

    @classmethod
    def verifier(cls, public_key_text: str, *, key_id: str = "whisipedia") -> Ed25519Signer:
        """A verify-only instance from an OpenSSH public key line (as stored under ``keys/``)."""
        key = serialization.load_ssh_public_key(public_key_text.strip().encode())
        if not isinstance(key, Ed25519PublicKey):
            raise SigningError("not an Ed25519 public key")
        return cls(key, key_id=key_id)

    @property
    def key_id(self) -> str:
        return self._key_id

    def private_pem(self) -> bytes:
        if self._private is None:
            raise SigningError("verify-only signer")
        return self._private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )

    def public_key_text(self) -> str:
        line = self._public.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        return f"{line.decode()} {self._key_id}\n"

    def _message(self, payload: bytes) -> bytes:
        return self._namespace + b"\0" + payload

    def sign(self, payload: bytes) -> bytes:
        if self._private is None:
            raise SigningError("verify-only signer")
        return _ED25519_HEADER + base64.b64encode(self._private.sign(self._message(payload))) + b"\n"

    def verify(self, payload: bytes, signature: bytes) -> bool:
        if not signature.startswith(_ED25519_HEADER):
            return False
        try:
            raw = base64.b64decode(signature[len(_ED25519_HEADER) :].strip(), validate=True)
            self._public.verify(raw, self._message(payload))
        except (InvalidSignature, binascii.Error, ValueError):
            return False
        return True


class SshSigner:
    """``ssh-keygen -Y`` signer. The private key must be unencrypted or loaded in an agent."""

    def __init__(
        self,
        private_key: Path,
        *,
        public_key: Path | None = None,
        identity: str = "whisipedia",
        namespace: str = NAMESPACE,
        ssh_keygen: str | None = None,
    ) -> None:
        exe = ssh_keygen or shutil.which("ssh-keygen")
        if exe is None:
            raise SigningError("ssh-keygen not found")
        self._exe = exe
        self._private = private_key
        self._public = public_key or private_key.with_name(private_key.name + ".pub")
        self._identity = identity
        self._namespace = namespace

    @property
    def key_id(self) -> str:
        return self._identity

    def public_key_text(self) -> str:
        return self._public.read_text(encoding="utf-8").strip() + "\n"

    def _run(self, args: list[str], payload: bytes) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run([self._exe, *args], input=payload, capture_output=True, check=False)  # noqa: S603

    def sign(self, payload: bytes) -> bytes:
        proc = self._run(["-Y", "sign", "-q", "-f", str(self._private), "-n", self._namespace], payload)
        if proc.returncode != 0 or not proc.stdout:
            raise SigningError(f"ssh-keygen sign failed: {proc.stderr.decode(errors='replace').strip()}")
        return proc.stdout

    def verify(self, payload: bytes, signature: bytes) -> bool:
        key_fields = self.public_key_text().split()
        if len(key_fields) < 2:
            return False
        with tempfile.TemporaryDirectory(prefix="wsp-verify-") as tmp:
            allowed = Path(tmp) / "allowed_signers"
            allowed.write_text(f"{self._identity} {key_fields[0]} {key_fields[1]}\n", encoding="utf-8")
            sig = Path(tmp) / "payload.sig"
            sig.write_bytes(signature)
            args = ["-Y", "verify", "-f", str(allowed), "-I", self._identity, "-n", self._namespace]
            proc = self._run([*args, "-s", str(sig)], payload)
        return proc.returncode == 0
