"""Tests for the device pairing module."""

import os
import stat
from unittest.mock import MagicMock, patch

import pytest

from nomothetic.pairing import (
    PairingState,
    _read_shared_secret,
    _write_shared_secret,
    generate_ap_passphrase,
    get_ap_passphrase_path,
    get_pairing_secret_path,
    is_valid_ap_passphrase,
)

# ============================================================================
# Secret generation
# ============================================================================


def test_generate_secret_returns_string():
    """generate_secret returns a non-empty string."""
    ps = PairingState()
    secret = ps.generate_secret()
    assert isinstance(secret, str)
    assert len(secret) > 0


def test_generate_secret_is_eight_digit_numeric():
    """Pairing secret is an 8-digit zero-padded numeric string."""
    ps = PairingState()
    secret = ps.generate_secret()
    assert len(secret) == 8
    assert secret.isdigit()


def test_generate_secret_unique():
    """Two generated secrets are different."""
    ps = PairingState()
    a = ps.generate_secret()
    b = ps.generate_secret()
    assert a != b


def test_generate_secret_stores_on_state():
    """generate_secret stores the secret on the PairingState."""
    ps = PairingState()
    secret = ps.generate_secret()
    assert ps.secret == secret


# ============================================================================
# Verify and consume
# ============================================================================


def test_verify_correct_secret():
    """Correct candidate returns True and consumes the secret."""
    ps = PairingState()
    secret = ps.generate_secret()
    assert ps.verify_and_consume(secret) is True
    assert ps.paired is True
    assert ps.secret is None


def test_verify_wrong_secret():
    """Wrong candidate returns False and leaves state unchanged."""
    ps = PairingState()
    ps.generate_secret()
    assert ps.verify_and_consume("wrong-secret") is False
    assert ps.paired is False
    assert ps.secret is not None


def test_consume_once_only():
    """A consumed secret cannot be used again."""
    ps = PairingState()
    secret = ps.generate_secret()
    assert ps.verify_and_consume(secret) is True
    assert ps.verify_and_consume(secret) is False


def test_verify_secret_uses_shared_secret_after_pairing(tmp_path):
    """verify_secret still works after in-memory consumption."""
    secret_path = str(tmp_path / "pairing_secret")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        ps = PairingState()
        secret = ps.generate_secret()
        assert ps.verify_and_consume(secret) is True
        assert ps.secret is None
        assert ps.has_pairing_secret() is True
        assert ps.verify_secret(secret) is True


def test_verify_no_secret_returns_false():
    """verify_and_consume returns False when no secret has been generated."""
    ps = PairingState()
    assert ps.verify_and_consume("anything") is False


def test_verify_already_paired_returns_false():
    """verify_and_consume returns False when already paired."""
    ps = PairingState()
    secret = ps.generate_secret()
    ps.verify_and_consume(secret)
    # Generate a new secret, but we're already paired
    new_secret = ps.generate_secret()
    # paired is reset by generate_secret
    assert ps.paired is False
    # Now verify with the new secret
    assert ps.verify_and_consume(new_secret) is True


# ============================================================================
# is_paired
# ============================================================================


def test_is_paired_initially_false():
    """is_paired returns False on a fresh PairingState."""
    ps = PairingState()
    assert ps.is_paired() is False


def test_is_paired_after_pairing():
    """is_paired returns True after successful pairing."""
    ps = PairingState()
    secret = ps.generate_secret()
    ps.verify_and_consume(secret)
    assert ps.is_paired() is True


# ============================================================================
# Reset
# ============================================================================


def test_reset_session_preserves_pairing_secret(tmp_path):
    """reset_session rotates auth state without deleting the shared secret."""
    secret_path = str(tmp_path / "pairing_secret")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        ps = PairingState()
        secret = ps.generate_secret()
        ps.verify_and_consume(secret)
        old_jwt = ps.jwt_secret

        ps.reset_session()

        assert ps.paired is False
        assert ps.owner_email is None
        assert ps.secret is None
        assert ps.jwt_secret != old_jwt
        assert os.path.exists(secret_path)
        assert ps.has_pairing_secret() is True
        assert ps.verify_secret(secret) is True


def test_reset_clears_state(tmp_path):
    """reset clears paired state, owner, and the shared pairing secret."""
    secret_path = str(tmp_path / "pairing_secret")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        ps = PairingState()
        secret = ps.generate_secret()
        ps.verify_and_consume(secret)
        ps.owner_email = "test@local"
        old_jwt = ps.jwt_secret

        ps.reset()

        assert ps.paired is False
        assert ps.owner_email is None
        assert ps.secret is None
        assert ps.jwt_secret != old_jwt
        assert not os.path.exists(secret_path)


# ============================================================================
# JWT secret
# ============================================================================


def test_jwt_secret_generated_on_init():
    """jwt_secret is generated on construction."""
    ps = PairingState()
    assert isinstance(ps.jwt_secret, str)
    assert len(ps.jwt_secret) >= 32


def test_jwt_secret_shared_across_instances(tmp_path):
    """PairingState instances on the same device share the persisted JWT secret."""
    secret_path = str(tmp_path / "device_jwt_secret")
    with patch.dict(os.environ, {"NOMON_DEVICE_JWT_SECRET_PATH": secret_path}):
        a = PairingState()
        b = PairingState()
        assert a.jwt_secret == b.jwt_secret


# ============================================================================
# get_pairing_secret_path
# ============================================================================


def test_get_pairing_secret_path_default():
    """Returns /var/lib/nomon/pairing_secret when env var is not set."""
    with patch.dict(os.environ, {}, clear=True):
        # Remove the env var if set
        os.environ.pop("NOMON_PAIRING_SECRET_PATH", None)
        assert get_pairing_secret_path() == "/var/lib/nomon/pairing_secret"


def test_get_pairing_secret_path_from_env():
    """Returns the path from NOMON_PAIRING_SECRET_PATH env var."""
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": "/tmp/test_secret"}):
        assert get_pairing_secret_path() == "/tmp/test_secret"


# ============================================================================
# Shared secret file write
# ============================================================================


def test_write_shared_secret_to_file(tmp_path):
    """Pairing secret is written to the configured path."""
    secret_path = str(tmp_path / "pairing_secret")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        _write_shared_secret("test-secret-value")

    assert os.path.exists(secret_path)
    with open(secret_path) as f:
        assert f.read() == "test-secret-value"


def test_write_shared_secret_read_back(tmp_path):
    """Written secret can be read back with correct content."""
    secret_path = str(tmp_path / "pairing_secret")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        _write_shared_secret("round-trip-test")

    with open(secret_path) as f:
        content = f.read()
    assert content == "round-trip-test"


def test_write_shared_secret_file_mode(tmp_path):
    """Secret file is created with mode 0640."""
    secret_path = str(tmp_path / "pairing_secret")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        _write_shared_secret("mode-test")

    file_stat = os.stat(secret_path)
    mode = stat.S_IMODE(file_stat.st_mode)
    assert mode == 0o640


def test_write_shared_secret_atomic_rename(tmp_path):
    """Uses atomic write (temp file + rename)."""
    secret_path = str(tmp_path / "pairing_secret")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        with patch("nomothetic.pairing.os.rename", wraps=os.rename) as mock_rename:
            _write_shared_secret("atomic-test")
            mock_rename.assert_called_once()
            # The second argument should be the final path
            assert mock_rename.call_args[0][1] == secret_path


def test_write_shared_secret_missing_directory(tmp_path):
    """Logs warning and returns without error when directory doesn't exist."""
    secret_path = str(tmp_path / "nonexistent" / "pairing_secret")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        # Should not raise — just log a warning
        _write_shared_secret("missing-dir-test")

    assert not os.path.exists(secret_path)


def test_write_shared_secret_group_set_attempted(tmp_path):
    """Attempts to set the 'nomon' group on the secret file."""
    secret_path = str(tmp_path / "pairing_secret")
    mock_grp = MagicMock()
    mock_grp.gr_gid = 12345
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        with patch("nomothetic.pairing.grp.getgrnam", return_value=mock_grp):
            with patch("nomothetic.pairing.os.chown") as mock_chown:
                _write_shared_secret("group-test")
                mock_chown.assert_called_once()
                _, gid = mock_chown.call_args[0][1], mock_chown.call_args[0][2]
                assert gid == 12345


def test_write_shared_secret_group_missing_graceful(tmp_path):
    """Handles missing 'nomon' group gracefully."""
    secret_path = str(tmp_path / "pairing_secret")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        with patch("nomothetic.pairing.grp.getgrnam", side_effect=KeyError("nomon")):
            # Should not raise — just log a warning
            _write_shared_secret("no-group-test")

    # File should still exist despite group error
    assert os.path.exists(secret_path)


def test_generate_secret_writes_shared_file(tmp_path):
    """generate_secret writes the secret to the shared file."""
    secret_path = str(tmp_path / "pairing_secret")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        ps = PairingState()
        secret = ps.generate_secret()

    with open(secret_path) as f:
        assert f.read() == secret


def test_write_shared_secret_overwrites_existing(tmp_path):
    """Writing a new secret overwrites the previous one atomically."""
    secret_path = str(tmp_path / "pairing_secret")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        _write_shared_secret("first-secret")
        _write_shared_secret("second-secret")

    with open(secret_path) as f:
        assert f.read() == "second-secret"


# ============================================================================
# _read_shared_secret
# ============================================================================


def test_read_shared_secret_returns_value(tmp_path):
    """Returns the secret string when file exists with valid content."""
    secret_path = str(tmp_path / "pairing_secret")
    with open(secret_path, "w") as fh:
        fh.write("12345678")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        assert _read_shared_secret() == "12345678"


def test_read_shared_secret_strips_whitespace(tmp_path):
    """Strips leading/trailing whitespace from the file content."""
    secret_path = str(tmp_path / "pairing_secret")
    with open(secret_path, "w") as fh:
        fh.write("  04200077\n")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        assert _read_shared_secret() == "04200077"


def test_read_shared_secret_absent_returns_none(tmp_path):
    """Returns None when the file does not exist."""
    secret_path = str(tmp_path / "no_such_file")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        assert _read_shared_secret() is None


def test_read_shared_secret_empty_returns_none(tmp_path):
    """Returns None when the file exists but is empty."""
    secret_path = str(tmp_path / "pairing_secret")
    with open(secret_path, "w") as fh:
        fh.write("")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        assert _read_shared_secret() is None


def test_read_shared_secret_whitespace_only_returns_none(tmp_path):
    """Returns None when the file contains only whitespace."""
    secret_path = str(tmp_path / "pairing_secret")
    with open(secret_path, "w") as fh:
        fh.write("   \n")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        assert _read_shared_secret() is None


# ============================================================================
# load_or_generate_secret
# ============================================================================


def test_load_or_generate_loads_existing_secret(tmp_path):
    """Returns and sets the on-disk secret without regenerating the file."""
    secret_path = str(tmp_path / "pairing_secret")
    with open(secret_path, "w") as fh:
        fh.write("04200077")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        ps = PairingState()
        result = ps.load_or_generate_secret()
    assert result == "04200077"
    assert ps.secret == "04200077"
    assert ps.paired is False


def test_load_or_generate_does_not_overwrite_existing(tmp_path):
    """Does not rewrite the file when loading an existing valid secret."""
    secret_path = str(tmp_path / "pairing_secret")
    with open(secret_path, "w") as fh:
        fh.write("04200077")
    mtime_before = os.path.getmtime(secret_path)
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        ps = PairingState()
        ps.load_or_generate_secret()
    mtime_after = os.path.getmtime(secret_path)
    assert mtime_before == mtime_after


def test_load_or_generate_creates_file_when_absent(tmp_path):
    """Generates and writes a new secret when no file exists."""
    secret_path = str(tmp_path / "pairing_secret")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        ps = PairingState()
        result = ps.load_or_generate_secret()
    assert os.path.exists(secret_path)
    assert result.isdigit() and len(result) == 8
    assert ps.secret == result


def test_load_or_generate_rejects_non_digit_secret(tmp_path):
    """Generates a new secret when the file contains a non-digit value."""
    secret_path = str(tmp_path / "pairing_secret")
    with open(secret_path, "w") as fh:
        fh.write("abcdef")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        ps = PairingState()
        result = ps.load_or_generate_secret()
    assert result.isdigit() and len(result) == 8
    assert result != "abcdef"


def test_load_or_generate_rejects_wrong_length_secret(tmp_path):
    """Generates a new secret when the file contains a secret of wrong length."""
    secret_path = str(tmp_path / "pairing_secret")
    with open(secret_path, "w") as fh:
        fh.write("1234")  # only 4 digits
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        ps = PairingState()
        result = ps.load_or_generate_secret()
    assert len(result) == 8
    assert result != "1234"


def test_load_or_generate_logs_loaded_path(tmp_path, caplog):
    """Logs at INFO level when loading an existing secret."""
    import logging

    secret_path = str(tmp_path / "pairing_secret")
    with open(secret_path, "w") as fh:
        fh.write("00077777")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        ps = PairingState()
        with caplog.at_level(logging.INFO, logger="nomothetic.pairing"):
            ps.load_or_generate_secret()
    assert any("Loaded existing pairing secret" in r.message for r in caplog.records)


def test_load_or_generate_logs_generated(tmp_path, caplog):
    """Logs visibly (WARNING) when generating a new secret — rotation is loud."""
    import logging

    secret_path = str(tmp_path / "no_secret")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        ps = PairingState()
        with caplog.at_level(logging.WARNING, logger="nomothetic.pairing"):
            ps.load_or_generate_secret()
    assert any("Generating a new pairing secret" in r.message for r in caplog.records)


# ============================================================================
# reset — deletes the on-disk secret file
# ============================================================================


def test_reset_deletes_secret_file(tmp_path):
    """reset() deletes the on-disk pairing secret file."""
    secret_path = str(tmp_path / "pairing_secret")
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": secret_path}):
        ps = PairingState()
        ps.generate_secret()
        assert os.path.exists(secret_path)
        ps.reset()
    assert not os.path.exists(secret_path)


def test_reset_tolerates_missing_file():
    """reset() does not raise if the secret file does not exist."""
    with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": "/nonexistent/path"}):
        ps = PairingState()
        ps.reset()  # Must not raise


# ============================================================================
# Unreadable-but-present secret files are never overwritten
# ============================================================================


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 000 does not block root")
def test_load_or_generate_never_overwrites_unreadable_file(tmp_path):
    """An existing-but-unreadable secret file survives; a session secret is used.

    The on-disk value doubles as the Soft AP passphrase, so a permissions or
    transient-I/O fault must not silently rotate it.
    """
    secret_path = tmp_path / "pairing_secret"
    secret_path.write_text("13572468")
    secret_path.chmod(0o000)
    try:
        with patch.dict(os.environ, {"NOMON_PAIRING_SECRET_PATH": str(secret_path)}):
            ps = PairingState()
            result = ps.load_or_generate_secret()
        assert result.isdigit() and len(result) == 8
    finally:
        secret_path.chmod(0o600)
    assert secret_path.read_text() == "13572468"


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 000 does not block root")
def test_jwt_store_never_overwrites_unreadable_file(tmp_path):
    """An existing-but-unreadable JWT secret survives; an in-memory one is used.

    Overwriting would silently invalidate every issued device token.
    """
    from nomothetic.device_jwt import DeviceJwtSecretStore

    jwt_path = tmp_path / "device_jwt_secret"
    jwt_path.write_text("x" * 64)
    jwt_path.chmod(0o000)
    try:
        with patch.dict(os.environ, {"NOMON_DEVICE_JWT_SECRET_PATH": str(jwt_path)}):
            secret = DeviceJwtSecretStore().load_or_generate()
        assert len(secret) >= 32
        assert secret != "x" * 64
    finally:
        jwt_path.chmod(0o600)
    assert jwt_path.read_text() == "x" * 64


# ============================================================================
# Soft AP passphrase — independent of the 8-digit pairing code (review S-1)
# ============================================================================


def test_ap_passphrase_is_long_and_wpa2_safe():
    """The AP passphrase is a long random WPA2-safe string, not the pairing code."""
    value = generate_ap_passphrase()
    assert len(value) == 20
    assert is_valid_ap_passphrase(value)
    assert not value.isdigit()
    for ambiguous in "0O1lI":
        assert ambiguous not in value


def test_ap_passphrase_unique():
    assert generate_ap_passphrase() != generate_ap_passphrase()


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("12345678", True),
        ("1234567", False),
        ("x" * 63, True),
        ("x" * 64, False),
        ("has\ttab", False),
        ("héllo-wörld", False),
    ],
)
def test_is_valid_ap_passphrase(value, ok):
    assert is_valid_ap_passphrase(value) is ok


def test_get_ap_passphrase_path_default():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NOMON_AP_PASSPHRASE_PATH", None)
        assert get_ap_passphrase_path() == "/var/lib/nomon/ap_passphrase"


def test_get_ap_passphrase_path_from_env():
    with patch.dict(os.environ, {"NOMON_AP_PASSPHRASE_PATH": "/tmp/x/ap"}):
        assert get_ap_passphrase_path() == "/tmp/x/ap"


def test_ap_passphrase_differs_from_pairing_secret(tmp_path):
    secret_path = str(tmp_path / "pairing_secret")
    ap_path = str(tmp_path / "ap_passphrase")
    with patch.dict(
        os.environ,
        {"NOMON_PAIRING_SECRET_PATH": secret_path, "NOMON_AP_PASSPHRASE_PATH": ap_path},
    ):
        ps = PairingState()
        code = ps.load_or_generate_secret()
        passphrase = ps.load_or_generate_ap_passphrase()
    assert code != passphrase
    assert len(code) == 8 and code.isdigit()
    assert len(passphrase) == 20


def test_load_or_generate_ap_passphrase_creates_file_0640(tmp_path):
    ap_path = str(tmp_path / "ap_passphrase")
    with patch.dict(os.environ, {"NOMON_AP_PASSPHRASE_PATH": ap_path}):
        ps = PairingState()
        value = ps.load_or_generate_ap_passphrase()
    assert ps.ap_passphrase == value
    with open(ap_path) as fh:
        assert fh.read() == value
    assert stat.S_IMODE(os.stat(ap_path).st_mode) == 0o640


def test_load_or_generate_ap_passphrase_loads_existing(tmp_path):
    ap_path = tmp_path / "ap_passphrase"
    ap_path.write_text("StableHotspotPass42\n")
    with patch.dict(os.environ, {"NOMON_AP_PASSPHRASE_PATH": str(ap_path)}):
        ps = PairingState()
        assert ps.load_or_generate_ap_passphrase() == "StableHotspotPass42"
    assert ap_path.read_text() == "StableHotspotPass42\n"


def test_load_or_generate_ap_passphrase_replaces_invalid(tmp_path):
    """A stored value that is not a valid WPA2 PSK (e.g. a legacy 8-digit code
    accidentally copied here, or too short) is regenerated."""
    ap_path = tmp_path / "ap_passphrase"
    ap_path.write_text("short")
    with patch.dict(os.environ, {"NOMON_AP_PASSPHRASE_PATH": str(ap_path)}):
        ps = PairingState()
        value = ps.load_or_generate_ap_passphrase()
    assert value != "short"
    assert ap_path.read_text() == value


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 000 does not block root")
def test_load_or_generate_ap_passphrase_never_overwrites_unreadable(tmp_path):
    ap_path = tmp_path / "ap_passphrase"
    ap_path.write_text("KeepMeHotspotPass99")
    os.chmod(ap_path, 0)
    try:
        with patch.dict(os.environ, {"NOMON_AP_PASSPHRASE_PATH": str(ap_path)}):
            ps = PairingState()
            value = ps.load_or_generate_ap_passphrase()
        assert is_valid_ap_passphrase(value)
    finally:
        os.chmod(ap_path, 0o600)
    assert ap_path.read_text() == "KeepMeHotspotPass99"


def test_reset_deletes_ap_passphrase_file(tmp_path):
    secret_path = str(tmp_path / "pairing_secret")
    ap_path = str(tmp_path / "ap_passphrase")
    with patch.dict(
        os.environ,
        {"NOMON_PAIRING_SECRET_PATH": secret_path, "NOMON_AP_PASSPHRASE_PATH": ap_path},
    ):
        ps = PairingState()
        ps.generate_secret()
        ps.load_or_generate_ap_passphrase()
        assert os.path.exists(ap_path)
        ps.reset()
    assert not os.path.exists(ap_path)
    assert ps.ap_passphrase is None


def test_reset_session_keeps_ap_passphrase(tmp_path):
    ap_path = str(tmp_path / "ap_passphrase")
    with patch.dict(os.environ, {"NOMON_AP_PASSPHRASE_PATH": ap_path}):
        ps = PairingState()
        value = ps.load_or_generate_ap_passphrase()
        ps.reset_session()
    assert os.path.exists(ap_path)
    assert ps.ap_passphrase == value
