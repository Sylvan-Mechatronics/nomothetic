"""Tests for the MQTT telemetry publisher module.

Tests mock ``paho.mqtt.client`` so no real broker is required and
paho-mqtt does not need to be installed in the test environment.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared helpers & fixtures
# ---------------------------------------------------------------------------


def _make_mock_mqtt():
    """Return a MagicMock module that looks enough like paho.mqtt.client."""
    mock_mqtt = MagicMock()

    # Make CallbackAPIVersion available on the mock module.
    mock_enum = MagicMock()
    mock_mqtt.enums = MagicMock()
    mock_mqtt.enums.CallbackAPIVersion = mock_enum

    # publish() returns an object with wait_for_publish().
    mock_result = MagicMock()
    mock_result.wait_for_publish = MagicMock()
    mock_client_instance = MagicMock()
    mock_client_instance.publish.return_value = mock_result
    mock_mqtt.Client.return_value = mock_client_instance

    return mock_mqtt, mock_client_instance, mock_result


@pytest.fixture
def mock_mqtt_module():
    """Patch nomothetic.telemetry.mqtt and CallbackAPIVersion with safe mocks."""
    mock_mqtt, mock_client_instance, mock_result = _make_mock_mqtt()
    with (
        patch("nomothetic.telemetry.mqtt", mock_mqtt),
        patch("nomothetic.telemetry.CallbackAPIVersion", mock_mqtt.enums.CallbackAPIVersion),
    ):
        yield mock_mqtt, mock_client_instance, mock_result


@pytest.fixture
def publisher(mock_mqtt_module):
    """A TelemetryPublisher with mocked paho-mqtt."""
    from nomothetic.telemetry import TelemetryPublisher

    return TelemetryPublisher(broker="localhost")


@pytest.fixture
def mock_camera():
    """A minimal mock Camera for payload tests."""
    cam = MagicMock()
    cam.width = 1280
    cam.height = 720
    cam.fps = 30
    cam.encoder = "h264"
    cam._is_recording = False
    return cam


# ---------------------------------------------------------------------------
# 1. Constructor & defaults
# ---------------------------------------------------------------------------


def test_constructor_defaults(mock_mqtt_module):
    """Default parameters are applied correctly."""
    from nomothetic.telemetry import TelemetryPublisher

    pub = TelemetryPublisher(broker="broker.local")
    assert pub.broker == "broker.local"
    assert pub.port == 1883
    assert pub.topic == "nomon/telemetry"
    assert pub.interval == 30.0
    assert pub.qos == 1
    assert pub.camera is None


def test_constructor_custom_params(mock_mqtt_module):
    """Custom constructor parameters are stored."""
    from nomothetic.telemetry import TelemetryPublisher

    pub = TelemetryPublisher(
        broker="10.0.0.1",
        port=8883,
        topic="fleet/status",
        interval=10.0,
        qos=0,
    )
    assert pub.port == 8883
    assert pub.topic == "fleet/status"
    assert pub.interval == 10.0
    assert pub.qos == 0


def test_constructor_explicit_device_id(mock_mqtt_module):
    """Explicit device_id overrides auto-detection."""
    from nomothetic.telemetry import TelemetryPublisher

    pub = TelemetryPublisher(broker="localhost", device_id="my-device")
    assert pub.device_id == "my-device"


def test_constructor_raises_without_paho():
    """ImportError raised when paho-mqtt is not installed."""
    with patch("nomothetic.telemetry.mqtt", None):
        from nomothetic.telemetry import TelemetryPublisher

        with pytest.raises(ImportError, match="paho-mqtt is required"):
            TelemetryPublisher(broker="localhost")


# ---------------------------------------------------------------------------
# 2. from_env() classmethod
# ---------------------------------------------------------------------------


def test_from_env_reads_broker(mock_mqtt_module, monkeypatch):
    """from_env reads NOMON_MQTT_BROKER from environment."""
    from nomothetic.telemetry import TelemetryPublisher

    monkeypatch.setenv("NOMON_MQTT_BROKER", "192.168.1.50")
    pub = TelemetryPublisher.from_env()
    assert pub.broker == "192.168.1.50"


def test_from_env_reads_all_vars(mock_mqtt_module, monkeypatch):
    """from_env reads all NOMON_MQTT_* env vars."""
    from nomothetic.telemetry import TelemetryPublisher

    monkeypatch.setenv("NOMON_MQTT_BROKER", "mybroker")
    monkeypatch.setenv("NOMON_MQTT_PORT", "8883")
    monkeypatch.setenv("NOMON_MQTT_TOPIC", "custom/topic")
    monkeypatch.setenv("NOMON_MQTT_INTERVAL", "15.0")
    monkeypatch.setenv("NOMON_DEVICE_ID", "env-device-01")

    pub = TelemetryPublisher.from_env()
    assert pub.port == 8883
    assert pub.topic == "custom/topic"
    assert pub.interval == 15.0
    assert pub.device_id == "env-device-01"


def test_from_env_applies_credentials_and_tls(mock_mqtt_module, monkeypatch):
    from nomothetic.telemetry import TelemetryPublisher

    """NOMON_MQTT_USERNAME/PASSWORD/TLS configure the paho client (S-4)."""
    _, mock_client_instance, _ = mock_mqtt_module
    monkeypatch.setenv("NOMON_MQTT_BROKER", "broker.example")
    monkeypatch.delenv("NOMON_MQTT_PORT", raising=False)
    monkeypatch.setenv("NOMON_MQTT_USERNAME", "nomon-ab12")
    monkeypatch.setenv("NOMON_MQTT_PASSWORD", "s3cret")
    monkeypatch.setenv("NOMON_MQTT_TLS", "1")
    pub = TelemetryPublisher.from_env()
    assert pub.port == 8883  # TLS default port
    mock_client_instance.username_pw_set.assert_called_once_with("nomon-ab12", "s3cret")
    mock_client_instance.tls_set.assert_called_once_with()


def test_from_env_private_ca_implies_tls(mock_mqtt_module, monkeypatch):
    from nomothetic.telemetry import TelemetryPublisher

    _, mock_client_instance, _ = mock_mqtt_module
    monkeypatch.setenv("NOMON_MQTT_BROKER", "broker.example")
    monkeypatch.delenv("NOMON_MQTT_TLS", raising=False)
    monkeypatch.delenv("NOMON_MQTT_USERNAME", raising=False)
    monkeypatch.setenv("NOMON_MQTT_CA_CERT", "/etc/nomothetic/mqtt-ca.pem")
    pub = TelemetryPublisher.from_env()
    assert pub.tls is True
    mock_client_instance.tls_set.assert_called_once_with(ca_certs="/etc/nomothetic/mqtt-ca.pem")


def test_from_env_plaintext_warns(mock_mqtt_module, monkeypatch, caplog):
    from nomothetic.telemetry import TelemetryPublisher

    _, mock_client_instance, _ = mock_mqtt_module
    for var in (
        "NOMON_MQTT_USERNAME",
        "NOMON_MQTT_PASSWORD",
        "NOMON_MQTT_TLS",
        "NOMON_MQTT_CA_CERT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NOMON_MQTT_BROKER", "broker.example")
    with caplog.at_level("WARNING", logger="nomothetic.telemetry"):
        TelemetryPublisher.from_env()
    assert "unauthenticated and cleartext" in caplog.text
    mock_client_instance.username_pw_set.assert_not_called()
    mock_client_instance.tls_set.assert_not_called()


def test_from_env_raises_without_broker(mock_mqtt_module, monkeypatch):
    """from_env raises ValueError when NOMON_MQTT_BROKER is not set."""
    from nomothetic.telemetry import TelemetryPublisher

    monkeypatch.delenv("NOMON_MQTT_BROKER", raising=False)
    with pytest.raises(ValueError, match="NOMON_MQTT_BROKER"):
        TelemetryPublisher.from_env()


# ---------------------------------------------------------------------------
# 3. get_device_id()
# ---------------------------------------------------------------------------


def test_get_device_id_from_env(monkeypatch):
    """get_device_id returns NOMON_DEVICE_ID when set."""
    from nomothetic.telemetry import TelemetryPublisher

    monkeypatch.setenv("NOMON_DEVICE_ID", "my-pi-01")
    assert TelemetryPublisher.get_device_id() == "my-pi-01"


def test_get_device_id_hostname_fallback(monkeypatch):
    """get_device_id falls back to hostname when env and /proc/cpuinfo unavailable."""
    import socket

    from nomothetic.telemetry import TelemetryPublisher

    monkeypatch.delenv("NOMON_DEVICE_ID", raising=False)
    with patch("builtins.open", side_effect=OSError("no such file")):
        device_id = TelemetryPublisher.get_device_id()
    assert device_id == socket.gethostname()


def test_get_device_id_from_cpuinfo(monkeypatch):
    """get_device_id parses serial from /proc/cpuinfo."""
    from nomothetic.telemetry import TelemetryPublisher

    monkeypatch.delenv("NOMON_DEVICE_ID", raising=False)
    cpuinfo_content = "Hardware\t: BCM2835\nRevision\t: a22082\nSerial\t\t: 00000000deadbeef\n"

    with patch(
        "builtins.open",
        MagicMock(
            return_value=MagicMock(
                __enter__=lambda s, *a: iter(cpuinfo_content.splitlines(keepends=True)),
                __exit__=lambda s, *a: None,
            )
        ),
    ):
        device_id = TelemetryPublisher.get_device_id()

    assert device_id == "pi-deadbeef"


def test_get_device_id_cpuinfo_zero_serial_falls_back_to_hostname(monkeypatch):
    """get_device_id falls back to hostname when /proc/cpuinfo serial is all zeros."""
    import socket

    from nomothetic.telemetry import TelemetryPublisher

    monkeypatch.delenv("NOMON_DEVICE_ID", raising=False)
    cpuinfo_content = "Hardware\t: BCM2835\nRevision\t: a22082\nSerial\t\t: 0000000000000000\n"

    with patch(
        "builtins.open",
        MagicMock(
            return_value=MagicMock(
                __enter__=lambda s, *a: iter(cpuinfo_content.splitlines(keepends=True)),
                __exit__=lambda s, *a: None,
            )
        ),
    ):
        device_id = TelemetryPublisher.get_device_id()

    assert device_id == socket.gethostname()


# ---------------------------------------------------------------------------
# 4. build_payload()
# ---------------------------------------------------------------------------


def test_build_payload_without_camera(publisher):
    """build_payload returns null camera field when no camera provided."""
    payload = publisher.build_payload()
    assert payload["camera"] is None
    assert payload["device_id"] == publisher.device_id
    assert "timestamp" in payload
    assert "nomon_version" in payload


def test_build_payload_with_camera(mock_mqtt_module, mock_camera):
    """build_payload includes camera status when camera is provided."""
    from nomothetic.telemetry import TelemetryPublisher

    pub = TelemetryPublisher(broker="localhost", camera=mock_camera)
    payload = pub.build_payload()

    assert payload["camera"] is not None
    cam = payload["camera"]
    assert cam["ready"] is True
    assert cam["recording"] is False
    assert cam["resolution"] == "1280x720"
    assert cam["fps"] == 30
    assert cam["encoder"] == "h264"


def test_build_payload_camera_recording(mock_mqtt_module, mock_camera):
    """build_payload reflects recording state correctly."""
    from nomothetic.telemetry import TelemetryPublisher

    mock_camera._is_recording = True
    pub = TelemetryPublisher(broker="localhost", camera=mock_camera)
    payload = pub.build_payload()
    assert payload["camera"]["recording"] is True


def test_build_payload_timestamp_is_utc(publisher):
    """build_payload timestamp includes UTC offset."""
    payload = publisher.build_payload()
    # ISO 8601 with +00:00 or Z
    ts = payload["timestamp"]
    assert "+00:00" in ts or ts.endswith("Z")


def test_build_payload_camera_error_graceful(mock_mqtt_module):
    """build_payload handles broken camera gracefully."""
    from unittest.mock import PropertyMock

    from nomothetic.telemetry import TelemetryPublisher

    broken_camera = MagicMock()
    type(broken_camera)._is_recording = PropertyMock(side_effect=RuntimeError("hardware error"))

    pub = TelemetryPublisher(broker="localhost", camera=broken_camera)
    payload = pub.build_payload()
    # Should not raise; camera field shows not-ready with error message
    assert payload["camera"] is not None
    assert payload["camera"]["ready"] is False
    assert "error" in payload["camera"]


# ---------------------------------------------------------------------------
# 5. start_background() & stop()
# ---------------------------------------------------------------------------


def test_start_background_returns_thread(mock_mqtt_module, monkeypatch):
    """start_background returns a running daemon thread."""
    from nomothetic.telemetry import TelemetryPublisher

    pub = TelemetryPublisher(broker="localhost", interval=999.0)

    # Prevent _run_loop from doing real work
    def idle_loop() -> None:
        pub._stop_event.wait()

    pub._run_loop = idle_loop
    thread = pub.start_background()
    assert isinstance(thread, threading.Thread)
    assert thread.daemon is True
    assert thread.is_alive()

    pub.stop()
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_stop_sets_event(publisher):
    """stop() sets the internal stop event."""
    assert not publisher._stop_event.is_set()
    publisher.stop()
    assert publisher._stop_event.is_set()


def test_stop_calls_disconnect(mock_mqtt_module):
    """stop() calls disconnect() on the underlying MQTT client."""
    from nomothetic.telemetry import TelemetryPublisher

    mock_mqtt, mock_client, _ = mock_mqtt_module
    pub = TelemetryPublisher(broker="localhost")
    pub.stop()
    mock_client.disconnect.assert_called_once()


def test_stop_ignores_disconnect_error(mock_mqtt_module):
    """stop() does not propagate exceptions raised by disconnect()."""
    from nomothetic.telemetry import TelemetryPublisher

    mock_mqtt, mock_client, _ = mock_mqtt_module
    mock_client.disconnect.side_effect = OSError("already disconnected")
    pub = TelemetryPublisher(broker="localhost")
    pub.stop()  # must not raise
    assert pub._stop_event.is_set()


def test_start_background_can_restart_after_stop(mock_mqtt_module):
    """start_background() clears the stop event so the publisher can be restarted."""
    from nomothetic.telemetry import TelemetryPublisher

    pub = TelemetryPublisher(broker="localhost", interval=999.0)

    def idle_loop() -> None:
        pub._stop_event.wait()

    pub._run_loop = idle_loop
    thread1 = pub.start_background()
    pub.stop()
    thread1.join(timeout=2.0)
    assert not thread1.is_alive()

    thread2 = pub.start_background()
    assert thread2.is_alive()
    pub.stop()
    thread2.join(timeout=2.0)
    assert not thread2.is_alive()


# ---------------------------------------------------------------------------
# 6. publish_now()
# ---------------------------------------------------------------------------


def test_publish_now_success(mock_mqtt_module):
    """publish_now returns True on successful publish."""
    from nomothetic.telemetry import TelemetryPublisher

    mock_mqtt, mock_client, mock_result = mock_mqtt_module
    pub = TelemetryPublisher(broker="localhost")

    result = pub.publish_now()

    assert result is True
    mock_client.publish.assert_called_once()


def test_publish_now_failure_returns_false(mock_mqtt_module):
    """publish_now returns False when publish raises an exception."""
    from nomothetic.telemetry import TelemetryPublisher

    mock_mqtt, mock_client, mock_result = mock_mqtt_module
    mock_client.connect.side_effect = ConnectionRefusedError("broker unreachable")

    pub = TelemetryPublisher(broker="localhost")
    result = pub.publish_now()

    assert result is False


def test_publish_now_skips_connect_when_already_connected(mock_mqtt_module):
    """publish_now skips connect() when already connected."""
    from nomothetic.telemetry import TelemetryPublisher

    mock_mqtt, mock_client, _ = mock_mqtt_module
    pub = TelemetryPublisher(broker="localhost")
    pub._connected = True

    pub.publish_now()

    mock_client.connect.assert_not_called()


def test_publish_now_uses_correct_topic_payload_and_qos(mock_mqtt_module):
    """publish_now passes the configured topic, valid JSON payload, and QoS to publish()."""
    import json

    from nomothetic.telemetry import TelemetryPublisher

    mock_mqtt, mock_client, _ = mock_mqtt_module
    pub = TelemetryPublisher(broker="localhost", topic="fleet/pi-01", qos=0)
    pub._connected = True

    pub.publish_now()

    call_args = mock_client.publish.call_args
    assert call_args[0][0] == "fleet/pi-01"
    json.loads(call_args[0][1])  # payload must be valid JSON
    assert call_args[1]["qos"] == 0


def test_publish_now_returns_false_when_wait_for_publish_raises(mock_mqtt_module):
    """publish_now returns False if wait_for_publish() raises."""
    from nomothetic.telemetry import TelemetryPublisher

    mock_mqtt, mock_client, mock_result = mock_mqtt_module
    mock_result.wait_for_publish.side_effect = RuntimeError("timeout")
    pub = TelemetryPublisher(broker="localhost")
    pub._connected = True

    assert pub.publish_now() is False


def test_publish_now_clears_connected_flag_on_failure(mock_mqtt_module):
    """publish_now resets _connected to False when publish() raises."""
    from nomothetic.telemetry import TelemetryPublisher

    mock_mqtt, mock_client, _ = mock_mqtt_module
    mock_client.publish.side_effect = OSError("broker gone")
    pub = TelemetryPublisher(broker="localhost")
    pub._connected = True

    assert pub.publish_now() is False
    assert pub._connected is False


# ---------------------------------------------------------------------------
# 7. Callbacks (_on_connect / _on_disconnect)
# ---------------------------------------------------------------------------


def test_on_connect_sets_connected_on_success(mock_mqtt_module):
    """_on_connect sets _connected=True when reason_code is not a failure."""
    from nomothetic.telemetry import TelemetryPublisher

    pub = TelemetryPublisher(broker="localhost")
    pub._connected = False

    reason_code = MagicMock()
    reason_code.is_failure = False
    pub._on_connect(None, None, None, reason_code, None)

    assert pub._connected is True


def test_on_connect_clears_connected_on_failure(mock_mqtt_module):
    """_on_connect sets _connected=False when the broker refuses the connection."""
    from nomothetic.telemetry import TelemetryPublisher

    pub = TelemetryPublisher(broker="localhost")
    pub._connected = True

    reason_code = MagicMock()
    reason_code.is_failure = True
    pub._on_connect(None, None, None, reason_code, None)

    assert pub._connected is False


def test_on_disconnect_clears_connected(mock_mqtt_module):
    """_on_disconnect sets _connected=False."""
    from nomothetic.telemetry import TelemetryPublisher

    pub = TelemetryPublisher(broker="localhost")
    pub._connected = True

    pub._on_disconnect(None, None, None, MagicMock(), None)

    assert pub._connected is False


# ---------------------------------------------------------------------------
# 8. Exponential backoff
# ---------------------------------------------------------------------------


def test_backoff_doubles_on_reconnect_failure(mock_mqtt_module: tuple) -> None:
    """_run_loop doubles the backoff delay after each failed connect."""
    from nomothetic.telemetry import _BACKOFF_BASE, TelemetryPublisher

    mock_mqtt, mock_client, _ = mock_mqtt_module
    pub = TelemetryPublisher(broker="localhost", interval=0.01)

    wait_delays: list[float] = []

    def fake_wait(timeout: float = 0.0) -> bool:
        wait_delays.append(timeout)
        pub._stop_event.set()  # always stop after first wait
        return True

    pub._stop_event.wait = fake_wait  # type: ignore
    mock_client.connect.side_effect = OSError("refused")

    thread = pub.start_background()
    thread.join(timeout=3.0)

    # At least one backoff delay should have been recorded
    assert len(wait_delays) >= 1
    assert wait_delays[0] == _BACKOFF_BASE


def test_backoff_capped_at_max(mock_mqtt_module: tuple) -> None:
    """Backoff delay does not exceed _BACKOFF_CAP when _run_loop retries."""
    from nomothetic.telemetry import _BACKOFF_CAP, TelemetryPublisher

    mock_mqtt, mock_client, _ = mock_mqtt_module
    pub = TelemetryPublisher(broker="localhost", interval=0.01)

    wait_delays: list[float] = []
    call_count = 0

    def fake_wait(timeout: float = 0.0) -> bool:
        nonlocal call_count
        wait_delays.append(timeout)
        call_count += 1
        # Run enough iterations to push past _BACKOFF_CAP (1→2→4→8→16→32→60→60)
        if call_count >= 8:
            pub._stop_event.set()
        return pub._stop_event.is_set()  # type: ignore[no-any-return]

    pub._stop_event.wait = fake_wait  # type: ignore
    mock_client.connect.side_effect = OSError("refused")

    thread = pub.start_background()
    thread.join(timeout=5.0)

    assert len(wait_delays) >= 7, "Loop did not run enough iterations to test the cap"
    assert all(d <= _BACKOFF_CAP for d in wait_delays), "A backoff delay exceeded _BACKOFF_CAP"
    assert wait_delays[-1] == _BACKOFF_CAP, "Cap was never reached"


def test_run_loop_backoff_resets_after_successful_connect(mock_mqtt_module: tuple) -> None:
    """_run_loop resets backoff to _BACKOFF_BASE after a successful connect."""
    from nomothetic.telemetry import _BACKOFF_BASE, TelemetryPublisher

    mock_mqtt, mock_client, _ = mock_mqtt_module
    pub = TelemetryPublisher(broker="localhost", interval=0.01)

    connect_calls = [0]

    def fake_connect(*args, **kwargs):
        connect_calls[0] += 1
        if connect_calls[0] <= 2:
            raise OSError("refused")

    mock_client.connect.side_effect = fake_connect

    wait_delays: list[float] = []
    wait_calls = [0]

    def fake_wait(timeout: float = 0.0) -> bool:
        wait_delays.append(timeout)
        wait_calls[0] += 1
        if wait_calls[0] >= 3:
            pub._stop_event.set()
        return pub._stop_event.is_set()  # type: ignore[no-any-return]

    pub._stop_event.wait = fake_wait  # type: ignore

    thread = pub.start_background()
    thread.join(timeout=5.0)

    # First two waits are exponential backoff delays
    assert wait_delays[0] == _BACKOFF_BASE
    assert wait_delays[1] == _BACKOFF_BASE * 2
    # Third wait is the normal interval — confirms backoff was reset after the successful connect
    assert wait_delays[2] == 0.01


def test_run_loop_publish_failure_triggers_reconnect(mock_mqtt_module):
    """A publish error inside _run_loop sets _connected=False, causing a reconnect."""
    from nomothetic.telemetry import TelemetryPublisher

    mock_mqtt, mock_client, _ = mock_mqtt_module
    pub = TelemetryPublisher(broker="localhost", interval=0.01)
    pub._connected = True  # skip the initial connect attempt

    publish_count = [0]

    def fake_publish(*args, **kwargs):
        publish_count[0] += 1
        if publish_count[0] == 1:
            raise OSError("broker gone")
        return MagicMock()

    mock_client.publish.side_effect = fake_publish

    wait_count = [0]

    def fake_wait(timeout: float = 0.0) -> bool:
        wait_count[0] += 1
        if wait_count[0] >= 2:
            pub._stop_event.set()
        return pub._stop_event.is_set()  # type: ignore[no-any-return]

    pub._stop_event.wait = fake_wait  # type: ignore

    thread = pub.start_background()
    thread.join(timeout=5.0)

    # connect() should have been called once after the publish failure reset _connected
    mock_client.connect.assert_called_once()
    assert publish_count[0] == 2


def test_run_loop_calls_loop_stop_on_exit(mock_mqtt_module):
    """_run_loop calls loop_stop() when the stop event is set."""
    from nomothetic.telemetry import TelemetryPublisher

    mock_mqtt, mock_client, _ = mock_mqtt_module
    pub = TelemetryPublisher(broker="localhost", interval=0.01)
    pub._connected = True  # skip the connect block

    # start_background() clears the stop event, so we must trigger stop from
    # inside the loop via the wait() intercept rather than presetting it.
    def fake_wait(timeout: float = 0.0) -> bool:
        pub._stop_event.set()
        return True

    pub._stop_event.wait = fake_wait  # type: ignore

    thread = pub.start_background()
    thread.join(timeout=2.0)

    mock_client.loop_stop.assert_called_once()


# ---------------------------------------------------------------------------
# 9. Package-level import
# ---------------------------------------------------------------------------


def test_package_exports_telemetry_publisher(mock_mqtt_module):
    """TelemetryPublisher is accessible from the nomothetic package."""
    import importlib

    import nomothetic

    importlib.reload(nomothetic)
    assert hasattr(nomothetic, "TelemetryPublisher")
    assert "TelemetryPublisher" in nomothetic.__all__
