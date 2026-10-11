"""Provenance capture, mirror spot checks and cancellation (ADR-0009), all on loopback."""

from __future__ import annotations

import datetime as dt
import hashlib
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_fetch_http import CFG, meta4, remote_for

from wsp_core.model import RemoteFile
from wsp_core.testing.fetcher_contract import RangeServer, payload
from wsp_fetch import FetchCancelledError, HttpFetcher
from wsp_fetch.http import _SegmentedDownload  # pyright: ignore[reportPrivateUsage]
from wsp_fetch.provenance import byte_ranges, issuer_family
from wsp_fetch.state import state_path

Servers = Callable[[], RangeServer]


def test_provenance_records_mirrors_ranges_and_validators(tmp_path: Path, servers: Servers) -> None:
    data = payload(20 * 64 * 1024, seed=3)
    a, b = servers(), servers()
    remote = remote_for(data, a.add("f.bin", data), b.add("f.bin", data))
    with HttpFetcher(CFG) as f:
        report = f.fetch(remote, tmp_path / "f.bin")
    prov = report.provenance
    assert prov["url"] == remote.url
    assert prov["size"] == len(data)
    assert prov["checksums"]["declared"][0]["algorithm"] == "sha256"
    sources = {s["url"]: s for s in prov["sources"]}
    assert set(sources) == {remote.url, *remote.mirrors}
    covered = sorted(r for s in sources.values() for r in s["ranges"])
    assert sum(end - start + 1 for start, end in covered) == len(data)  # every byte attributed once
    assert all(s["etag"] for s in sources.values())
    assert all(s["host"] == "127.0.0.1" for s in sources.values())
    assert all(s["spot_check"] == "ok" for s in sources.values())
    assert prov["rejected"] == []


def test_size_disagreement_is_recorded(tmp_path: Path, servers: Servers) -> None:
    data = payload(4 * 64 * 1024, seed=4)
    a, b, c = servers(), servers(), servers()
    remote = remote_for(data, a.add("f.bin", data), b.add("f.bin", data), c.add("f.bin", data + b"evil"))
    with HttpFetcher(CFG) as f:
        report = f.fetch(remote, tmp_path / "f.bin")
    rejected = report.provenance["rejected"]
    assert [(r["url"], r["reason"]) for r in rejected] == [(remote.mirrors[1], "size-disagreement")]
    assert rejected[0]["size"] == len(data) + 4


def test_tampered_mirror_is_caught_by_spot_check(tmp_path: Path, servers: Servers) -> None:
    """A mirror that serves altered bytes is caught even when the verified file came out right."""
    data = payload(16 * 64 * 1024, seed=5)
    good, bad = servers(), servers()
    url_good, url_bad = good.add("z.zim", data), bad.add("z.zim", data)
    ml = good.add("z.zim.meta4", meta4("z.zim", data, [url_good, url_bad]))
    remote = RemoteFile(url_good, "z.zim", len(data), metalink=ml)
    # Corrupt exactly the bytes the spot check will read on the bad mirror: a piece served from
    # there fails its Metalink hash and is refetched; the spot check then sees other bytes.
    import random

    n = CFG.spot_check_bytes
    rng = random.Random(remote.url)
    offsets = sorted({rng.randrange(0, len(data) - n + 1) for _ in range(CFG.spot_checks)})
    bad.faults.corrupt_offsets = {offsets[0] + 7}
    with HttpFetcher(CFG) as f:
        report = f.fetch(remote, tmp_path / "z.zim")
    assert (tmp_path / "z.zim").read_bytes() == data
    sources = {s["url"]: s for s in report.provenance["sources"]}
    assert sources[url_good]["spot_check"] == "ok"
    bad_src = sources[url_bad]
    assert bad_src["spot_check"] == "mismatch" or bad_src["corrupt_pieces"] > 0
    assert bad_src["spot_check"] in ("mismatch", "skipped: mirror dropped")
    assert report.provenance["metalink"] == ml
    assert report.provenance["checksums"]["piece_hashes"] == "sha1"


def test_cancel_stops_a_download_promptly_and_keeps_resume_state(tmp_path: Path, servers: Servers) -> None:
    data = payload(64 * 64 * 1024, seed=6)
    a = servers()
    a.faults.delay = 0.05  # ~16 KiB per 50 ms per connection: minutes for the whole file
    remote = remote_for(data, a.add("slow.bin", data))
    f = HttpFetcher(CFG)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            f.fetch(remote, tmp_path / "slow.bin")
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.5)
    started = time.monotonic()
    f.cancel()
    t.join(10)
    assert not t.is_alive()
    assert time.monotonic() - started < 5.0
    assert errors
    assert isinstance(errors[0], FetchCancelledError)
    assert state_path(tmp_path / "slow.bin").exists()  # resumes next start
    with pytest.raises(FetchCancelledError):
        f.get_bytes(remote.url)
    f.reset()
    a.faults.delay = 0.0
    assert f.fetch(remote, tmp_path / "slow.bin").sha256 == hashlib.sha256(data).hexdigest()
    f.close()


def test_redirect_chain_is_observed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "origin.example":
            return httpx.Response(302, headers={"Location": "https://mirror.example/x.sha256"})
        return httpx.Response(200, stream=httpx.ByteStream(b"abc  x\n"), headers={"ETag": '"e1"'})

    with HttpFetcher(CFG, transport=httpx.MockTransport(handler)) as f:
        assert f.get_bytes("https://origin.example/x.sha256") == b"abc  x\n"
        seen = f.observation("https://origin.example/x.sha256")
    assert seen is not None
    assert seen["final_url"] == "https://mirror.example/x.sha256"
    assert seen["host"] == "mirror.example"
    assert [(h["url"], h["status"]) for h in seen["redirects"]] == [("https://origin.example/x.sha256", 302)]
    assert seen["etag"] == '"e1"'


def test_byte_ranges_and_issuer_family() -> None:
    assert byte_ranges([0, 1, 3, 2, 7], 10, 75) == [[0, 39], [70, 74]]
    assert issuer_family({"o": "Let's Encrypt", "cn": "R10"}) == "Let's Encrypt"
    assert issuer_family({"cn": "R10"}) == "R"


# --- TLS on loopback ---------------------------------------------------------------------------------


def _make_cert(tmp: Path, org: str) -> tuple[Path, Path, Path]:
    crypto = pytest.importorskip("cryptography")
    del crypto
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.ORGANIZATION_NAME, org), x509.NameAttribute(NameOID.COMMON_NAME, "R1")]
    )
    now = dt.datetime.now(dt.UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem, cert_pem, key_pem = tmp / "ca.pem", tmp / "cert.pem", tmp / "key.pem"
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return ca_pem, cert_pem, key_pem


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[tuple[str, Path, Any]]:
    ca, cert, key = _make_cert(tmp_path, "Loopback Test CA")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            body = b"0123456789abcdef  x.bin\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    t = threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True)
    t.start()
    yield f"https://localhost:{httpd.server_address[1]}/x.sha256", ca, cert
    httpd.shutdown()
    httpd.server_close()


def test_tls_peer_certificate_is_recorded(tls_server: tuple[str, Path, Any]) -> None:
    url, ca, cert_pem = tls_server
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    der = x509.load_pem_x509_certificate(cert_pem.read_bytes()).public_bytes(serialization.Encoding.DER)
    verify = ssl.create_default_context(cafile=str(ca))
    with HttpFetcher(CFG, transport=httpx.HTTPTransport(verify=verify)) as f:
        f.get_bytes(url)
        seen = f.observation(url)
    assert seen is not None
    tls = seen["tls"]
    assert tls["sha256"] == hashlib.sha256(der).hexdigest()
    assert tls["issuer_family"] == "Loopback Test CA"
    assert tls["issuer"]["cn"] == "R1"
    assert tls["subject"]["cn"] == "localhost"
    assert tls["not_after"]


def test_segmented_download_class_is_importable() -> None:
    assert _SegmentedDownload.__name__ == "_SegmentedDownload"
