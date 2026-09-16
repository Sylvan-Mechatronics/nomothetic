"""Tests for plugin authentication (Ed25519 challenge-response).

Covers the PluginKeyStore / ChallengeStore units and the
register/challenge/token route flow. No Pi hardware required.
"""

import base64
import os
import time
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from nomothetic.api import create_app
from nomothetic.plugin_auth import (
    ChallengeStore,
    InvalidPluginName,
    KeyConflict,
    PluginAuthError,
    PluginKeyStore,
    verify_signature,
)

# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    """Return (private_key, public_key_pem)."""
    priv = Ed25519PrivateKey.generate()
    pem = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return priv, pem


def _sign(priv: Ed25519PrivateKey, nonce: str) -> str:
    return base64.b64encode(priv.sign(nonce.encode("utf-8"))).decode()


# ---------------------------------------------------------------------------
# PluginKeyStore
# ---------------------------------------------------------------------------


def test_register_new_key(tmp_path):
    store = PluginKeyStore(str(tmp_path))
    _, pem = _keypair()
    assert store.register("autonomon", pem) == "registered"
    assert store.get_public_key("autonomon") is not None


def test_register_same_key_is_idempotent(tmp_path):
    store = PluginKeyStore(str(tmp_path))
    _, pem = _keypair()
    assert store.register("autonomon", pem) == "registered"
    assert store.register("autonomon", pem) == "exists"


def test_register_different_key_conflicts(tmp_path):
    store = PluginKeyStore(str(tmp_path))
    _, pem1 = _keypair()
    _, pem2 = _keypair()
    store.register("autonomon", pem1)
    with pytest.raises(KeyConflict):
        store.register("autonomon", pem2)


def test_register_invalid_pem_raises(tmp_path):
    store = PluginKeyStore(str(tmp_path))
    with pytest.raises(PluginAuthError):
        store.register("autonomon", "not a pem")


def test_register_rsa_key_rejected(tmp_path):
    # A valid PEM public key that is not Ed25519 must be rejected.
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_pub = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    store = PluginKeyStore(str(tmp_path))
    with pytest.raises(PluginAuthError):
        store.register("autonomon", rsa_pub)


@pytest.mark.parametrize("bad", ["../escape", "Has Space", "UPPER", "with/slash", ""])
def test_invalid_plugin_names_rejected(tmp_path, bad):
    store = PluginKeyStore(str(tmp_path))
    _, pem = _keypair()
    with pytest.raises(InvalidPluginName):
        store.register(bad, pem)


def test_get_unregistered_returns_none(tmp_path):
    store = PluginKeyStore(str(tmp_path))
    assert store.get_public_key("autonomon") is None


# ---------------------------------------------------------------------------
# ChallengeStore
# ---------------------------------------------------------------------------


def test_challenge_issue_and_consume():
    cs = ChallengeStore(ttl_seconds=10)
    nonce, ttl = cs.issue("autonomon")
    assert ttl == 10
    assert cs.consume("autonomon", nonce) is True


def test_challenge_is_single_use():
    cs = ChallengeStore(ttl_seconds=10)
    nonce, _ = cs.issue("autonomon")
    assert cs.consume("autonomon", nonce) is True
    assert cs.consume("autonomon", nonce) is False


def test_challenge_wrong_nonce_rejected():
    cs = ChallengeStore(ttl_seconds=10)
    cs.issue("autonomon")
    assert cs.consume("autonomon", "wrong") is False


def test_challenge_expired_rejected():
    cs = ChallengeStore(ttl_seconds=0.0)
    nonce, _ = cs.issue("autonomon")
    time.sleep(0.01)
    assert cs.consume("autonomon", nonce) is False


def test_challenge_unknown_plugin_rejected():
    cs = ChallengeStore()
    assert cs.consume("nobody", "x") is False


def test_challenge_multiple_outstanding_per_plugin():
    # Concurrent acquisition: two issued nonces for the same plugin must NOT
    # evict each other — both remain independently consumable.
    cs = ChallengeStore(ttl_seconds=10)
    n1, _ = cs.issue("autonomon")
    n2, _ = cs.issue("autonomon")
    assert n1 != n2
    assert cs.consume("autonomon", n1) is True
    assert cs.consume("autonomon", n2) is True


def test_challenge_nonce_bound_to_plugin():
    # A nonce issued for one plugin cannot be consumed under another plugin name.
    cs = ChallengeStore(ttl_seconds=10)
    nonce, _ = cs.issue("autonomon")
    assert cs.consume("other", nonce) is False


def test_challenge_caps_outstanding_nonces():
    cs = ChallengeStore(ttl_seconds=100, max_outstanding=3)
    nonces = [cs.issue("autonomon")[0] for _ in range(5)]
    # Never exceeds the cap; the oldest were evicted.
    assert len(cs._pending) == 3
    # The last 3 issued are still valid; the first 2 were evicted.
    assert cs.consume("autonomon", nonces[-1]) is True
    assert cs.consume("autonomon", nonces[0]) is False


# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------


def test_verify_signature_valid():
    priv, _ = _keypair()
    msg = b"some-nonce"
    sig = priv.sign(msg)
    assert verify_signature(priv.public_key(), msg, sig) is True


def test_verify_signature_invalid():
    priv, _ = _keypair()
    other, _ = _keypair()
    msg = b"some-nonce"
    sig = other.sign(msg)
    assert verify_signature(priv.public_key(), msg, sig) is False


# ---------------------------------------------------------------------------
# Route flow
# ---------------------------------------------------------------------------


@pytest.fixture
def plugin_client(tmp_path):
    """Device-auth app with an isolated key store and a loopback client."""
    with patch.dict(
        os.environ,
        {"NOMON_DEVICE_AUTH": "true", "NOMON_API_MODE": "device"},
        clear=False,
    ):
        app = create_app()
        # Replace the default (/var/lib/...) store with a tmp one for the test.
        app.state.plugin_key_store = PluginKeyStore(str(tmp_path))
        app.state.plugin_challenge_store = ChallengeStore()
        client = TestClient(app, client=("127.0.0.1", 50000))
        yield client, app


@pytest.fixture
def remote_client(tmp_path):
    """Same app, but requests appear to come from a non-loopback address."""
    with patch.dict(
        os.environ,
        {"NOMON_DEVICE_AUTH": "true", "NOMON_API_MODE": "device"},
        clear=False,
    ):
        app = create_app()
        app.state.plugin_key_store = PluginKeyStore(str(tmp_path))
        app.state.plugin_challenge_store = ChallengeStore()
        client = TestClient(app, client=("10.0.0.5", 40000))
        yield client, app


def _register(client, plugin, pem):
    return client.post("/api/plugin/register", json={"plugin": plugin, "public_key": pem})


def test_register_from_localhost_ok(plugin_client):
    client, _ = plugin_client
    _, pem = _keypair()
    resp = _register(client, "autonomon", pem)
    assert resp.status_code == 200
    assert resp.json()["status"] == "registered"


def test_register_from_remote_forbidden(remote_client):
    client, _ = remote_client
    _, pem = _keypair()
    resp = _register(client, "autonomon", pem)
    assert resp.status_code == 403


def test_register_conflict_returns_409(plugin_client):
    client, _ = plugin_client
    _, pem1 = _keypair()
    _, pem2 = _keypair()
    _register(client, "autonomon", pem1)
    resp = _register(client, "autonomon", pem2)
    assert resp.status_code == 409


def test_challenge_from_remote_forbidden_by_default(remote_client, monkeypatch):
    """challenge/token are localhost-only unless remote plugin auth is enabled (S-12)."""
    monkeypatch.delenv("NOMON_PLUGIN_AUTH_ALLOW_REMOTE", raising=False)
    client, _ = remote_client
    assert client.get("/api/plugin/challenge", params={"plugin": "autonomon"}).status_code == 403
    resp = client.post(
        "/api/plugin/token", json={"plugin": "autonomon", "nonce": "x", "signature": "eA=="}
    )
    assert resp.status_code == 403


def test_challenge_from_remote_allowed_when_enabled(remote_client, monkeypatch):
    monkeypatch.setenv("NOMON_PLUGIN_AUTH_ALLOW_REMOTE", "1")
    client, _ = remote_client
    # Passes the loopback gate; 404 because nothing is registered.
    assert client.get("/api/plugin/challenge", params={"plugin": "autonomon"}).status_code == 404


def test_plugin_auth_endpoints_rate_limited(plugin_client):
    client, _ = plugin_client
    _, pem = _keypair()
    _register(client, "autonomon", pem)
    codes = [
        client.get("/api/plugin/challenge", params={"plugin": "autonomon"}).status_code
        for _ in range(31)
    ]
    assert codes[:30] == [200] * 30
    assert codes[30] == 429


def test_challenge_unregistered_404(plugin_client):
    client, _ = plugin_client
    resp = client.get("/api/plugin/challenge", params={"plugin": "autonomon"})
    assert resp.status_code == 404


def test_full_token_flow_grants_device_access(plugin_client):
    client, _ = plugin_client
    priv, pem = _keypair()
    _register(client, "autonomon", pem)

    ch = client.get("/api/plugin/challenge", params={"plugin": "autonomon"})
    assert ch.status_code == 200
    nonce = ch.json()["nonce"]

    tok = client.post(
        "/api/plugin/token",
        json={"plugin": "autonomon", "nonce": nonce, "signature": _sign(priv, nonce)},
    )
    assert tok.status_code == 200
    body = tok.json()
    access_token = body["access_token"]
    assert access_token
    assert body["timestamp"]  # all REST responses carry a UTC timestamp

    headers = {"Authorization": f"Bearer {access_token}"}

    # The plugin token authenticates, but is *plugin*-scoped (review S-2): raw
    # I/O routes pass auth (here 503 — no HAT in tests — proves we got past the
    # 401/403 gate), while owner-only and control routes are refused with 403.
    ultrasonic = client.get("/api/sensor/ultrasonic", headers=headers)
    assert ultrasonic.status_code not in (401, 403)
    drive = client.post("/api/drive", json={"speed_pct": 10}, headers=headers)
    assert drive.status_code not in (401, 403)
    events = client.post("/api/routines/explore/events", json={"type": "starting"}, headers=headers)
    assert events.status_code == 200

    assert client.get("/api/device/auth/me", headers=headers).status_code == 403
    assert client.delete("/api/device/auth/session", headers=headers).status_code == 403
    assert client.get("/api/device/auth/identity", headers=headers).status_code == 403
    assert (
        client.post("/api/routines/start", json={"routine": "explore"}, headers=headers).status_code
        == 403
    )
    assert client.post("/api/routines/stop-all", headers=headers).status_code == 403
    assert client.get("/api/ai/key", headers=headers).status_code == 403
    assert (
        client.post("/api/device/wifi/ap", json={"enabled": True}, headers=headers).status_code
        == 403
    )


def test_token_bad_signature_rejected(plugin_client):
    client, _ = plugin_client
    priv, pem = _keypair()
    other, _ = _keypair()
    _register(client, "autonomon", pem)

    nonce = client.get("/api/plugin/challenge", params={"plugin": "autonomon"}).json()["nonce"]
    tok = client.post(
        "/api/plugin/token",
        json={"plugin": "autonomon", "nonce": nonce, "signature": _sign(other, nonce)},
    )
    assert tok.status_code == 401


def test_token_replayed_nonce_rejected(plugin_client):
    client, _ = plugin_client
    priv, pem = _keypair()
    _register(client, "autonomon", pem)

    nonce = client.get("/api/plugin/challenge", params={"plugin": "autonomon"}).json()["nonce"]
    sig = _sign(priv, nonce)
    first = client.post(
        "/api/plugin/token",
        json={"plugin": "autonomon", "nonce": nonce, "signature": sig},
    )
    assert first.status_code == 200
    replay = client.post(
        "/api/plugin/token",
        json={"plugin": "autonomon", "nonce": nonce, "signature": sig},
    )
    assert replay.status_code == 401


def test_token_unregistered_plugin_rejected(plugin_client):
    client, _ = plugin_client
    priv, _ = _keypair()
    # No registration. Challenge would 404, but a forged nonce must also fail.
    tok = client.post(
        "/api/plugin/token",
        json={"plugin": "ghost", "nonce": "forged", "signature": _sign(priv, "forged")},
    )
    assert tok.status_code == 401


# ---------------------------------------------------------------------------
# Token scope (review finding S-2)
# ---------------------------------------------------------------------------


def test_plugin_token_carries_plugin_scope():
    from nomothetic.auth import SCOPE_OWNER, SCOPE_PLUGIN, AuthService

    svc = AuthService(secret="x" * 32)
    plugin = svc.verify_token(svc.create_plugin_token("autonomon"))
    assert plugin.scope == SCOPE_PLUGIN
    assert plugin.sub == "plugin:autonomon"
    owner = svc.verify_token(svc.create_access_token("owner@local"))
    assert owner.scope == SCOPE_OWNER


def test_scope_inferred_for_legacy_tokens_without_claim():
    """A pre-scope plugin token must not be upgraded to owner on deploy."""
    from datetime import datetime, timedelta, timezone

    from authlib.jose import JsonWebToken

    from nomothetic.auth import SCOPE_OWNER, SCOPE_PLUGIN, AuthService

    secret = "y" * 32
    svc = AuthService(secret=secret)
    now = datetime.now(timezone.utc)
    jwt = JsonWebToken(["HS256"])

    def mint(sub: str) -> str:
        payload = {
            "sub": sub,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "iss": "nomon-central",
        }
        tok = jwt.encode({"alg": "HS256"}, payload, secret)
        return tok.decode() if isinstance(tok, bytes) else tok

    assert svc.verify_token(mint("plugin:autonomon")).scope == SCOPE_PLUGIN
    assert svc.verify_token(mint("owner@local")).scope == SCOPE_OWNER


def test_unknown_scope_rejected():
    from datetime import datetime, timedelta, timezone

    from authlib.jose import JsonWebToken

    from nomothetic.auth import AuthService

    secret = "z" * 32
    svc = AuthService(secret=secret)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "owner@local",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "iss": "nomon-central",
        "scope": "admin",
    }
    tok = JsonWebToken(["HS256"]).encode({"alg": "HS256"}, payload, secret)
    tok = tok.decode() if isinstance(tok, bytes) else tok
    with pytest.raises(ValueError):
        svc.verify_token(tok)


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("GET", "/api/sensor/ultrasonic", True),
        ("GET", "/api/sensor/grayscale/normalized", True),
        ("GET", "/api/hat/battery", True),
        ("GET", "/api/camera/frame", True),
        ("POST", "/api/drive", True),
        ("POST", "/api/steer", True),
        ("POST", "/api/hat/motor/stop", True),
        ("POST", "/api/camera/pan", True),
        ("POST", "/api/camera/tilt", True),
        ("POST", "/api/routines/explore/events", True),
        ("POST", "/api/routines/explore/events/", True),
        ("POST", "/api/routines/start", False),
        ("POST", "/api/routines/stop", False),
        ("POST", "/api/routines/heartbeat", False),
        ("GET", "/api/routines/explore/logs", False),
        ("POST", "/api/routines//events", False),
        ("POST", "/api/sensor/ultrasonic", False),
        ("GET", "/api/drive", False),
        ("POST", "/api/hat/motor/0", False),
        ("POST", "/api/hat/reset", False),
        ("PUT", "/api/ai/key", False),
        ("POST", "/api/device/network/configure", False),
        ("DELETE", "/api/device/auth/session", False),
    ],
)
def test_plugin_allow_list(method, path, allowed):
    from nomothetic.auth import _plugin_may_call

    assert _plugin_may_call(method, path) is allowed


def test_owner_token_unaffected_by_scope_gate(plugin_client):
    """An owner token still reaches control routes on the device router."""
    from nomothetic.auth import get_auth_service

    client, _ = plugin_client
    svc = get_auth_service()
    assert svc is not None
    headers = {"Authorization": f"Bearer {svc.create_access_token('owner@local')}"}
    # 503 (routine manager has no credentials in tests) proves the gate passed.
    resp = client.post("/api/routines/start", json={"routine": "explore"}, headers=headers)
    assert resp.status_code not in (401, 403)
    assert client.get("/api/ai/key", headers=headers).status_code not in (401, 403)
