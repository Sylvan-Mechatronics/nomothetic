"""Tests for device-mode authentication endpoints."""

import os
from typing import cast
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nomothetic.api import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def device_auth_client():
    """Test client with device auth enabled (default)."""
    with patch.dict(
        os.environ,
        {"NOMON_DEVICE_AUTH": "true", "NOMON_API_MODE": "device"},
        clear=False,
    ):
        app = create_app()
        client = TestClient(app)
        yield client, app


@pytest.fixture
def device_no_auth_client():
    """Test client with device auth disabled."""
    with patch.dict(
        os.environ,
        {"NOMON_DEVICE_AUTH": "false", "NOMON_API_MODE": "device"},
        clear=False,
    ):
        app = create_app()
        client = TestClient(app)
        yield client, app


def _get_pairing_secret(app) -> str:
    """Extract the pairing secret from app state."""
    return cast(str, app.state.pairing_state.secret)


def _pair(client, secret: str, display_name: str = "Test Owner"):
    """Perform a pairing request and return the response."""
    return client.post(
        "/api/device/auth/pair",
        json={"secret": secret, "display_name": display_name},
    )


def _get_token(client, app) -> str:
    """Pair and return the access token."""
    secret = _get_pairing_secret(app)
    resp = _pair(client, secret)
    return cast(str, resp.json()["access_token"])


# ============================================================================
# Status endpoint
# ============================================================================


def test_status_unpaired(device_auth_client):
    """Status shows unpaired with secret available before pairing."""
    client, app = device_auth_client
    resp = client.get("/api/device/auth/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["paired"] is False
    assert data["pairing_available"] is True


def test_status_after_pairing(device_auth_client):
    """Status shows paired after successful pairing."""
    client, app = device_auth_client
    secret = _get_pairing_secret(app)
    _pair(client, secret)
    resp = client.get("/api/device/auth/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["paired"] is True
    assert data["pairing_available"] is False


# ============================================================================
# Pair endpoint
# ============================================================================


def test_pair_success(device_auth_client):
    """Correct secret pairs the device and returns tokens."""
    client, app = device_auth_client
    secret = _get_pairing_secret(app)
    resp = _pair(client, secret)
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0


def test_pair_wrong_secret(device_auth_client):
    """Wrong secret returns 401."""
    client, app = device_auth_client
    resp = _pair(client, "wrong-secret-value")
    assert resp.status_code == 401


def test_repair_with_wrong_secret_returns_401(device_auth_client):
    """Re-pairing with a wrong secret is rejected without clearing the session."""
    client, app = device_auth_client
    secret = _get_pairing_secret(app)
    first = _pair(client, secret)
    old_access = first.json()["access_token"]

    resp = _pair(client, "wrong-secret-value")
    assert resp.status_code == 401

    me_resp = client.get(
        "/api/device/auth/me",
        headers={"Authorization": f"Bearer {old_access}"},
    )
    assert me_resp.status_code == 200


def test_repair_with_correct_secret_returns_fresh_tokens(device_auth_client):
    """Re-pairing with the correct secret succeeds and returns fresh tokens."""
    client, app = device_auth_client
    secret = _get_pairing_secret(app)
    first = _pair(client, secret)
    assert first.status_code == 200

    resp = _pair(client, secret)
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"] != first.json()["access_token"]
    assert data["refresh_token"] != first.json()["refresh_token"]


def test_repair_invalidates_old_tokens(device_auth_client):
    """After re-pairing, both access and refresh tokens from the old session fail."""
    client, app = device_auth_client
    secret = _get_pairing_secret(app)
    first = _pair(client, secret)
    old_access = first.json()["access_token"]
    old_refresh = first.json()["refresh_token"]

    _pair(client, secret)

    refresh_resp = client.post(
        "/api/device/auth/refresh",
        json={"refresh_token": old_refresh},
    )
    assert refresh_resp.status_code == 401

    me_resp = client.get(
        "/api/device/auth/me",
        headers={"Authorization": f"Bearer {old_access}"},
    )
    assert me_resp.status_code == 401


def test_pair_rate_limited(device_auth_client):
    """Pairing endpoint is rate-limited to 3 requests per minute."""
    client, app = device_auth_client
    for _ in range(3):
        client.post(
            "/api/device/auth/pair",
            json={"secret": "wrong", "display_name": "Attacker"},
        )
    resp = client.post(
        "/api/device/auth/pair",
        json={"secret": "wrong", "display_name": "Attacker"},
    )
    assert resp.status_code == 429


# ============================================================================
# Pair via AP endpoint
# ============================================================================


def _pair_via_ap(client, display_name: str = "Test Owner"):
    """Perform an AP pairing request and return the response."""
    return client.post(
        "/api/device/auth/pair/ap",
        json={"display_name": display_name},
    )


def test_pair_via_ap_forbidden_when_not_on_ap(device_auth_client):
    """Request from a non-AP IP returns 403 without consuming the secret."""
    client, app = device_auth_client
    # Default TestClient IP is not in 192.168.4.0/24
    resp = _pair_via_ap(client)
    assert resp.status_code == 403
    # Secret must still be available (not consumed)
    assert app.state.pairing_state.secret is not None


def test_pair_via_ap_success_from_ap_subnet(device_auth_client):
    """AP-subnet request pairs the device without an explicit secret."""
    client, app = device_auth_client
    with patch("nomothetic.device_auth_routes._is_ap_client", return_value=True):
        resp = _pair_via_ap(client)
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0
    assert "device_hostname" in data
    # Secret must be consumed and device marked paired
    assert app.state.pairing_state.is_paired() is True
    assert app.state.pairing_state.secret is None


def test_pair_via_ap_already_paired_wrong_path(device_auth_client):
    """AP pairing from a non-AP IP returns 403 even after the device is paired."""
    client, app = device_auth_client
    secret = _get_pairing_secret(app)
    _pair(client, secret)
    resp = _pair_via_ap(client)
    assert resp.status_code == 403


def test_pair_via_ap_reissues_tokens_when_already_paired(device_auth_client):
    """AP pairing can securely re-pair and invalidate the old session."""
    client, app = device_auth_client
    secret = _get_pairing_secret(app)
    first = _pair(client, secret)
    old_access = first.json()["access_token"]
    old_refresh = first.json()["refresh_token"]

    with patch("nomothetic.device_auth_routes._is_ap_client", return_value=True):
        resp = _pair_via_ap(client, display_name="Re-paired Owner")
    assert resp.status_code == 200

    refresh_resp = client.post(
        "/api/device/auth/refresh",
        json={"refresh_token": old_refresh},
    )
    assert refresh_resp.status_code == 401

    me_resp = client.get(
        "/api/device/auth/me",
        headers={"Authorization": f"Bearer {old_access}"},
    )
    assert me_resp.status_code == 401


def test_pair_via_ap_rate_limited(device_auth_client):
    """AP pairing endpoint shares the pairing rate limit (3 requests per minute)."""
    client, app = device_auth_client
    with patch("nomothetic.device_auth_routes._is_ap_client", return_value=True):
        for _ in range(3):
            _pair_via_ap(client)
        resp = _pair_via_ap(client)
    assert resp.status_code == 429


# ============================================================================
# Refresh endpoint
# ============================================================================


def test_refresh_success(device_auth_client):
    """Valid refresh token returns new tokens."""
    client, app = device_auth_client
    secret = _get_pairing_secret(app)
    pair_resp = _pair(client, secret)
    refresh_token = pair_resp.json()["refresh_token"]

    resp = client.post(
        "/api/device/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert "refresh_token" in data


def test_refresh_invalid_token(device_auth_client):
    """Invalid refresh token returns 401."""
    client, app = device_auth_client
    resp = client.post(
        "/api/device/auth/refresh",
        json={"refresh_token": "invalid-refresh-token"},
    )
    assert resp.status_code == 401


# ============================================================================
# Session reset endpoint
# ============================================================================


def test_delete_session_revokes_current_session(device_auth_client):
    """DELETE /session revokes the old session and reopens pairing."""
    client, app = device_auth_client
    secret = _get_pairing_secret(app)
    pair_resp = _pair(client, secret)
    access = pair_resp.json()["access_token"]
    refresh = pair_resp.json()["refresh_token"]

    resp = client.delete(
        "/api/device/auth/session",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    status_resp = client.get("/api/device/auth/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["paired"] is False
    assert status_resp.json()["pairing_available"] is True

    refresh_resp = client.post(
        "/api/device/auth/refresh",
        json={"refresh_token": refresh},
    )
    assert refresh_resp.status_code == 401

    me_resp = client.get(
        "/api/device/auth/me",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert me_resp.status_code == 401


def test_delete_session_requires_auth(device_auth_client):
    """DELETE /session without a JWT returns 401."""
    client, app = device_auth_client
    resp = client.delete("/api/device/auth/session")
    assert resp.status_code == 401


# ============================================================================
# Me endpoint
# ============================================================================


def test_me_returns_profile(device_auth_client):
    """Authenticated /me returns the paired owner profile."""
    client, app = device_auth_client
    token = _get_token(client, app)
    resp = client.get(
        "/api/device/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "device-owner@local"
    assert data["display_name"] == "Test Owner"
    assert "timestamp" in data


def test_me_requires_auth(device_auth_client):
    """/me without token returns 401."""
    client, app = device_auth_client
    resp = client.get("/api/device/auth/me")
    assert resp.status_code == 401


# ============================================================================
# Device endpoints require auth
# ============================================================================


def test_device_endpoint_requires_token(device_auth_client):
    """Device API endpoint returns 401 without token."""
    client, app = device_auth_client
    resp = client.get("/api/camera/status")
    assert resp.status_code == 401


def test_device_endpoint_succeeds_with_token(device_auth_client):
    """Device API endpoint succeeds with valid token (may 500 due to no camera, but not 401)."""
    client, app = device_auth_client
    token = _get_token(client, app)
    resp = client.get(
        "/api/camera/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    # 500 (no camera) or 503 (no daemon) is acceptable — NOT 401
    assert resp.status_code != 401


def test_health_no_auth_required(device_auth_client):
    """Health endpoint is accessible without token."""
    client, app = device_auth_client
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


# ============================================================================
# Auth disabled mode
# ============================================================================


def test_no_auth_mode_health(device_no_auth_client):
    """Health works in no-auth mode."""
    client, app = device_no_auth_client
    resp = client.get("/")
    assert resp.status_code == 200


def test_no_auth_mode_no_pairing_endpoints(device_no_auth_client):
    """Pairing endpoints are not registered in no-auth mode."""
    client, app = device_no_auth_client
    resp = client.get("/api/device/auth/status")
    assert resp.status_code == 404


def test_no_auth_mode_endpoints_unauthenticated(device_no_auth_client):
    """Device endpoints work without token in no-auth mode."""
    client, app = device_no_auth_client
    resp = client.get("/api/camera/status")
    # 500 (no camera) is expected — NOT 401
    assert resp.status_code != 401
    assert resp.status_code != 403


# ============================================================================
# Token isolation (issuer check)
# ============================================================================


def test_central_token_rejected_on_device(device_auth_client):
    """A token with central issuer is rejected by device-mode auth."""
    client, app = device_auth_client
    from nomothetic.auth import AuthService

    # Create a separate AuthService with central issuer
    central_svc = AuthService(
        secret=app.state.pairing_state.jwt_secret,
        issuer="nomon-central",
    )
    central_token = central_svc.create_access_token("alice@example.com")

    resp = client.get(
        "/api/camera/status",
        headers={"Authorization": f"Bearer {central_token}"},
    )
    assert resp.status_code == 401


# ============================================================================
# Identity endpoint
# ============================================================================


def test_identity_requires_auth(device_auth_client):
    """GET /identity without a token returns 401."""
    client, app = device_auth_client
    resp = client.get("/api/device/auth/identity")
    assert resp.status_code == 401


def test_identity_returns_fields(device_auth_client):
    """Authenticated GET /identity returns vin, model, hostname, firmware_version, and proof."""
    client, app = device_auth_client
    token = _get_token(client, app)
    resp = client.get(
        "/api/device/auth/identity",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "vin" in data
    assert "model" in data
    assert "hostname" in data
    assert "firmware_version" in data
    assert isinstance(data["firmware_version"], str)
    assert len(data["firmware_version"]) > 0
    assert "registration_proof" in data
    assert len(data["registration_proof"]) > 0


def test_identity_vin_env_override(device_auth_client):
    """NOMON_DEVICE_ID env var overrides the hardware-derived VIN."""
    client, app = device_auth_client
    token = _get_token(client, app)
    with patch.dict(os.environ, {"NOMON_DEVICE_ID": "TEST-DEVICE-0001"}):
        resp = client.get(
            "/api/device/auth/identity",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["vin"] == "TEST-DEVICE-0001"


def test_identity_proof_structure(device_auth_client):
    """Registration proof is a three-part JWT with correct claims."""
    import base64
    import json as _json
    import time

    client, app = device_auth_client
    token = _get_token(client, app)
    resp = client.get(
        "/api/device/auth/identity",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    proof = data["registration_proof"]

    parts = proof.split(".")
    assert len(parts) == 3, "Proof must be a three-part JWT"

    padding = "=" * (-len(parts[1]) % 4)
    payload = _json.loads(base64.urlsafe_b64decode(parts[1] + padding))

    assert payload["exp"] > time.time(), "Proof must not be expired"
    assert payload["sub"] == data["vin"], "Proof sub must match returned VIN"
    assert payload["aud"] == "nomon-fleet", "Proof audience must be nomon-fleet"
    assert "jti" in payload, "Proof must include a unique jti"


def test_identity_proof_vin_bound_to_env_override(device_auth_client):
    """Proof sub claim tracks the overridden NOMON_DEVICE_ID value."""
    import base64
    import json as _json

    client, app = device_auth_client
    token = _get_token(client, app)
    with patch.dict(os.environ, {"NOMON_DEVICE_ID": "CUSTOM-VIN-9999"}):
        resp = client.get(
            "/api/device/auth/identity",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    parts = resp.json()["registration_proof"].split(".")
    padding = "=" * (-len(parts[1]) % 4)
    payload = _json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    assert payload["sub"] == "CUSTOM-VIN-9999"


def test_identity_not_starved_by_pairing_limit(device_auth_client):
    """GET /identity has its own limiter, so pairing attempts do not starve it."""
    client, app = device_auth_client
    token = _get_token(client, app)
    # Exhaust the pairing rate limit (3/min) with wrong-secret pair attempts.
    for _ in range(3):
        client.post(
            "/api/device/auth/pair",
            json={"secret": "wrong", "display_name": "Attacker"},
        )
    # Identity is a separate budget and must still be reachable.
    resp = client.get(
        "/api/device/auth/identity",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


def test_identity_rate_limited(device_auth_client):
    """GET /identity is rate limited on its own budget (10 per minute)."""
    client, app = device_auth_client
    token = _get_token(client, app)
    headers = {"Authorization": f"Bearer {token}"}
    for _ in range(10):
        assert client.get("/api/device/auth/identity", headers=headers).status_code == 200
    resp = client.get("/api/device/auth/identity", headers=headers)
    assert resp.status_code == 429


def test_identity_limit_does_not_starve_pairing(device_auth_client):
    """Exhausting the identity budget leaves the pairing budget intact."""
    client, app = device_auth_client
    token = _get_token(client, app)
    headers = {"Authorization": f"Bearer {token}"}
    for _ in range(11):
        client.get("/api/device/auth/identity", headers=headers)
    resp = client.post(
        "/api/device/auth/pair",
        json={"secret": "wrong", "display_name": "Attacker"},
    )
    assert resp.status_code != 429
