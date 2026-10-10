import shutil
import subprocess
from pathlib import Path

import pytest

from wsp_core.ports import Signer
from wsp_vault.signing import Ed25519Signer, PublishableSigner, SigningError, SshSigner

PAYLOAD = b'{"files": []}\n'


def test_ed25519_round_trip_and_tamper() -> None:
    signer = Ed25519Signer.generate()
    assert isinstance(signer, Signer)
    assert isinstance(signer, PublishableSigner)
    sig = signer.sign(PAYLOAD)
    assert signer.verify(PAYLOAD, sig)
    assert not signer.verify(PAYLOAD + b" ", sig)
    assert not signer.verify(PAYLOAD, sig[:-5] + b"AAAA\n")
    assert not signer.verify(PAYLOAD, b"garbage")
    assert not Ed25519Signer.generate().verify(PAYLOAD, sig)


def test_ed25519_namespace_separates_domains() -> None:
    signer = Ed25519Signer.generate(namespace="a")
    other = Ed25519Signer(signer._public, namespace="b")  # pyright: ignore[reportPrivateUsage]
    assert not other.verify(PAYLOAD, signer.sign(PAYLOAD))


def test_ed25519_verifier_from_published_key_and_pem_reload() -> None:
    signer = Ed25519Signer.generate(key_id="test")
    sig = signer.sign(PAYLOAD)
    verifier = Ed25519Signer.verifier(signer.public_key_text())
    assert verifier.verify(PAYLOAD, sig)
    with pytest.raises(SigningError):
        verifier.sign(PAYLOAD)
    reloaded = Ed25519Signer.from_private_pem(signer.private_pem())
    assert signer.verify(PAYLOAD, reloaded.sign(PAYLOAD))


@pytest.fixture
def ssh_key(tmp_path: Path) -> Path:
    exe = shutil.which("ssh-keygen")
    if exe is None:
        pytest.skip("ssh-keygen not installed")
    key = tmp_path / "keys" / "id_ed25519"
    key.parent.mkdir()
    subprocess.run([exe, "-q", "-t", "ed25519", "-N", "", "-C", "whisipedia", "-f", str(key)], check=True)
    return key


def test_ssh_signer_round_trip(ssh_key: Path) -> None:
    signer = SshSigner(ssh_key)
    sig = signer.sign(PAYLOAD)
    assert sig.startswith(b"-----BEGIN SSH SIGNATURE-----")
    assert signer.verify(PAYLOAD, sig)
    assert not signer.verify(PAYLOAD + b"x", sig)
    assert not signer.verify(PAYLOAD, b"not a signature")
    assert signer.public_key_text().startswith("ssh-ed25519 ")


def test_ssh_signer_namespace_is_enforced(ssh_key: Path) -> None:
    sig = SshSigner(ssh_key, namespace="other").sign(PAYLOAD)
    assert not SshSigner(ssh_key).verify(PAYLOAD, sig)


def test_ssh_signer_reports_failure(tmp_path: Path) -> None:
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen not installed")
    with pytest.raises(SigningError):
        SshSigner(tmp_path / "missing").sign(PAYLOAD)
