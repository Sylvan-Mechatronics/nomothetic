"""Device pairing lifecycle management.

Manages the one-time pairing secret flow for device-mode authentication.
On first boot, a pairing secret is generated and logged to the console.
The device owner enters this secret via the nomotactic UI to claim the
device and receive JWT tokens.

A second, independent secret — the **Soft AP passphrase** — is written to its
own shared file so that nomopractic reads it as the WPA2 passphrase for the
Wi-Fi Soft AP (see nomopractic ADR-005). The two values are deliberately
different: the 8-digit pairing code is short so a person can type it into the
app, but an 8-digit WPA2 PSK can be recovered offline from a captured handshake
in minutes. The AP passphrase is therefore a long random string (~116 bits) that
the user enters once in their Wi-Fi settings, and network presence on the AP
subnet proves possession of *that* secret (review finding S-1, 2026-09-13).

See ADR-014 for design rationale.
"""

import grp
import hmac
import logging
import os
import secrets
import stat
import tempfile

from nomothetic.device_jwt import DeviceJwtSecretStore

logger = logging.getLogger(__name__)

_DEFAULT_PAIRING_SECRET_PATH = "/var/lib/nomon/pairing_secret"
_PAIRING_SECRET_DIGITS = 8

_DEFAULT_AP_PASSPHRASE_PATH = "/var/lib/nomon/ap_passphrase"
# 20 characters from a 57-symbol alphabet ≈ 116 bits — far beyond offline
# WPA2-PSK cracking. Ambiguous glyphs (0/O, 1/l/I) are omitted because the
# user types this once into a phone's Wi-Fi dialog.
_AP_PASSPHRASE_LENGTH = 20
_AP_PASSPHRASE_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# WPA2-PSK passphrase bounds (IEEE 802.11i): 8–63 printable ASCII characters.
_WPA2_MIN_LEN = 8
_WPA2_MAX_LEN = 63


def get_ap_passphrase_path() -> str:
    """Return the configured Soft AP passphrase file path.

    Reads from the ``NOMON_AP_PASSPHRASE_PATH`` environment variable, falling
    back to ``/var/lib/nomon/ap_passphrase``. nomopractic's ``ap-mode.sh``
    reads the same path (same env var, same default).

    Returns
    -------
    str
        Absolute path to the shared AP passphrase file.
    """
    return os.environ.get("NOMON_AP_PASSPHRASE_PATH", _DEFAULT_AP_PASSPHRASE_PATH)


def is_valid_ap_passphrase(value: str) -> bool:
    """Return whether *value* is a usable WPA2-PSK passphrase (8–63 printable ASCII).

    Parameters
    ----------
    value : str
        Candidate passphrase.
    """
    if not (_WPA2_MIN_LEN <= len(value) <= _WPA2_MAX_LEN):
        return False
    return all(32 <= ord(ch) <= 126 for ch in value)


def generate_ap_passphrase() -> str:
    """Return a fresh random Soft AP passphrase (see module docstring)."""
    return "".join(secrets.choice(_AP_PASSPHRASE_ALPHABET) for _ in range(_AP_PASSPHRASE_LENGTH))


def get_pairing_secret_path() -> str:
    """Return the configured pairing secret file path.

    Reads from the ``NOMON_PAIRING_SECRET_PATH`` environment variable,
    falling back to ``/var/lib/nomon/pairing_secret``.

    Returns
    -------
    str
        Absolute path to the shared pairing secret file.
    """
    return os.environ.get("NOMON_PAIRING_SECRET_PATH", _DEFAULT_PAIRING_SECRET_PATH)


def _read_secret_file(path: str) -> str | None:
    """Read a shared secret file, returning its stripped contents or ``None``.

    Parameters
    ----------
    path : str
        File to read.

    Returns
    -------
    str or None
        The non-empty value, or ``None`` if the file is absent, unreadable, or
        blank.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _read_shared_secret() -> str | None:
    """Read the pairing secret from the shared file, if present and valid.

    Returns the stripped secret string if the file exists and contains a
    non-empty value, otherwise returns ``None``.

    Returns
    -------
    str or None
        The pairing secret read from disk, or ``None`` if the file is absent
        or cannot be read.
    """
    return _read_secret_file(get_pairing_secret_path())


def _read_shared_ap_passphrase() -> str | None:
    """Read the Soft AP passphrase from its shared file, or ``None``."""
    return _read_secret_file(get_ap_passphrase_path())


def _secret_file_present(path: str) -> bool:
    """True when a non-empty pairing secret file exists (readable or not).

    ``os.path.getsize`` only needs directory permissions, so this detects an
    existing file even when its content cannot be read — the case where the
    file must never be overwritten.
    """
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return os.path.exists(path)


def _write_shared_secret(secret: str) -> None:
    """Write the pairing secret to its shared file (see :func:`_write_secret_file`).

    Parameters
    ----------
    secret : str
        The pairing secret to persist.
    """
    _write_secret_file(get_pairing_secret_path(), secret, "Pairing secret")


def _write_shared_ap_passphrase(passphrase: str) -> None:
    """Write the Soft AP passphrase to its shared file (see :func:`_write_secret_file`).

    Parameters
    ----------
    passphrase : str
        The WPA2 passphrase to persist for ``ap-mode.sh``.
    """
    _write_secret_file(get_ap_passphrase_path(), passphrase, "AP passphrase")


def _write_secret_file(path: str, secret: str, label: str) -> None:
    """Write a shared secret to *path* atomically.

    Uses a write-to-temp-then-rename pattern for atomicity.  Sets file
    mode ``0640`` and group ``nomon`` so that nomopractic can read it.

    If the target directory does not exist or permissions cannot be set,
    a warning is logged but no exception is raised — HTTP pairing still
    works; only the Wi-Fi Soft AP is affected.

    Parameters
    ----------
    path : str
        Destination file.
    secret : str
        The value to persist.
    label : str
        Human-readable name used in log messages.
    """
    target_dir = os.path.dirname(path)

    if not os.path.isdir(target_dir):
        logger.warning(
            "%s directory %s does not exist; Wi-Fi Soft AP will not work until it is created",
            label,
            target_dir,
        )
        return

    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=f".{os.path.basename(path)}_")
        os.write(fd, secret.encode("utf-8"))
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)  # 0o640
        os.close(fd)
        fd = None

        try:
            nomon_gid = grp.getgrnam("nomon").gr_gid
            os.chown(tmp_path, -1, nomon_gid)
        except (KeyError, PermissionError):
            logger.warning(
                "Could not set group 'nomon' on %s file; "
                "nomopractic may not be able to read it for the Wi-Fi Soft AP",
                label.lower(),
            )

        os.rename(tmp_path, path)
        logger.info("%s written to %s", label, path)
        tmp_path = None  # rename succeeded — don't clean up
    except OSError:
        logger.warning(
            "Failed to write %s to %s; Wi-Fi Soft AP may not be available",
            label.lower(),
            path,
            exc_info=True,
        )
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


class PairingState:
    """Manages device pairing lifecycle.

    On construction, a fresh JWT signing secret is generated.  The pairing
    secret is created on demand via :meth:`generate_secret` and consumed
    (single-use) via :meth:`verify_and_consume`.

    Attributes
    ----------
    secret : str or None
        Current pairing secret (None when consumed or not yet generated).
    paired : bool
        Whether the device has been successfully paired.
    owner_email : str or None
        Email of the paired owner (set externally after pairing).
    jwt_secret : str
        Random JWT signing key (regenerated on :meth:`reset`).
    """

    def __init__(self) -> None:
        self.secret: str | None = None
        self._last_secret: str | None = None
        self.paired: bool = False
        self.owner_email: str | None = None
        # Soft AP WPA2 passphrase — independent of the 8-digit pairing code
        # (see module docstring). Populated by load_or_generate_ap_passphrase().
        self.ap_passphrase: str | None = None
        # JWT signing secret is loaded from (or generated into) the persistent
        # store at /var/lib/nomon/device_jwt_secret so it survives AP → WiFi
        # mode switches and service restarts.  See DeviceJwtSecretStore (ADR-016).
        self.jwt_secret: str = DeviceJwtSecretStore().load_or_generate()

    def load_or_generate_secret(self) -> str:
        """Load an existing pairing secret or generate a new one.

        On **first boot** (no file on disk) or when the stored value is
        readable but invalid, delegates to :meth:`generate_secret` to create
        and persist a new secret.

        On **subsequent restarts** (valid 8-digit secret already on disk),
        loads that value without overwriting the file, so the WPA2 Soft AP
        passphrase stays stable across service restarts.

        When the file **exists but cannot be read** (permissions, transient
        I/O), it is never overwritten: the on-disk value doubles as the
        Soft AP passphrase and must stay constant unless intentionally reset.
        This session runs with an in-memory secret and the file is left for
        the next start.

        Returns
        -------
        str
            The active pairing secret.
        """
        existing = _read_shared_secret()
        if existing is not None and existing.isdigit() and len(existing) == _PAIRING_SECRET_DIGITS:
            self.secret = existing
            self._last_secret = existing
            self.paired = False
            logger.info(
                "Loaded existing pairing secret from %s",
                get_pairing_secret_path(),
            )
            return self.secret

        path = get_pairing_secret_path()
        if existing is None and _secret_file_present(path):
            logger.error(
                "Pairing secret file %s exists but could not be read; using an "
                "in-memory secret for this session without overwriting the file.",
                path,
            )
            return self._set_new_secret()

        logger.warning(
            "Generating a new pairing secret (%s)",
            "no stored secret" if existing is None else "stored value is not an 8-digit passkey",
        )
        return self.generate_secret()

    def _set_new_secret(self) -> str:
        """Set a fresh random 8-digit secret on this state (in memory only)."""
        passkey = secrets.randbelow(10**_PAIRING_SECRET_DIGITS)
        self.secret = f"{passkey:0{_PAIRING_SECRET_DIGITS}d}"
        self._last_secret = self.secret
        self.paired = False
        return self.secret

    def generate_secret(self) -> str:
        """Generate a pairing secret for device authentication.

        The secret is written to the shared pairing secret file so that
        nomopractic's Wi-Fi Soft AP (`scripts/ap-mode.sh`) can read it
        as the WPA2 hotspot passphrase (see nomopractic ADR-005).

        Returns
        -------
        str
            The pairing secret (8-digit zero-padded numeric string).
        """
        secret = self._set_new_secret()
        _write_shared_secret(secret)
        return secret

    def load_or_generate_ap_passphrase(self) -> str:
        """Load the Soft AP passphrase from its shared file or generate a new one.

        Mirrors :meth:`load_or_generate_secret` for the WPA2 passphrase:

        - a valid stored value (8–63 printable ASCII) is loaded and kept, so the
          hotspot passphrase is stable across restarts;
        - a file that exists but cannot be read is never overwritten (the
          hotspot keeps whatever it has); an in-memory value is used this run;
        - otherwise a fresh passphrase is generated and persisted.

        Returns
        -------
        str
            The active Soft AP passphrase.
        """
        path = get_ap_passphrase_path()
        existing = _read_shared_ap_passphrase()
        if existing is not None and is_valid_ap_passphrase(existing):
            self.ap_passphrase = existing
            logger.info("Loaded existing Soft AP passphrase from %s", path)
            return existing

        if existing is None and _secret_file_present(path):
            logger.error(
                "Soft AP passphrase file %s exists but could not be read; using an "
                "in-memory passphrase for this session without overwriting the file.",
                path,
            )
            self.ap_passphrase = generate_ap_passphrase()
            return self.ap_passphrase

        logger.warning(
            "Generating a new Soft AP passphrase (%s)",
            "no stored passphrase" if existing is None else "stored value is not a valid WPA2 PSK",
        )
        return self.generate_ap_passphrase()

    def generate_ap_passphrase(self) -> str:
        """Generate and persist a fresh Soft AP passphrase.

        Returns
        -------
        str
            The new passphrase (also stored on ``self.ap_passphrase``).
        """
        self.ap_passphrase = generate_ap_passphrase()
        _write_shared_ap_passphrase(self.ap_passphrase)
        return self.ap_passphrase

    def get_active_secret(self) -> str | None:
        """Return the current pairing secret from memory or the shared file."""
        if self.secret is not None:
            return self.secret
        existing = _read_shared_secret()
        if existing is not None and existing.isdigit() and len(existing) == _PAIRING_SECRET_DIGITS:
            return existing
        return self._last_secret

    def has_pairing_secret(self) -> bool:
        """Return True when a valid pairing secret is available."""
        return self.get_active_secret() is not None

    def verify_secret(self, candidate: str) -> bool:
        """Verify candidate against the active pairing secret (constant-time)."""
        active = self.get_active_secret()
        return active is not None and hmac.compare_digest(active, candidate)

    def complete_pairing(self, owner_email: str) -> None:
        """Mark the device as paired for *owner_email*."""
        self.paired = True
        self.owner_email = owner_email
        self.secret = None

    def verify_and_consume(self, candidate: str) -> bool:
        """Verify candidate against stored secret (constant-time).

        On success the secret is consumed from in-memory state — subsequent
        calls in the same runtime will return False until a new session is
        started. The shared on-disk secret remains available for secure
        re-pairing.

        Parameters
        ----------
        candidate : str
            The secret value to verify.

        Returns
        -------
        bool
            True if the candidate matches and the device was not already paired.
        """
        if self.paired or not self.verify_secret(candidate):
            return False
        self.paired = True
        self.secret = None
        return True

    def is_paired(self) -> bool:
        """Return whether the device has been paired.

        Returns
        -------
        bool
        """
        return self.paired

    def reset_session(self) -> None:
        """Invalidate the current auth session while preserving the pairing secret.

        Rotates the JWT signing secret so previously issued access tokens stop
        working, clears owner state, and leaves the shared pairing secret file
        untouched so the legitimate owner can pair again without restarting the
        service.
        """
        self.paired = False
        self.owner_email = None
        self.secret = None
        self.jwt_secret = DeviceJwtSecretStore().rotate()

    def reset(self) -> None:
        """Clear pairing state, rotate JWT state, and delete both shared secrets.

        Deletes the on-disk pairing secret and Soft AP passphrase files so the
        next service startup generates fresh values.  After reset the device is
        unpaired and a new pairing secret must be generated via
        :meth:`generate_secret` or :meth:`load_or_generate_secret`.
        """
        self.reset_session()
        self._last_secret = None
        self.ap_passphrase = None
        for path in (get_pairing_secret_path(), get_ap_passphrase_path()):
            try:
                os.unlink(path)
            except OSError:
                pass
