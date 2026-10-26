"""Tests for TLS certificate provisioning (``provision_tls_cert``).

The Tailscale CLI is mocked throughout; no network or Tailscale install is
needed.
"""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from nomothetic.api import create_self_signed_cert, provision_tls_cert

FQDN = "nomon-host.tailnet.ts.net"


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _write_ca_issued_cert(cert_path: Path, key_path: Path, days: int = 60) -> None:
    """Write a leaf cert signed by a throwaway CA (issuer != subject)."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(FQDN))
        .issuer_name(_name("Test CA"))
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days))
        .sign(ca_key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def _tailscale(cert_returncode: int):
    """Build a ``subprocess.run`` stand-in for ``tailscale status`` / ``cert``.

    On a successful ``tailscale cert`` it writes a CA-issued pair to the
    requested paths, as the real CLI does.
    """

    def run(args, **_kwargs):
        if args[:2] == ["tailscale", "status"]:
            out = json.dumps({"Self": {"DNSName": FQDN + "."}})
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
        if args[:2] == ["tailscale", "cert"]:
            if cert_returncode == 0:
                cert_file = Path(args[args.index("--cert-file") + 1])
                key_file = Path(args[args.index("--key-file") + 1])
                _write_ca_issued_cert(cert_file, key_file)
            return subprocess.CompletedProcess(args, cert_returncode, stdout="", stderr="denied")
        raise AssertionError(f"unexpected command: {args}")

    return run


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "cert.pem", tmp_path / "key.pem"


class TestProvisionTlsCert:
    def test_reuses_valid_ca_issued_cert_without_calling_tailscale(self, paths):
        cert_path, key_path = paths
        _write_ca_issued_cert(cert_path, key_path)
        before = cert_path.read_bytes()

        with patch("nomothetic.api.subprocess.run") as run:
            assert provision_tls_cert(cert_path, key_path) == "existing"

        run.assert_not_called()
        assert cert_path.read_bytes() == before

    def test_self_signed_cert_is_upgraded_when_tailscale_succeeds(self, paths):
        cert_path, key_path = paths
        create_self_signed_cert(cert_path, key_path)

        with patch("nomothetic.api.subprocess.run", side_effect=_tailscale(0)):
            assert provision_tls_cert(cert_path, key_path) == "tailscale"

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        assert cert.issuer != cert.subject

    def test_self_signed_cert_is_kept_unchanged_when_tailscale_fails(self, paths):
        cert_path, key_path = paths
        create_self_signed_cert(cert_path, key_path)
        cert_before, key_before = cert_path.read_bytes(), key_path.read_bytes()

        with patch("nomothetic.api.subprocess.run", side_effect=_tailscale(1)):
            assert provision_tls_cert(cert_path, key_path) == "self-signed"

        assert cert_path.read_bytes() == cert_before
        assert key_path.read_bytes() == key_before

    def test_generates_self_signed_cert_when_none_exists_and_no_tailscale(self, paths):
        cert_path, key_path = paths

        with patch("nomothetic.api.subprocess.run", side_effect=FileNotFoundError):
            assert provision_tls_cert(cert_path, key_path) == "self-signed"

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        assert cert.issuer == cert.subject
        assert key_path.exists()
