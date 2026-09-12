"""Shared pytest fixtures for the nomothetic test suite.

Every persistent-state path is redirected to a per-test temporary directory.
Without this, tests that construct ``PairingState`` / ``DeviceJwtSecretStore``
/ ``PluginKeyStore`` / ``AiKeyStore`` with default paths silently read and
**write** the real device state under ``/var/lib/nomon`` whenever that
directory exists and is writable — which is exactly the situation during an
on-device deploy's ``make test`` run. That clobbered the Pi's pairing secret
(the Wi-Fi Soft AP passphrase) and rotated the device JWT signing secret
(invalidating every issued token) on every deploy. On dev machines and CI,
``/var/lib/nomon`` usually does not exist, so the stray writes no-op'd and the
bug stayed invisible.

Tests that need a specific path still win: setting the env var inside the
test (``patch.dict`` / ``monkeypatch``) overrides this fixture's value, and
tests that clear the environment see the built-in defaults as before.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_device_state_paths(tmp_path, monkeypatch):
    """Point every persistent-state env var at this test's tmp directory."""
    state_dir = tmp_path / "nomon-state"
    state_dir.mkdir()
    monkeypatch.setenv("NOMON_PAIRING_SECRET_PATH", str(state_dir / "pairing_secret"))
    monkeypatch.setenv("NOMON_DEVICE_JWT_SECRET_PATH", str(state_dir / "device_jwt_secret"))
    monkeypatch.setenv("NOMON_AI_API_KEY_PATH", str(state_dir / "ai_api_key"))
    monkeypatch.setenv("NOMON_PLUGIN_KEY_DIR", str(state_dir / "plugin_keys"))
    monkeypatch.setenv("NOMON_ROUTINE_CATALOG_PATH", str(state_dir / "routine_catalog.json"))
    # A configured wake phrase on the host (device deploys export it) would
    # make every lifespan-running test spawn a real wake-word listener that
    # grabs the USB microphone — keep the feature off unless a test opts in.
    monkeypatch.delenv("NOMON_WAKE_PHRASE", raising=False)
    monkeypatch.delenv("NOMON_WAKE_PHRASE_VARIANTS", raising=False)
    monkeypatch.delenv("NOMON_WAKE_INPUT_INDEX", raising=False)
    monkeypatch.delenv("NOMON_WAKE_INPUT_NAME", raising=False)
    monkeypatch.delenv("NOMON_WAKE_OUTPUT_INDEX", raising=False)
    monkeypatch.delenv("NOMON_WAKE_OUTPUT_NAME", raising=False)
    # AudioRecorder/AudioPlayer read the same style of device overrides; host
    # values (device deploys export them) must not leak into the test suite.
    monkeypatch.delenv("NOMON_AUDIO_INPUT_INDEX", raising=False)
    monkeypatch.delenv("NOMON_AUDIO_INPUT_NAME", raising=False)
    monkeypatch.delenv("NOMON_AUDIO_OUTPUT_INDEX", raising=False)
    monkeypatch.delenv("NOMON_AUDIO_OUTPUT_NAME", raising=False)
