"""Tests for the JWT authentication module."""

import os
import time
from unittest.mock import patch

import pytest

from nomothetic.auth import AuthService, set_auth_service

_TEST_SECRET = "test-secret-key-that-is-at-least-32-bytes-long!"


@pytest.fixture
def auth_service():
    """Create an AuthService with a test secret."""
    svc = AuthService(secret=_TEST_SECRET)
    set_auth_service(svc)
    yield svc
    set_auth_service(None)


# ============================================================================
# Initialisation
# ============================================================================


def test_secret_too_short():
    """AuthService rejects secrets shorter than 32 bytes."""
    with pytest.raises(ValueError, match="at least 32 characters"):
        AuthService(secret="short")


def test_secret_from_env():
    """AuthService reads NOMON_JWT_SECRET from environment."""
    with patch.dict(os.environ, {"NOMON_JWT_SECRET": _TEST_SECRET}):
        svc = AuthService()
        assert svc is not None


def test_missing_secret_raises():
    """AuthService raises when no secret is provided."""
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("NOMON_JWT_SECRET", None)
        with pytest.raises(ValueError):
            AuthService()


# ============================================================================
# Password hashing
# ============================================================================


def test_hash_and_verify_password(auth_service):
    """Hashed password verifies correctly."""
    hashed = auth_service.hash_password("my-password")
    assert auth_service.verify_password("my-password", hashed)


def test_wrong_password_fails(auth_service):
    """Wrong password does not verify."""
    hashed = auth_service.hash_password("correct")
    assert not auth_service.verify_password("wrong", hashed)


# ============================================================================
# User management
# ============================================================================


@pytest.mark.asyncio
async def test_create_user(auth_service):
    """Creating a user stores and returns a record."""
    user = await auth_service.create_user("alice@example.com", "password123", "Alice")
    assert user.email == "alice@example.com"
    assert user.display_name == "Alice"
    assert user.active is True
    assert user.created_at is not None


@pytest.mark.asyncio
async def test_create_duplicate_user_raises(auth_service):
    """Duplicate email registration raises ValueError."""
    await auth_service.create_user("alice@example.com", "password123", "Alice")
    with pytest.raises(ValueError, match="already registered"):
        await auth_service.create_user("alice@example.com", "password456", "Alice2")


@pytest.mark.asyncio
async def test_email_normalised(auth_service):
    """Emails are normalised to lowercase."""
    await auth_service.create_user("Alice@EXAMPLE.com", "password123", "Alice")
    user = await auth_service.get_user("alice@example.com")
    assert user is not None
    assert user.email == "alice@example.com"


# ============================================================================
# Authentication
# ============================================================================


@pytest.mark.asyncio
async def test_authenticate_success(auth_service):
    """Valid credentials return the user record."""
    await auth_service.create_user("bob@example.com", "secret123", "Bob")
    user = await auth_service.authenticate("bob@example.com", "secret123")
    assert user is not None
    assert user.email == "bob@example.com"
    assert user.last_login_at is not None


@pytest.mark.asyncio
async def test_authenticate_wrong_password(auth_service):
    """Wrong password returns None."""
    await auth_service.create_user("bob@example.com", "secret123", "Bob")
    assert await auth_service.authenticate("bob@example.com", "wrong") is None


@pytest.mark.asyncio
async def test_authenticate_unknown_email(auth_service):
    """Unknown email returns None."""
    assert await auth_service.authenticate("nobody@example.com", "password") is None


@pytest.mark.asyncio
async def test_authenticate_inactive_user(auth_service):
    """Inactive user cannot authenticate."""
    user = await auth_service.create_user("eve@example.com", "password123", "Eve")
    user.active = False
    assert await auth_service.authenticate("eve@example.com", "password123") is None


# ============================================================================
# Token creation and verification
# ============================================================================


def test_create_and_verify_access_token(auth_service):
    """Access token round-trips through create/verify."""
    token = auth_service.create_access_token("alice@example.com")
    payload = auth_service.verify_token(token)
    assert payload.sub == "alice@example.com"
    assert payload.iss == "nomon-central"


def test_expired_token_rejected(auth_service):
    """Expired access token raises ValueError."""
    from authlib.jose import jwt as authlib_jwt

    header = {"alg": "HS256"}
    payload = {
        "sub": "alice@example.com",
        "iat": int(time.time()) - 3600,
        "exp": int(time.time()) - 1800,
        "iss": "nomon-central",
    }
    token = authlib_jwt.encode(header, payload, _TEST_SECRET)
    with pytest.raises(ValueError, match="expired"):
        auth_service.verify_token(token)


def test_invalid_token_rejected(auth_service):
    """Malformed token raises ValueError."""
    with pytest.raises(ValueError, match="Invalid token"):
        auth_service.verify_token("not-a-real-token")


def test_create_tokens_returns_both(auth_service):
    """create_tokens returns access, refresh, type, and expiry."""
    import asyncio

    tokens = asyncio.run(auth_service.create_tokens("alice@example.com"))
    assert "access_token" in tokens
    assert "refresh_token" in tokens
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == 900


# ============================================================================
# Refresh token rotation
# ============================================================================


@pytest.mark.asyncio
async def test_refresh_token_rotation(auth_service):
    """Refresh token issues new tokens and invalidates the old one."""
    await auth_service.create_user("carol@example.com", "password123", "Carol")
    tokens = await auth_service.create_tokens("carol@example.com")
    old_refresh = tokens["refresh_token"]

    new_tokens = await auth_service.refresh_token(old_refresh)
    assert "access_token" in new_tokens
    assert "refresh_token" in new_tokens

    # Old refresh token should be invalidated
    with pytest.raises(ValueError, match="Invalid or expired"):
        await auth_service.refresh_token(old_refresh)


@pytest.mark.asyncio
async def test_refresh_unknown_token(auth_service):
    """Unknown refresh token raises ValueError."""
    with pytest.raises(ValueError, match="Invalid or expired"):
        await auth_service.refresh_token("bogus-token")
