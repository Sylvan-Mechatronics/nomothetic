"""Per-device asymmetric identity for fleet registration proofs.

Each device holds an Ed25519 private key generated on first boot
(``/var/lib/nomon/device_identity.key``, ``0600``). ``GET
/api/device/auth/identity`` returns a **registration proof** signed with that
key (a JWT with ``alg=EdDSA``) plus the matching public key. The central fleet
API verifies the signature against the public key and **pins** the key to the
VIN on first registration, so a later registration of the same VIN must carry a
proof signed by the same device — an attacker who merely guesses a VIN cannot
claim a device that is already registered, and cannot forge a proof for an
unregistered one without the device (review finding S-3; supersedes the
unverifiable HS256 proof from ``AuthService.create_registration_proof``).

Only the private key touches disk. If the key file cannot be written (missing
state directory on a dev machine) an in-memory key is used for the process
lifetime, exactly like :class:`~nomothetic.device_jwt.DeviceJwtSecretStore`.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

try:
    from authlib.jose import JsonWebToken

    _eddsa_jwt: Any = JsonWebToken(["EdDSA"])
except ImportError:  # pragma: no cover
    _eddsa_jwt = None

logger = logging.getLogger(__name__)

_DEFAULT_KEY_PATH = "/var/lib/nomon/device_identity.key"
PROOF_ISSUER = "nomon-device"
PROOF_AUDIENCE = "nomon-fleet"


def get_device_identity_path() -> str:
    """Return the device identity key path (``NOMON_DEVICE_IDENTITY_PATH`` or default)."""
    return os.environ.get("NOMON_DEVICE_IDENTITY_PATH", _DEFAULT_KEY_PATH)


class DeviceIdentity:
    """Load-or-generate the device's Ed25519 identity key and sign proofs with it.

    Parameters
    ----------
    path : str, optional
        Private key PEM path. Defaults to ``$NOMON_DEVICE_IDENTITY_PATH`` or
        ``/var/lib/nomon/device_identity.key``.
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path or get_device_identity_path()
        self._key = self._load_or_generate()

    # -- key management -------------------------------------------------------

    def _load_or_generate(self) -> Ed25519PrivateKey:
        try:
            with open(self._path, "rb") as fh:
                key = serialization.load_pem_private_key(fh.read(), password=None)
            if isinstance(key, Ed25519PrivateKey):
                logger.info("Device identity key loaded from %s", self._path)
                return key
            logger.error("%s is not an Ed25519 key; using an in-memory identity", self._path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.error(
                "Device identity key at %s could not be read (%s); using an in-memory "
                "identity for this run without overwriting the file",
                self._path,
                exc,
            )
            return Ed25519PrivateKey.generate()

        key = Ed25519PrivateKey.generate()
        self._write(key)
        return key

    def _write(self, key: Ed25519PrivateKey) -> None:
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        target_dir = os.path.dirname(self._path)
        if not os.path.isdir(target_dir):
            logger.warning(
                "Device identity directory %s does not exist; the identity key will not "
                "be persisted (registration proofs change on restart)",
                target_dir,
            )
            return
        fd, tmp = -1, ""
        try:
            fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".device_identity_")
            os.write(fd, pem)
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
            os.close(fd)
            fd = -1
            os.rename(tmp, self._path)
            tmp = ""
            logger.info("Device identity key written to %s", self._path)
        except OSError:
            logger.warning("Failed to write device identity key to %s", self._path, exc_info=True)
        finally:
            if fd >= 0:
                os.close(fd)
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # -- public API -----------------------------------------------------------

    @property
    def public_key_pem(self) -> str:
        """PEM (SubjectPublicKeyInfo) of the device public key."""
        return (
            self._key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("utf-8")
        )

    def create_registration_proof(self, vin: str, ttl_seconds: int = 300) -> str:
        """Return an ``EdDSA`` JWT binding *vin* to this device for *ttl_seconds*.

        Claims: ``iss=nomon-device``, ``sub=<vin>``, ``aud=nomon-fleet``,
        ``iat``, ``exp``, ``jti``.
        """
        if _eddsa_jwt is None:  # pragma: no cover
            raise RuntimeError("authlib is required for registration proofs")
        now = datetime.now(timezone.utc)
        payload = {
            "iss": PROOF_ISSUER,
            "sub": vin,
            "aud": PROOF_AUDIENCE,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
            "jti": str(uuid.uuid4()),
        }
        private_pem = self._key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        token = _eddsa_jwt.encode({"alg": "EdDSA"}, payload, private_pem)
        return token.decode("utf-8") if isinstance(token, bytes) else str(token)


def verify_registration_proof(proof: str, vin: str, public_key_pem: str) -> bool:
    """Return whether *proof* is a valid, unexpired EdDSA proof for *vin* under *public_key_pem*.

    Used by the central fleet API. Never raises: any malformed input is ``False``.
    """
    if _eddsa_jwt is None:  # pragma: no cover
        return False
    try:
        public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        claims = _eddsa_jwt.decode(
            proof,
            public_key,
            claims_options={
                "iss": {"essential": True, "value": PROOF_ISSUER},
                "aud": {"essential": True, "value": PROOF_AUDIENCE},
                "sub": {"essential": True, "value": vin},
                "exp": {"essential": True},
            },
        )
        claims.validate()
        return True
    except Exception:  # noqa: BLE001 — any failure is "not verified"
        return False
