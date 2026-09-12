"""Tests for the wake-word HTTP endpoints (/api/voice/wake) and app wiring.

The routes are exercised with device auth disabled and (where lifecycle
control matters) a fake listener injected into ``app.state`` — asserting HTTP
behaviour without threads, pyaudio, or vosk.  The listener itself is covered
in test_wake.py.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from nomothetic.api import create_app
from nomothetic.wake import WakeWordListener


class FakeListener:
    """In-memory stand-in for WakeWordListener used by the route tests."""

    def __init__(self, phrase=""):
        self.phrase = phrase
        self.variants = []
        self.enabled = False
        self.is_running = False
        self.is_paused = False
        self.state = "idle"
        self.followup_window_s = 8.0
        self.start_result = True
        self.calls = []
        self.attached_loop = None

    def attach_loop(self, loop):
        self.calls.append("attach_loop")
        self.attached_loop = loop

    def start_background(self, loop=None):
        self.calls.append("start")
        if self.start_result:
            self.enabled = True
            self.is_running = True
            self.state = "listening"
        return self.start_result

    def stop(self):
        self.calls.append("stop")
        self.enabled = False
        self.is_running = False
        self.state = "stopped"

    def unload_model(self):
        self.calls.append("unload_model")
        return True

    def set_phrase_config(self, phrase=None, variants=None):
        self.calls.append("set_config")
        if phrase is not None:
            self.phrase = " ".join(phrase.lower().split())
        if variants is not None:
            self.variants = list(variants)

    def pause(self, timeout_s=3.0):
        self.calls.append("pause")
        self.is_paused = True

    def resume(self):
        self.calls.append("resume")
        self.is_paused = False


@pytest.fixture
def app():
    """Device-mode app with auth disabled (wake env cleared by conftest)."""
    with patch.dict(
        os.environ,
        {"NOMON_DEVICE_AUTH": "false", "NOMON_API_MODE": "device"},
        clear=False,
    ):
        return create_app()


@pytest.fixture
def client(app):
    return TestClient(app)


# ============================================================================
# Wiring
# ============================================================================


def test_create_app_constructs_wake_listener(app):
    """The listener is always constructed in device mode (side-effect free)."""
    listener = app.state.wake_listener
    assert isinstance(listener, WakeWordListener)
    assert listener.phrase == ""  # conftest clears NOMON_WAKE_PHRASE
    assert not listener.enabled


def test_lifespan_attaches_loop_and_starts_when_phrase_set(app):
    fake = FakeListener(phrase="hey nomon")
    app.state.wake_listener = fake
    with TestClient(app):
        assert fake.calls[:2] == ["attach_loop", "start"]
        assert fake.attached_loop is not None
    assert fake.calls[-1] == "stop"


def test_lifespan_skips_start_without_phrase(app):
    fake = FakeListener(phrase="")
    app.state.wake_listener = fake
    with TestClient(app):
        assert "start" not in fake.calls
        assert "attach_loop" in fake.calls
    assert fake.calls[-1] == "stop"


# ============================================================================
# GET /api/voice/wake
# ============================================================================


def test_get_status_default(client):
    resp = client.get("/api/voice/wake")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["running"] is False
    assert body["paused"] is False
    assert body["state"] == "idle"
    assert body["phrase"] is None
    assert body["variants"] == []
    assert body["timestamp"]


def test_get_status_503_when_listener_missing(app):
    app.state.wake_listener = None
    client = TestClient(app)
    resp = client.get("/api/voice/wake")
    assert resp.status_code == 503


# ============================================================================
# PUT /api/voice/wake
# ============================================================================


def test_put_updates_phrase_and_variants_while_disabled(client):
    resp = client.put(
        "/api/voice/wake",
        json={"phrase": "  Hey  Robot ", "variants": ["hey row bot"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["phrase"] == "hey robot"
    assert body["variants"] == ["hey row bot"]
    assert body["enabled"] is False
    assert body["running"] is False


def test_put_enable_without_phrase_returns_400(client):
    resp = client.put("/api/voice/wake", json={"enabled": True})
    assert resp.status_code == 400
    # The app-wide handler reshapes HTTPException detail into "error".
    assert "phrase" in resp.json()["error"].lower()


def test_put_enable_without_audio_stack_returns_503(client):
    with patch("nomothetic.wake._PYAUDIO_AVAILABLE", False):
        resp = client.put("/api/voice/wake", json={"enabled": True, "phrase": "hey robot"})
    assert resp.status_code == 503


def test_put_enable_and_disable_with_fake_listener(app):
    fake = FakeListener(phrase="hey nomon")
    app.state.wake_listener = fake
    client = TestClient(app)

    resp = client.put("/api/voice/wake", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["running"] is True
    assert fake.calls == ["start"]

    resp = client.put("/api/voice/wake", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["running"] is False
    # Disabling also releases the shared Vosk model's memory.
    assert fake.calls == ["start", "stop", "unload_model"]


def test_put_config_change_restarts_running_listener(app):
    fake = FakeListener(phrase="hey nomon")
    fake.enabled = True
    fake.is_running = True
    app.state.wake_listener = fake
    client = TestClient(app)

    resp = client.put("/api/voice/wake", json={"phrase": "hey robot"})
    assert resp.status_code == 200
    # A config-change restart must NOT unload the model — it's about to be
    # needed again immediately, unlike an actual disable.
    assert fake.calls == ["stop", "set_config", "start"]
    assert resp.json()["phrase"] == "hey robot"
    assert resp.json()["running"] is True


def test_put_disable_from_stopped_state_still_unloads(app):
    """Disabling an already-idle listener still releases the model (idempotent)."""
    fake = FakeListener(phrase="hey nomon")
    app.state.wake_listener = fake
    client = TestClient(app)

    resp = client.put("/api/voice/wake", json={"enabled": False})
    assert resp.status_code == 200
    assert fake.calls == ["stop", "unload_model"]


def test_put_empty_body_is_a_noop_status(app):
    fake = FakeListener(phrase="hey nomon")
    app.state.wake_listener = fake
    client = TestClient(app)
    resp = client.put("/api/voice/wake", json={})
    assert resp.status_code == 200
    # Disabled target -> stop is called defensively; no config or start calls.
    assert "set_config" not in fake.calls and "start" not in fake.calls


@pytest.mark.parametrize(
    "body",
    [
        {"phrase": "x" * 65},
        {"variants": ["ok"] * 9},
        {"variants": [""]},
    ],
)
def test_put_validation_rejects_bad_payloads(client, body):
    resp = client.put("/api/voice/wake", json=body)
    assert resp.status_code == 422


# ============================================================================
# Audio-record contention hooks
# ============================================================================


def test_record_start_pauses_and_stop_resumes_listener(app):
    import nomothetic.api

    fake = FakeListener(phrase="hey nomon")
    app.state.wake_listener = fake
    client = TestClient(app)

    mock_recorder = MagicMock()
    mock_recorder.is_recording = False
    mock_recorder.start.return_value = "/tmp/recording_001.wav"
    mock_recorder.stop.return_value = "/tmp/recording_001.wav"
    nomothetic.api._audio_recorder = mock_recorder
    try:
        resp = client.post("/api/audio/record/start", json={})
        assert resp.status_code == 200
        assert "pause" in fake.calls and "resume" not in fake.calls

        resp = client.post("/api/audio/record/stop")
        assert resp.status_code == 200
        assert fake.calls[-1] == "resume"
    finally:
        nomothetic.api._audio_recorder = None


def test_record_start_failure_resumes_listener(app):
    import nomothetic.api

    fake = FakeListener(phrase="hey nomon")
    app.state.wake_listener = fake
    client = TestClient(app)

    mock_recorder = MagicMock()
    mock_recorder.is_recording = False
    mock_recorder.start.side_effect = ValueError("filename cannot contain path separators")
    nomothetic.api._audio_recorder = mock_recorder
    try:
        resp = client.post("/api/audio/record/start", json={"filename": "bad"})
        assert resp.status_code == 400
        assert fake.calls == ["pause", "resume"]
    finally:
        nomothetic.api._audio_recorder = None
