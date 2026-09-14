"""Tests for workspace-review 2026-09-13 hardening items S-3, S-10, S-11, S-14.

Complements the per-module suites; groups the cross-cutting checks so the
review's §7 status table has one place to point at.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nomothetic.device_identity import DeviceIdentity, verify_registration_proof
from nomothetic.routine_manager import validate_routine_params

# ---------------------------------------------------------------------------
# S-10 — routine params allow-list
# ---------------------------------------------------------------------------

_SCHEMA = {
    "obstacle_threshold_cm": {"type": "number", "default": 40.0},
    "cliff_detection": {"type": "boolean", "default": True},
    "planner": {"type": "string", "default": "avoidance"},
}


def test_params_scalars_only_without_schema():
    validate_routine_params({"speed": 1, "name": "x", "flag": True, "none": None}, {})
    with pytest.raises(ValueError, match="scalar"):
        validate_routine_params({"nested": {"a": 1}}, {})
    with pytest.raises(ValueError, match="scalar"):
        validate_routine_params({"items": [1, 2]}, {})


def test_params_key_shape():
    with pytest.raises(ValueError, match="param name"):
        validate_routine_params({"Bad-Key": 1}, {})
    with pytest.raises(ValueError, match="param name"):
        validate_routine_params({"../x": 1}, {})
    with pytest.raises(ValueError, match="'routine'"):
        validate_routine_params({"routine": "explore"}, {})


def test_params_string_length_cap():
    validate_routine_params({"planner": "x" * 512}, {})
    with pytest.raises(ValueError, match="too long"):
        validate_routine_params({"planner": "x" * 513}, {})


def test_params_must_be_declared_when_schema_published():
    validate_routine_params({"obstacle_threshold_cm": 25, "cliff_detection": False}, _SCHEMA)
    with pytest.raises(ValueError, match="unknown routine param"):
        validate_routine_params({"model_path": "/etc/passwd"}, _SCHEMA)


def test_params_types_checked_against_schema():
    with pytest.raises(ValueError, match="must be number"):
        validate_routine_params({"obstacle_threshold_cm": "25"}, _SCHEMA)
    with pytest.raises(ValueError, match="must be number"):
        validate_routine_params({"obstacle_threshold_cm": True}, _SCHEMA)
    with pytest.raises(ValueError, match="must be boolean"):
        validate_routine_params({"cliff_detection": 1}, _SCHEMA)
    with pytest.raises(ValueError, match="must be string"):
        validate_routine_params({"planner": 3}, _SCHEMA)


@pytest.mark.asyncio
async def test_routine_start_rejects_undeclared_param(tmp_path):
    """RoutineManager.start reads the published schema and refuses unknown keys."""
    import json

    from nomothetic.routine_manager import RoutineManager, RoutineManagerConfig

    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "routines": ["explore"],
                "params_schema": _SCHEMA,
                "autonomon_bin": "/nonexistent/nomon-autonomon",
            }
        )
    )
    launched: list[list[str]] = []

    class _Proc:
        pid = 1
        returncode = None

        async def wait(self):
            return 0

        def send_signal(self, sig):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

    async def launcher(argv, env):
        launched.append(argv)
        return _Proc()

    manager = RoutineManager(
        RoutineManagerConfig(plugin_token="t", routine_catalog_path=catalog), launcher=launcher
    )
    with pytest.raises(ValueError, match="unknown routine param"):
        await manager.start("explore", {"rules_path": "/etc/passwd"})
    assert launched == []
    await manager.start("explore", {"obstacle_threshold_cm": 30})
    assert len(launched) == 1
    await manager.stop_all()


# ---------------------------------------------------------------------------
# S-11 — AI tool allow-list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ai_service_allowed_tools_filters_and_refuses():
    from nomothetic.ai_command import MOTION_TOOLS, AiCommandService

    seen: dict[str, object] = {}

    class _Block:
        type = "tool_use"
        id = "t1"

        def __init__(self, name, inp):
            self.name = name
            self.input = inp

    class _Resp:
        def __init__(self, stop_reason, content):
            self.stop_reason = stop_reason
            self.content = content

    class _Messages:
        def __init__(self):
            self.calls = 0

        async def create(self, **kwargs):
            seen["tools"] = [t["name"] for t in kwargs["tools"]]
            self.calls += 1
            if self.calls == 1:
                return _Resp("tool_use", [_Block("drive", {"speed_pct": 20})])

            class _Text:
                type = "text"
                text = "done"

            return _Resp("end_turn", [_Text()])

    class _Client:
        def __init__(self):
            self.messages = _Messages()

        async def close(self):
            pass

    async def _hat_call(*args, **kwargs):
        return {"ok": True}

    service = AiCommandService(hat_call=_hat_call, client_factory=lambda k: _Client())
    allowed = set(service.tool_names) - MOTION_TOOLS
    result = await service.run_command(
        [{"role": "user", "content": "drive"}], api_key="k", allowed_tools=allowed
    )
    assert "drive" not in seen["tools"] and "stop" in seen["tools"]
    assert result["actions"][0]["ok"] is False
    assert "not available" in result["actions"][0]["error"]


# ---------------------------------------------------------------------------
# S-14 — refresh endpoints are rate limited
# ---------------------------------------------------------------------------


def test_device_refresh_rate_limited():
    with patch.dict(os.environ, {"NOMON_DEVICE_AUTH": "true", "NOMON_API_MODE": "device"}):
        from nomothetic.api import create_app

        client = TestClient(create_app())
    codes = [
        client.post("/api/device/auth/refresh", json={"refresh_token": "nope"}).status_code
        for _ in range(21)
    ]
    assert set(codes[:20]) == {401}
    assert codes[20] == 429


# ---------------------------------------------------------------------------
# S-3 — device identity + verified, pinned registration proofs
# ---------------------------------------------------------------------------


def test_device_identity_persists_and_signs(tmp_path):
    path = tmp_path / "device_identity.key"
    ident = DeviceIdentity(str(path))
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    again = DeviceIdentity(str(path))
    assert again.public_key_pem == ident.public_key_pem
    proof = ident.create_registration_proof("VIN-1")
    assert verify_registration_proof(proof, "VIN-1", ident.public_key_pem)
    assert not verify_registration_proof(proof, "VIN-2", ident.public_key_pem)
    other = DeviceIdentity(str(tmp_path / "other.key"))
    assert not verify_registration_proof(proof, "VIN-1", other.public_key_pem)
    assert not verify_registration_proof("garbage", "VIN-1", ident.public_key_pem)


def test_device_identity_survives_missing_dir(tmp_path):
    ident = DeviceIdentity(str(tmp_path / "missing" / "k.key"))
    assert ident.create_registration_proof("V")


def test_identity_endpoint_returns_verifiable_proof(tmp_path):
    with patch.dict(
        os.environ,
        {
            "NOMON_DEVICE_AUTH": "true",
            "NOMON_API_MODE": "device",
            "NOMON_DEVICE_IDENTITY_PATH": str(tmp_path / "id.key"),
            "NOMON_PAIRING_SECRET_PATH": str(tmp_path / "ps"),
            "NOMON_AP_PASSPHRASE_PATH": str(tmp_path / "ap"),
            "NOMON_DEVICE_ID": "VIN-TEST-3",
        },
    ):
        from nomothetic.api import create_app

        app = create_app()
        client = TestClient(app)
        secret = app.state.pairing_state.get_active_secret()
        tokens = client.post(
            "/api/device/auth/pair", json={"secret": secret, "display_name": "o"}
        ).json()
        resp = client.get(
            "/api/device/auth/identity",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["device_public_key"].startswith("-----BEGIN PUBLIC KEY-----")
    assert verify_registration_proof(
        data["registration_proof"], "VIN-TEST-3", data["device_public_key"]
    )


@pytest.fixture
def central(tmp_path):
    with patch.dict(
        os.environ,
        {
            "NOMON_API_MODE": "central",
            "NOMON_JWT_SECRET": "test-secret-key-that-is-at-least-32-bytes-long!",
            "NOMON_FLEET_REQUIRE_DEVICE_KEY": "",
        },
    ):
        from nomothetic.api import create_app

        yield TestClient(create_app())


def _central_token(client, email):
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123", "display_name": "u"},
    )
    return resp.json()["access_token"]


def test_registration_verifies_and_pins_device_key(central, tmp_path, monkeypatch):
    monkeypatch.delenv("NOMON_REGISTRATION_INVITE_CODE", raising=False)
    device = DeviceIdentity(str(tmp_path / "dev.key"))
    attacker = DeviceIdentity(str(tmp_path / "atk.key"))
    alice = _central_token(central, "alice@example.com")
    mallory = _central_token(central, "mallory@example.com")

    # Wrong signature for the supplied key → refused.
    bad = central.post(
        "/api/fleet/devices",
        json={
            "vin": "VIN-9",
            "model": "nomon",
            "registration_proof": attacker.create_registration_proof("VIN-9"),
            "device_public_key": device.public_key_pem,
        },
        headers={"Authorization": f"Bearer {alice}"},
    )
    assert bad.status_code == 400

    ok = central.post(
        "/api/fleet/devices",
        json={
            "vin": "VIN-9",
            "model": "nomon",
            "registration_proof": device.create_registration_proof("VIN-9"),
            "device_public_key": device.public_key_pem,
        },
        headers={"Authorization": f"Bearer {alice}"},
    )
    assert ok.status_code == 201

    # The key is now pinned: another user with their own key cannot claim VIN-9,
    # even with a self-consistent proof + key pair (legacy structural path is
    # bypassed because a pinned key exists).
    squat = central.post(
        "/api/fleet/devices",
        json={
            "vin": "VIN-9",
            "model": "nomon",
            "registration_proof": attacker.create_registration_proof("VIN-9"),
            "device_public_key": attacker.public_key_pem,
        },
        headers={"Authorization": f"Bearer {mallory}"},
    )
    assert squat.status_code == 400
    squat_legacy = central.post(
        "/api/fleet/devices",
        json={
            "vin": "VIN-9",
            "model": "nomon",
            "registration_proof": attacker.create_registration_proof("VIN-9"),
        },
        headers={"Authorization": f"Bearer {mallory}"},
    )
    assert squat_legacy.status_code == 400


def test_registration_requires_key_when_configured(central, monkeypatch):
    monkeypatch.delenv("NOMON_REGISTRATION_INVITE_CODE", raising=False)
    monkeypatch.setenv("NOMON_FLEET_REQUIRE_DEVICE_KEY", "1")
    token = _central_token(central, "bob@example.com")
    from tests.test_central import _make_registration_proof

    resp = central.post(
        "/api/fleet/devices",
        json={
            "vin": "VIN-L",
            "model": "nomon",
            "registration_proof": _make_registration_proof("VIN-L"),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "device_public_key" in resp.text
