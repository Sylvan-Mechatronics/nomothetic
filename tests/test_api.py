"""Tests for the HTTP REST API module.

Tests cover endpoint functionality, error handling, and CORS behavior.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from nomothetic.api import APIServer, create_app


@pytest.fixture
def client():
    """Create a test client for the FastAPI app."""
    with patch.dict(
        os.environ,
        {
            "NOMON_API_MODE": "device",
            "NOMON_DEVICE_AUTH": "false",
        },
        clear=False,
    ):
        app = create_app()
        yield TestClient(app)


@pytest.fixture
def mock_camera():
    """Create a mock camera for testing."""
    camera = MagicMock()
    camera.width = 1280
    camera.height = 720
    camera.fps = 30
    camera.encoder = "h264"
    camera._is_recording = False
    return camera


# ============================================================================
# Health & Status Endpoints
# ============================================================================


def test_health_check(client):
    """Test health check endpoint returns success."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "nomon-camera-api"
    assert "version" in data


def test_camera_status_without_camera(client):
    """Test camera status endpoint without initialized camera."""
    response = client.get("/api/camera/status")
    assert response.status_code == 503
    assert "not initialized" in response.json()["error"]


def test_camera_status_with_camera(client, mock_camera):
    """Test camera status endpoint returns current state."""
    import nomothetic.api

    nomothetic.api._camera = mock_camera

    response = client.get("/api/camera/status")
    assert response.status_code == 200
    data = response.json()
    assert data["camera_ready"] is True
    assert data["recording"] is False
    assert data["resolution"] == "1280x720"
    assert data["fps"] == 30
    assert data["encoder"] == "h264"
    assert "timestamp" in data

    # Cleanup
    nomothetic.api._camera = None


def test_camera_status_recording(client, mock_camera):
    """Test camera status reflects recording state."""
    import nomothetic.api

    mock_camera._is_recording = True
    nomothetic.api._camera = mock_camera

    response = client.get("/api/camera/status")
    assert response.status_code == 200
    assert response.json()["recording"] is True

    # Cleanup
    nomothetic.api._camera = None


# ============================================================================
# Image Capture Endpoints
# ============================================================================


def test_capture_image_success(client, mock_camera):
    """Test successful image capture."""
    import nomothetic.api

    nomothetic.api._camera = mock_camera

    response = client.post(
        "/api/camera/capture",
        json={"filename": "test.jpg"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["filename"] == "test.jpg"
    assert "timestamp" in data
    mock_camera.capture_image.assert_called_once_with("test.jpg")

    # Cleanup
    nomothetic.api._camera = None


def test_capture_image_invalid_filename(client, mock_camera):
    """Test capture with invalid filename raises error."""
    import nomothetic.api

    mock_camera.capture_image.side_effect = ValueError("Invalid filename")
    nomothetic.api._camera = mock_camera

    response = client.post(
        "/api/camera/capture",
        json={"filename": "../etc/passwd"},
    )
    assert response.status_code == 400
    assert "Invalid filename" in response.json()["error"]

    # Cleanup
    nomothetic.api._camera = None


def test_capture_image_camera_error(client, mock_camera):
    """Test capture with camera error."""
    import nomothetic.api

    mock_camera.capture_image.side_effect = RuntimeError("Camera failed")
    nomothetic.api._camera = mock_camera

    response = client.post(
        "/api/camera/capture",
        json={"filename": "photo.jpg"},
    )
    assert response.status_code == 500
    assert "Camera failed" in response.json()["error"]

    # Cleanup
    nomothetic.api._camera = None


def test_capture_without_camera(client):
    """Test capture endpoint without initialized camera."""
    response = client.post(
        "/api/camera/capture",
        json={"filename": "test.jpg"},
    )
    assert response.status_code == 503
    assert "not initialized" in response.json()["error"]


def test_camera_frame_returns_jpeg_bytes(client, mock_camera):
    """The frame endpoint returns raw image/jpeg bytes from the camera."""
    import nomothetic.api

    jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIFtestframe\xff\xd9"

    def _one_frame():
        yield jpeg

    mock_camera.get_jpeg_frame_generator.return_value = _one_frame()
    nomothetic.api._camera = mock_camera

    response = client.get("/api/camera/frame")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == jpeg

    # Cleanup
    nomothetic.api._camera = None


def test_camera_frame_capture_error(client, mock_camera):
    """A camera failure during frame capture returns 500."""
    import nomothetic.api

    mock_camera.get_jpeg_frame_generator.side_effect = RuntimeError("Camera failed")
    nomothetic.api._camera = mock_camera

    response = client.get("/api/camera/frame")
    assert response.status_code == 500
    assert "Frame capture failed" in response.json()["error"]

    # Cleanup
    nomothetic.api._camera = None


def test_camera_frame_without_camera(client):
    """The frame endpoint returns 503 when the camera is not initialized."""
    response = client.get("/api/camera/frame")
    assert response.status_code == 503
    assert "not initialized" in response.json()["error"]


# ============================================================================
# Video Recording Endpoints
# ============================================================================


def test_record_start_success(client, mock_camera):
    """Test successful recording start."""
    import nomothetic.api

    nomothetic.api._camera = mock_camera

    response = client.post(
        "/api/camera/record/start",
        json={"filename": "video.mp4"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["filename"] == "video.mp4"
    assert "timestamp" in data
    mock_camera.start_recording.assert_called_once_with("video.mp4")

    # Cleanup
    nomothetic.api._camera = None


def test_record_start_with_encoder(client, mock_camera):
    """Test recording start with encoder specification."""
    import nomothetic.api

    nomothetic.api._camera = mock_camera

    response = client.post(
        "/api/camera/record/start",
        json={"filename": "video.mp4", "encoder": "mjpeg"},
    )
    assert response.status_code == 200
    # Encoder should be updated
    assert mock_camera.encoder == "mjpeg"

    # Cleanup
    nomothetic.api._camera = None


def test_record_start_already_recording(client, mock_camera):
    """Test recording start when already recording."""
    import nomothetic.api

    mock_camera._is_recording = True
    nomothetic.api._camera = mock_camera

    response = client.post(
        "/api/camera/record/start",
        json={"filename": "video.mp4"},
    )
    assert response.status_code == 409
    assert "Recording already in progress" in response.json()["error"]

    # Cleanup
    nomothetic.api._camera = None


def test_record_start_invalid_filename(client, mock_camera):
    """Test recording start with invalid filename."""
    import nomothetic.api

    mock_camera.start_recording.side_effect = ValueError("Invalid filename")
    nomothetic.api._camera = mock_camera

    response = client.post(
        "/api/camera/record/start",
        json={"filename": "../etc/passwd"},
    )
    assert response.status_code == 400

    # Cleanup
    nomothetic.api._camera = None


def test_record_start_without_camera(client):
    """Test record start without initialized camera."""
    response = client.post(
        "/api/camera/record/start",
        json={"filename": "video.mp4"},
    )
    assert response.status_code == 503
    assert "not initialized" in response.json()["error"]


def test_record_stop_success(client, mock_camera):
    """Test successful recording stop."""
    import nomothetic.api

    mock_camera._is_recording = True
    nomothetic.api._camera = mock_camera

    response = client.post("/api/camera/record/stop")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "timestamp" in data
    mock_camera.stop_recording.assert_called_once()

    # Cleanup
    nomothetic.api._camera = None


def test_record_stop_not_recording(client, mock_camera):
    """Test recording stop when not recording."""
    import nomothetic.api

    nomothetic.api._camera = mock_camera

    response = client.post("/api/camera/record/stop")
    assert response.status_code == 409
    assert "No recording in progress" in response.json()["error"]

    # Cleanup
    nomothetic.api._camera = None


def test_record_stop_camera_error(client, mock_camera):
    """Test recording stop with camera error."""
    import nomothetic.api

    mock_camera._is_recording = True
    mock_camera.stop_recording.side_effect = RuntimeError("Stop failed")
    nomothetic.api._camera = mock_camera

    response = client.post("/api/camera/record/stop")
    assert response.status_code == 500
    assert "Stop failed" in response.json()["error"]

    # Cleanup
    nomothetic.api._camera = None


def test_record_stop_without_camera(client):
    """Test record stop without initialized camera."""
    response = client.post("/api/camera/record/stop")
    assert response.status_code == 503
    assert "not initialized" in response.json()["error"]


# ============================================================================
# CORS Headers
# ============================================================================


def test_cors_middleware_configured(client):
    """Test that middleware is configured."""
    app = client.app
    # Check that app has middleware
    assert len(app.user_middleware) > 0


# ============================================================================
# API Server Configuration
# ============================================================================


def test_api_server_initialization():
    """Test APIServer initialization with defaults."""
    server = APIServer()
    assert server.host == "127.0.0.1"
    assert server.port == 8443
    assert server.use_ssl is True


def test_api_server_custom_host_port():
    """Test APIServer with custom host and port."""
    server = APIServer(host="0.0.0.0", port=9000)
    assert server.host == "0.0.0.0"
    assert server.port == 9000


def test_api_server_invalid_port():
    """Test APIServer rejects invalid ports."""
    with pytest.raises(ValueError, match="Invalid port"):
        APIServer(port=0)

    with pytest.raises(ValueError, match="Invalid port"):
        APIServer(port=70000)


def test_api_server_get_config():
    """Test APIServer configuration generation."""
    server = APIServer(host="localhost", port=8000, use_ssl=False)
    config = server.get_config()
    assert config["host"] == "localhost"
    assert config["port"] == 8000
    assert "ssl_certfile" not in config


def test_api_server_get_config_with_ssl():
    """Test APIServer configuration with SSL."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        server = APIServer(port=8443, use_ssl=True, cert_dir=Path(tmpdir))
        config = server.get_config()
        assert "ssl_certfile" in config
        assert "ssl_keyfile" in config


# ============================================================================
# Request/Response Models
# ============================================================================


def test_capture_request_model():
    """Test CaptureRequest validation."""
    from nomothetic.api import CaptureRequest

    req = CaptureRequest(filename="test.jpg")
    assert req.filename == "test.jpg"


def test_record_request_model():
    """Test RecordRequest validation."""
    from nomothetic.api import RecordRequest

    req = RecordRequest(filename="video.mp4")
    assert req.filename == "video.mp4"
    assert req.encoder == "h264"

    req2 = RecordRequest(filename="video.mp4", encoder="mjpeg")
    assert req2.encoder == "mjpeg"


def test_camera_status_model():
    """Test CameraStatus model."""
    from nomothetic.api import CameraStatus

    status = CameraStatus(
        camera_ready=True,
        recording=False,
        resolution="1280x720",
        fps=30,
        encoder="h264",
        timestamp="2024-01-01T00:00:00",
    )
    assert status.camera_ready is True
    assert status.recording is False


# ============================================================================
# HAT Endpoints
# ============================================================================


@pytest.fixture
def mock_hat():
    """Create a mock HatClient for HAT endpoint tests."""
    return MagicMock()


def test_get_battery_no_client(client):
    """GET /api/hat/battery returns 503 when _hat_client is None."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.get("/api/hat/battery")
    assert response.status_code == 503
    assert "not available" in response.json()["error"]


def test_get_battery_success(client, mock_hat):
    """GET /api/hat/battery returns voltage and timestamp on success."""
    import nomothetic.api

    mock_hat.get_battery_voltage.return_value = 7.42
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/hat/battery")
    assert response.status_code == 200
    data = response.json()
    assert data["voltage_v"] == pytest.approx(7.42)
    assert "timestamp" in data

    nomothetic.api._hat_client = None


def test_get_battery_connection_error(client, mock_hat):
    """GET /api/hat/battery returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.get_battery_voltage.side_effect = HatConnectionError("socket gone")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/hat/battery")
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_get_battery_hardware_error(client, mock_hat):
    """GET /api/hat/battery returns 500 on HatError (hardware failure)."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.get_battery_voltage.side_effect = HatError("HARDWARE_ERROR", "I2C failed")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/hat/battery")
    assert response.status_code == 500

    nomothetic.api._hat_client = None


def test_set_servo_no_client(client):
    """POST /api/hat/servo returns 503 when _hat_client is None."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.post("/api/hat/servo", json={"channel": 0, "angle_deg": 90.0})
    assert response.status_code == 503


def test_set_servo_success(client, mock_hat):
    """POST /api/hat/servo returns channel, angle, and timestamp on success."""
    import nomothetic.api

    mock_hat.set_servo_angle.return_value = None
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/hat/servo", json={"channel": 2, "angle_deg": 45.0})
    assert response.status_code == 200
    data = response.json()
    assert data["channel"] == 2
    assert data["angle_deg"] == pytest.approx(45.0)
    assert "timestamp" in data

    nomothetic.api._hat_client = None


def test_set_servo_invalid_channel(client, mock_hat):
    """POST /api/hat/servo returns 422 when channel is out of range."""
    import nomothetic.api

    nomothetic.api._hat_client = mock_hat
    response = client.post("/api/hat/servo", json={"channel": 12, "angle_deg": 90.0})
    assert response.status_code == 422

    nomothetic.api._hat_client = None


def test_set_servo_invalid_angle(client, mock_hat):
    """POST /api/hat/servo returns 422 when angle_deg is out of range."""
    import nomothetic.api

    nomothetic.api._hat_client = mock_hat
    response = client.post("/api/hat/servo", json={"channel": 0, "angle_deg": 200.0})
    assert response.status_code == 422

    nomothetic.api._hat_client = None


def test_set_servo_connection_error(client, mock_hat):
    """POST /api/hat/servo returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.set_servo_angle.side_effect = HatConnectionError("daemon gone")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/hat/servo", json={"channel": 0, "angle_deg": 90.0})
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_reset_mcu_no_client(client):
    """POST /api/hat/reset returns 503 when _hat_client is None."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.post("/api/hat/reset")
    assert response.status_code == 503


def test_reset_mcu_success(client, mock_hat):
    """POST /api/hat/reset returns success and timestamp."""
    import nomothetic.api

    mock_hat.reset_mcu.return_value = None
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/hat/reset")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "timestamp" in data

    nomothetic.api._hat_client = None


def test_get_servo_status_no_client(client):
    """GET /api/hat/servo/status returns 503 when _hat_client is None."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.get("/api/hat/servo/status")
    assert response.status_code == 503


def test_get_servo_status_success(client, mock_hat):
    """GET /api/hat/servo/status returns lease list and timestamp."""
    import nomothetic.api
    from nomothetic.hat import ServoLeaseEntry, ServoStatusResult

    mock_hat.get_servo_status.return_value = ServoStatusResult(
        active_leases=[ServoLeaseEntry(channel=1, ttl_remaining_ms=400, conn_id=5)]
    )
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/hat/servo/status")
    assert response.status_code == 200
    data = response.json()
    assert len(data["active_leases"]) == 1
    assert data["active_leases"][0]["channel"] == 1
    assert data["active_leases"][0]["ttl_remaining_ms"] == 400
    assert data["active_leases"][0]["conn_id"] == 5
    assert "timestamp" in data

    nomothetic.api._hat_client = None


def test_get_servo_status_empty(client, mock_hat):
    """GET /api/hat/servo/status returns empty list when no leases active."""
    import nomothetic.api
    from nomothetic.hat import ServoStatusResult

    mock_hat.get_servo_status.return_value = ServoStatusResult(active_leases=[])
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/hat/servo/status")
    assert response.status_code == 200
    assert response.json()["active_leases"] == []

    nomothetic.api._hat_client = None


def test_get_servo_status_connection_error(client, mock_hat):
    """GET /api/hat/servo/status returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.get_servo_status.side_effect = HatConnectionError("daemon gone")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/hat/servo/status")
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_get_servo_status_hardware_error(client, mock_hat):
    """GET /api/hat/servo/status returns 500 on HatError."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.get_servo_status.side_effect = HatError("HARDWARE_ERROR", "lease read failed")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/hat/servo/status")
    assert response.status_code == 500

    nomothetic.api._hat_client = None


def test_get_mcu_status_no_client(client):
    """GET /api/hat/mcu/status returns 503 when _hat_client is None."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.get("/api/hat/mcu/status")
    assert response.status_code == 503


def test_get_mcu_status_success(client, mock_hat):
    """GET /api/hat/mcu/status returns reset count, last reset age, and timestamp."""
    import nomothetic.api
    from nomothetic.hat import McuStatusResult

    mock_hat.get_mcu_status.return_value = McuStatusResult(
        resets_since_start=2, last_reset_s_ago=60
    )
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/hat/mcu/status")
    assert response.status_code == 200
    data = response.json()
    assert data["resets_since_start"] == 2
    assert data["last_reset_s_ago"] == 60
    assert "timestamp" in data

    nomothetic.api._hat_client = None


def test_get_mcu_status_never_reset(client, mock_hat):
    """GET /api/hat/mcu/status returns null last_reset_s_ago when never reset."""
    import nomothetic.api
    from nomothetic.hat import McuStatusResult

    mock_hat.get_mcu_status.return_value = McuStatusResult(
        resets_since_start=0, last_reset_s_ago=None
    )
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/hat/mcu/status")
    assert response.status_code == 200
    data = response.json()
    assert data["resets_since_start"] == 0
    assert data["last_reset_s_ago"] is None

    nomothetic.api._hat_client = None


def test_get_mcu_status_connection_error(client, mock_hat):
    """GET /api/hat/mcu/status returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.get_mcu_status.side_effect = HatConnectionError("daemon gone")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/hat/mcu/status")
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_get_mcu_status_hardware_error(client, mock_hat):
    """GET /api/hat/mcu/status returns 500 on HatError."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.get_mcu_status.side_effect = HatError("HARDWARE_ERROR", "MCU status read failed")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/hat/mcu/status")
    assert response.status_code == 500

    nomothetic.api._hat_client = None


# ============================================================================
# Motor Endpoints
# ============================================================================


def test_set_motor_no_client(client):
    """POST /api/hat/motor returns 503 when _hat_client is None."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.post("/api/hat/motor", json={"channel": 0, "speed_pct": 50.0})
    assert response.status_code == 503


def test_set_motor_success(client, mock_hat):
    """POST /api/hat/motor returns channel, speed_pct, and timestamp on success."""
    import nomothetic.api

    mock_hat.set_motor_speed.return_value = None
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/hat/motor", json={"channel": 1, "speed_pct": -75.0})
    assert response.status_code == 200
    data = response.json()
    assert data["channel"] == 1
    assert data["speed_pct"] == pytest.approx(-75.0)
    assert "timestamp" in data
    mock_hat.set_motor_speed.assert_called_once_with(1, -75.0, 500)

    nomothetic.api._hat_client = None


def test_set_motor_invalid_channel(client, mock_hat):
    """POST /api/hat/motor returns 422 when channel is out of range."""
    import nomothetic.api

    nomothetic.api._hat_client = mock_hat
    response = client.post("/api/hat/motor", json={"channel": 4, "speed_pct": 50.0})
    assert response.status_code == 422

    nomothetic.api._hat_client = None


def test_set_motor_invalid_speed(client, mock_hat):
    """POST /api/hat/motor returns 422 when speed_pct is out of range."""
    import nomothetic.api

    nomothetic.api._hat_client = mock_hat
    response = client.post("/api/hat/motor", json={"channel": 0, "speed_pct": 150.0})
    assert response.status_code == 422

    nomothetic.api._hat_client = None


def test_set_motor_connection_error(client, mock_hat):
    """POST /api/hat/motor returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.set_motor_speed.side_effect = HatConnectionError("daemon gone")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/hat/motor", json={"channel": 0, "speed_pct": 50.0})
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_set_motor_hardware_error(client, mock_hat):
    """POST /api/hat/motor returns 500 on HatError."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.set_motor_speed.side_effect = HatError("HARDWARE_ERROR", "GPIO failed")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/hat/motor", json={"channel": 0, "speed_pct": 50.0})
    assert response.status_code == 500

    nomothetic.api._hat_client = None


def test_stop_motors_no_client(client):
    """POST /api/hat/motor/stop returns 503 when _hat_client is None."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.post("/api/hat/motor/stop")
    assert response.status_code == 503


def test_stop_motors_success(client, mock_hat):
    """POST /api/hat/motor/stop returns stopped count and timestamp."""
    import nomothetic.api

    mock_hat.stop_all_motors.return_value = 2
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/hat/motor/stop")
    assert response.status_code == 200
    data = response.json()
    assert data["stopped"] == 2
    assert "timestamp" in data

    nomothetic.api._hat_client = None


def test_stop_motors_connection_error(client, mock_hat):
    """POST /api/hat/motor/stop returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.stop_all_motors.side_effect = HatConnectionError("daemon gone")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/hat/motor/stop")
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_get_motor_status_no_client(client):
    """GET /api/hat/motor/status returns 503 when _hat_client is None."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.get("/api/hat/motor/status")
    assert response.status_code == 503


def test_get_motor_status_success(client, mock_hat):
    """GET /api/hat/motor/status returns lease list and timestamp."""
    import nomothetic.api
    from nomothetic.hat import MotorLeaseEntry, MotorStatusResult

    mock_hat.get_motor_status.return_value = MotorStatusResult(
        active_leases=[
            MotorLeaseEntry(channel=0, ttl_remaining_ms=312, conn_id=4),
            MotorLeaseEntry(channel=1, ttl_remaining_ms=198, conn_id=4),
        ]
    )
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/hat/motor/status")
    assert response.status_code == 200
    data = response.json()
    assert len(data["active_leases"]) == 2
    assert data["active_leases"][0]["channel"] == 0
    assert data["active_leases"][0]["ttl_remaining_ms"] == 312
    assert data["active_leases"][1]["channel"] == 1
    assert "timestamp" in data

    nomothetic.api._hat_client = None


def test_get_motor_status_empty(client, mock_hat):
    """GET /api/hat/motor/status returns empty list when no leases active."""
    import nomothetic.api
    from nomothetic.hat import MotorStatusResult

    mock_hat.get_motor_status.return_value = MotorStatusResult(active_leases=[])
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/hat/motor/status")
    assert response.status_code == 200
    assert response.json()["active_leases"] == []

    nomothetic.api._hat_client = None


def test_get_motor_status_connection_error(client, mock_hat):
    """GET /api/hat/motor/status returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.get_motor_status.side_effect = HatConnectionError("daemon gone")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/hat/motor/status")
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_get_motor_status_hardware_error(client, mock_hat):
    """GET /api/hat/motor/status returns 500 on HatError."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.get_motor_status.side_effect = HatError("HARDWARE_ERROR", "motor read failed")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/hat/motor/status")
    assert response.status_code == 500

    nomothetic.api._hat_client = None


# ============================================================================
# Vehicle Endpoints (/api/drive, /api/steer, /api/camera/pan,
#                   /api/camera/tilt, /api/sensor/grayscale)
# ============================================================================


def test_drive_success(client, mock_hat):
    """POST /api/drive returns DriveResponse on success."""
    import nomothetic.api

    mock_hat.drive.return_value = 2
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/drive", json={"speed_pct": 60.0, "ttl_ms": 500})
    assert response.status_code == 200
    data = response.json()
    assert data["speed_pct"] == 60.0
    assert data["motors"] == 2
    assert "timestamp" in data
    mock_hat.drive.assert_called_once_with(60.0, 500)

    nomothetic.api._hat_client = None


def test_drive_no_client(client):
    """POST /api/drive returns 503 when daemon unavailable."""
    response = client.post("/api/drive", json={"speed_pct": 50.0})
    assert response.status_code == 503


def test_drive_connection_error(client, mock_hat):
    """POST /api/drive returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.drive.side_effect = HatConnectionError("lost")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/drive", json={"speed_pct": 30.0})
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_drive_hardware_error(client, mock_hat):
    """POST /api/drive returns 500 on HatError."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.drive.side_effect = HatError("HARDWARE_ERROR", "motor stuck")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/drive", json={"speed_pct": 30.0})
    assert response.status_code == 500

    nomothetic.api._hat_client = None


def test_drive_invalid_speed(client, mock_hat):
    """POST /api/drive returns 422 for speed_pct out of range."""
    import nomothetic.api

    nomothetic.api._hat_client = mock_hat
    response = client.post("/api/drive", json={"speed_pct": 200.0})
    assert response.status_code == 422

    nomothetic.api._hat_client = None


def test_steer_success(client, mock_hat):
    """POST /api/steer returns SteerResponse on success."""
    import nomothetic.api

    mock_hat.steer.return_value = None
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/steer", json={"angle_deg": 90.0, "ttl_ms": 500})
    assert response.status_code == 200
    data = response.json()
    assert data["angle_deg"] == 90.0
    assert "timestamp" in data
    mock_hat.steer.assert_called_once_with(90.0, 500)

    nomothetic.api._hat_client = None


def test_steer_no_client(client):
    """POST /api/steer returns 503 when daemon unavailable."""
    response = client.post("/api/steer", json={"angle_deg": 90.0})
    assert response.status_code == 503


def test_steer_connection_error(client, mock_hat):
    """POST /api/steer returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.steer.side_effect = HatConnectionError("lost")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/steer", json={"angle_deg": 45.0})
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_steer_invalid_angle(client, mock_hat):
    """POST /api/steer returns 422 for angle_deg out of range."""
    import nomothetic.api

    nomothetic.api._hat_client = mock_hat
    response = client.post("/api/steer", json={"angle_deg": 200.0})
    assert response.status_code == 422

    nomothetic.api._hat_client = None


def test_pan_camera_success(client, mock_hat):
    """POST /api/camera/pan returns PanResponse on success."""
    import nomothetic.api

    mock_hat.pan_camera.return_value = None
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/camera/pan", json={"angle_deg": 45.0, "ttl_ms": 500})
    assert response.status_code == 200
    data = response.json()
    assert data["angle_deg"] == 45.0
    assert "timestamp" in data
    mock_hat.pan_camera.assert_called_once_with(45.0, 500)

    nomothetic.api._hat_client = None


def test_pan_camera_no_client(client):
    """POST /api/camera/pan returns 503 when daemon unavailable."""
    response = client.post("/api/camera/pan", json={"angle_deg": 90.0})
    assert response.status_code == 503


def test_pan_camera_invalid_angle(client, mock_hat):
    """POST /api/camera/pan returns 422 for angle_deg out of range."""
    import nomothetic.api

    nomothetic.api._hat_client = mock_hat
    response = client.post("/api/camera/pan", json={"angle_deg": -10.0})
    assert response.status_code == 422

    nomothetic.api._hat_client = None


def test_pan_camera_connection_error(client, mock_hat):
    """POST /api/camera/pan returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.pan_camera.side_effect = HatConnectionError("lost")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/camera/pan", json={"angle_deg": 45.0})
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_tilt_camera_success(client, mock_hat):
    """POST /api/camera/tilt returns TiltResponse on success."""
    import nomothetic.api

    mock_hat.tilt_camera.return_value = None
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/camera/tilt", json={"angle_deg": 60.0, "ttl_ms": 500})
    assert response.status_code == 200
    data = response.json()
    assert data["angle_deg"] == 60.0
    assert "timestamp" in data
    mock_hat.tilt_camera.assert_called_once_with(60.0, 500)

    nomothetic.api._hat_client = None


def test_tilt_camera_no_client(client):
    """POST /api/camera/tilt returns 503 when daemon unavailable."""
    response = client.post("/api/camera/tilt", json={"angle_deg": 90.0})
    assert response.status_code == 503


def test_tilt_camera_connection_error(client, mock_hat):
    """POST /api/camera/tilt returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.tilt_camera.side_effect = HatConnectionError("lost")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/camera/tilt", json={"angle_deg": 60.0})
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_get_grayscale_success(client, mock_hat):
    """GET /api/sensor/grayscale returns GrayscaleResponse on success."""
    import nomothetic.api
    from nomothetic.hat import GrayscaleResult

    mock_hat.read_grayscale.return_value = GrayscaleResult(
        channels=[0, 1, 2], values=[1200, 3000, 800]
    )
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/sensor/grayscale")
    assert response.status_code == 200
    data = response.json()
    assert data["channels"] == [0, 1, 2]
    assert data["values"] == [1200, 3000, 800]
    assert "timestamp" in data

    nomothetic.api._hat_client = None


def test_get_grayscale_no_client(client):
    """GET /api/sensor/grayscale returns 503 when daemon unavailable."""
    response = client.get("/api/sensor/grayscale")
    assert response.status_code == 503


def test_get_grayscale_connection_error(client, mock_hat):
    """GET /api/sensor/grayscale returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.read_grayscale.side_effect = HatConnectionError("lost")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/sensor/grayscale")
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_get_grayscale_hardware_error(client, mock_hat):
    """GET /api/sensor/grayscale returns 500 on HatError."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.read_grayscale.side_effect = HatError("HARDWARE_ERROR", "ADC failed")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/sensor/grayscale")
    assert response.status_code == 500

    nomothetic.api._hat_client = None


# ============================================================================
# Ultrasonic Sensor Endpoint (/api/sensor/ultrasonic)
# ============================================================================


def test_get_ultrasonic_success(client, mock_hat):
    """GET /api/sensor/ultrasonic returns distance_cm and timestamp."""
    import nomothetic.api
    from nomothetic.hat import UltrasonicResult

    mock_hat.read_ultrasonic.return_value = UltrasonicResult(distance_cm=42.5)
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/sensor/ultrasonic")
    assert response.status_code == 200
    data = response.json()
    assert data["distance_cm"] == pytest.approx(42.5)
    assert "timestamp" in data

    nomothetic.api._hat_client = None


def test_get_ultrasonic_no_client(client):
    """GET /api/sensor/ultrasonic returns 503 when daemon unavailable."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.get("/api/sensor/ultrasonic")
    assert response.status_code == 503


def test_get_ultrasonic_connection_error(client, mock_hat):
    """GET /api/sensor/ultrasonic returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.read_ultrasonic.side_effect = HatConnectionError("socket gone")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/sensor/ultrasonic")
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_get_ultrasonic_no_echo_returns_null_distance(client, mock_hat):
    """GET /api/sensor/ultrasonic returns 200 with null distance on NO_ECHO."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.read_ultrasonic.side_effect = HatError("NO_ECHO", "out of range")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/sensor/ultrasonic")
    assert response.status_code == 200
    data = response.json()
    assert data["distance_cm"] is None
    assert "timestamp" in data

    nomothetic.api._hat_client = None


def test_get_ultrasonic_timeout_returns_null_distance(client, mock_hat):
    """GET /api/sensor/ultrasonic returns 200 with null distance on TIMEOUT."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.read_ultrasonic.side_effect = HatError("TIMEOUT", "no echo")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/sensor/ultrasonic")
    assert response.status_code == 200
    data = response.json()
    assert data["distance_cm"] is None
    assert "timestamp" in data

    nomothetic.api._hat_client = None


def test_get_ultrasonic_hardware_error(client, mock_hat):
    """GET /api/sensor/ultrasonic returns 500 on GPIO hardware failure.

    HARDWARE_ERROR is a real hardware fault, not a benign no-reading condition.
    It must propagate as 500 so operators can observe and diagnose GPIO failures.
    """
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.read_ultrasonic.side_effect = HatError("HARDWARE_ERROR", "GPIO bus fault")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/sensor/ultrasonic")
    assert response.status_code == 500

    nomothetic.api._hat_client = None


# ============================================================================
# Speaker Endpoint (/api/hat/speaker)
# ============================================================================


def test_set_speaker_enable_success(client, mock_hat):
    """POST /api/hat/speaker enable returns enabled=true and timestamp."""
    import nomothetic.api

    mock_hat.enable_speaker.return_value = None
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/hat/speaker", json={"enabled": True})
    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert "timestamp" in data
    mock_hat.enable_speaker.assert_called_once()

    nomothetic.api._hat_client = None


def test_set_speaker_disable_success(client, mock_hat):
    """POST /api/hat/speaker disable returns enabled=false and timestamp."""
    import nomothetic.api

    mock_hat.disable_speaker.return_value = None
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/hat/speaker", json={"enabled": False})
    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    mock_hat.disable_speaker.assert_called_once()

    nomothetic.api._hat_client = None


def test_set_speaker_no_client(client):
    """POST /api/hat/speaker returns 503 when daemon unavailable."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.post("/api/hat/speaker", json={"enabled": True})
    assert response.status_code == 503


def test_set_speaker_connection_error(client, mock_hat):
    """POST /api/hat/speaker returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.enable_speaker.side_effect = HatConnectionError("daemon gone")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/hat/speaker", json={"enabled": True})
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_set_speaker_hardware_error(client, mock_hat):
    """POST /api/hat/speaker returns 500 on HatError."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.enable_speaker.side_effect = HatError("HARDWARE_ERROR", "GPIO failed")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/hat/speaker", json={"enabled": True})
    assert response.status_code == 500

    nomothetic.api._hat_client = None


# ============================================================================
# Stream Endpoints (/api/stream/*)
# ============================================================================


def test_start_stream_success(client, mock_camera):
    """POST /api/stream/start starts stream and returns url + access token."""
    from unittest.mock import ANY, MagicMock, patch

    import nomothetic.api

    mock_server = MagicMock()
    mock_server.host = "0.0.0.0"
    mock_server.port = 8000

    nomothetic.api._camera = mock_camera
    nomothetic.api._stream_server = None

    with patch("nomothetic.api.StreamServer", return_value=mock_server) as MockStreamServer:
        response = client.post("/api/stream/start", json={})
        # Verify the existing camera instance is passed to avoid resource
        # conflicts, and that the server is armed with an access token (P10).
        # Loopback by default (review S-8): viewers use the TLS relay.
        MockStreamServer.assert_called_once_with(
            host="127.0.0.1", port=8000, camera=mock_camera, access_token=ANY
        )

    assert response.status_code == 200
    data = response.json()
    assert "url" in data
    assert "port" in data
    assert "timestamp" in data
    # The stream server runs outside the JWT'd API; the response must carry
    # the per-run token and embed it in the ready-to-open URL.
    assert data["token"]
    assert f"token={data['token']}" in data["url"]
    # live_path relays the stream over this same (trusted) TLS origin —
    # clients should join it with the device base URL, not host/port/url.
    assert data["live_path"] == f"/api/stream/live?token={data['token']}"
    mock_server.start_background.assert_called_once()

    # Cleanup
    nomothetic.api._camera = None
    nomothetic.api._stream_server = None


def test_start_stream_coerces_remote_bind_to_loopback(client, mock_camera, monkeypatch):
    """A non-loopback host is ignored unless NOMON_STREAM_ALLOW_REMOTE_BIND is set (S-8)."""
    from unittest.mock import ANY, MagicMock, patch

    import nomothetic.api

    monkeypatch.delenv("NOMON_STREAM_ALLOW_REMOTE_BIND", raising=False)
    mock_server = MagicMock()
    mock_server.host = "127.0.0.1"
    mock_server.port = 8000
    nomothetic.api._camera = mock_camera
    nomothetic.api._stream_server = None

    with patch("nomothetic.api.StreamServer", return_value=mock_server) as MockStreamServer:
        response = client.post("/api/stream/start", json={"host": "0.0.0.0"})
        MockStreamServer.assert_called_once_with(
            host="127.0.0.1", port=8000, camera=mock_camera, access_token=ANY
        )
    assert response.status_code == 200
    nomothetic.api._stream_server = None

    monkeypatch.setenv("NOMON_STREAM_ALLOW_REMOTE_BIND", "1")
    with patch("nomothetic.api.StreamServer", return_value=mock_server) as MockStreamServer:
        client.post("/api/stream/start", json={"host": "0.0.0.0"})
        MockStreamServer.assert_called_once_with(
            host="0.0.0.0", port=8000, camera=mock_camera, access_token=ANY
        )
    nomothetic.api._stream_server = None
    nomothetic.api._stream_token = None


def test_start_stream_already_running(client, mock_camera):
    """POST /api/stream/start returns existing url when stream is running."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_server = MagicMock()
    mock_server.host = "0.0.0.0"
    mock_server.port = 8001

    nomothetic.api._camera = mock_camera
    nomothetic.api._stream_server = mock_server
    nomothetic.api._stream_token = "existing-token"

    response = client.post("/api/stream/start", json={})
    assert response.status_code == 200
    data = response.json()
    assert data["port"] == 8001
    assert data["live_path"] == "/api/stream/live?token=existing-token"

    # Cleanup
    nomothetic.api._camera = None
    nomothetic.api._stream_server = None
    nomothetic.api._stream_token = None


def test_start_stream_no_camera(client):
    """POST /api/stream/start returns 503 when camera unavailable."""
    import nomothetic.api

    nomothetic.api._camera = None

    response = client.post("/api/stream/start", json={})
    assert response.status_code == 503


def test_stop_stream_success(client):
    """POST /api/stream/stop stops a running stream and returns success=true."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_server = MagicMock()
    nomothetic.api._stream_server = mock_server

    response = client.post("/api/stream/stop")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    mock_server.close.assert_called_once()
    assert nomothetic.api._stream_server is None


def test_stop_stream_not_running(client):
    """POST /api/stream/stop returns success=false when no stream is running."""
    import nomothetic.api

    nomothetic.api._stream_server = None

    response = client.post("/api/stream/stop")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False


def test_get_stream_status_running(client):
    """GET /api/stream/status returns running=true when stream is active."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_server = MagicMock()
    mock_server.host = "0.0.0.0"
    mock_server.port = 8000
    nomothetic.api._stream_server = mock_server

    response = client.get("/api/stream/status")
    assert response.status_code == 200
    data = response.json()
    assert data["running"] is True
    assert data["url"] is not None

    nomothetic.api._stream_server = None


def test_get_stream_status_not_running(client):
    """GET /api/stream/status returns running=false when no stream is active."""
    import nomothetic.api

    nomothetic.api._stream_server = None

    response = client.get("/api/stream/status")
    assert response.status_code == 200
    data = response.json()
    assert data["running"] is False
    assert data["url"] is None


def test_stream_live_not_running(client):
    """GET /api/stream/live returns 404 when no stream is running."""
    import nomothetic.api

    nomothetic.api._stream_server = None
    nomothetic.api._stream_token = None

    response = client.get("/api/stream/live", params={"token": "anything"})
    assert response.status_code == 404


def test_stream_live_invalid_token(client):
    """GET /api/stream/live returns 403 when the token doesn't match."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_server = MagicMock()
    mock_server.port = 8000
    nomothetic.api._stream_server = mock_server
    nomothetic.api._stream_token = "correct-token"

    response = client.get("/api/stream/live", params={"token": "wrong-token"})
    assert response.status_code == 403

    nomothetic.api._stream_server = None
    nomothetic.api._stream_token = None


def test_stream_live_relays_upstream(client):
    """GET /api/stream/live relays bytes from the internal stream server."""
    from unittest.mock import AsyncMock, MagicMock, patch

    import nomothetic.api

    mock_server = MagicMock()
    mock_server.port = 8000
    nomothetic.api._stream_server = mock_server
    nomothetic.api._stream_token = "good-token"

    async def _aiter_raw():
        yield b"--frame\r\n"
        yield b"fake-jpeg-bytes"

    mock_upstream = MagicMock()
    mock_upstream.headers = {"content-type": "multipart/x-mixed-replace; boundary=frame"}
    mock_upstream.aiter_raw = _aiter_raw
    mock_upstream.aclose = AsyncMock()

    mock_client = MagicMock()
    mock_client.build_request = MagicMock(return_value="built-request")
    mock_client.send = AsyncMock(return_value=mock_upstream)
    mock_client.aclose = AsyncMock()

    with patch("nomothetic.api.httpx.AsyncClient", return_value=mock_client):
        response = client.get("/api/stream/live", params={"token": "good-token"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "multipart/x-mixed-replace; boundary=frame"
    assert response.content == b"--frame\r\nfake-jpeg-bytes"
    mock_client.send.assert_called_once()

    nomothetic.api._stream_server = None
    nomothetic.api._stream_token = None


# ============================================================================
# Audio Recording Endpoints (/api/audio/record/*)
# ============================================================================


def test_start_audio_recording_success(client):
    """POST /api/audio/record/start returns recording=true and filename."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_recorder = MagicMock()
    mock_recorder.is_recording = False
    mock_recorder.start.return_value = "/tmp/recording_001.wav"
    nomothetic.api._audio_recorder = mock_recorder

    response = client.post("/api/audio/record/start", json={})
    assert response.status_code == 200
    data = response.json()
    assert data["recording"] is True
    assert data["filename"] == "recording_001.wav"
    assert "timestamp" in data

    nomothetic.api._audio_recorder = None


def test_start_audio_recording_already_recording(client):
    """POST /api/audio/record/start returns 409 when already recording."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_recorder = MagicMock()
    mock_recorder.is_recording = True
    nomothetic.api._audio_recorder = mock_recorder

    response = client.post("/api/audio/record/start", json={})
    assert response.status_code == 409

    nomothetic.api._audio_recorder = None


def test_start_audio_recording_no_recorder(client):
    """POST /api/audio/record/start returns 503 when audio unavailable."""
    import nomothetic.api

    nomothetic.api._audio_recorder = None

    response = client.post("/api/audio/record/start", json={})
    assert response.status_code == 503


def test_start_audio_recording_with_filename(client):
    """POST /api/audio/record/start passes filename to recorder."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_recorder = MagicMock()
    mock_recorder.is_recording = False
    mock_recorder.start.return_value = "/tmp/custom.wav"
    nomothetic.api._audio_recorder = mock_recorder

    response = client.post("/api/audio/record/start", json={"filename": "custom.wav"})
    assert response.status_code == 200
    mock_recorder.start.assert_called_once_with("custom.wav")

    nomothetic.api._audio_recorder = None


def test_start_audio_recording_invalid_filename(client):
    """POST /api/audio/record/start returns 400 for path-traversal filenames."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_recorder = MagicMock()
    mock_recorder.is_recording = False
    mock_recorder.start.side_effect = ValueError("filename cannot contain path separators")
    nomothetic.api._audio_recorder = mock_recorder

    response = client.post("/api/audio/record/start", json={"filename": "../escape.wav"})
    assert response.status_code == 400

    nomothetic.api._audio_recorder = None


def test_stop_audio_recording_success(client):
    """POST /api/audio/record/stop returns recording=false and filename."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_recorder = MagicMock()
    mock_recorder.stop.return_value = "/tmp/recording_001.wav"
    nomothetic.api._audio_recorder = mock_recorder

    response = client.post("/api/audio/record/stop")
    assert response.status_code == 200
    data = response.json()
    assert data["recording"] is False
    assert data["filename"] == "recording_001.wav"

    nomothetic.api._audio_recorder = None


def test_stop_audio_recording_not_recording(client):
    """POST /api/audio/record/stop returns filename=null when not recording."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_recorder = MagicMock()
    mock_recorder.stop.return_value = None
    nomothetic.api._audio_recorder = mock_recorder

    response = client.post("/api/audio/record/stop")
    assert response.status_code == 200
    assert response.json()["filename"] is None

    nomothetic.api._audio_recorder = None


def test_stop_audio_recording_no_recorder(client):
    """POST /api/audio/record/stop returns 503 when audio unavailable."""
    import nomothetic.api

    nomothetic.api._audio_recorder = None

    response = client.post("/api/audio/record/stop")
    assert response.status_code == 503


# ============================================================================
# Audio Playback Endpoints (/api/audio/play, /api/audio/play/stop)
# ============================================================================


def test_play_audio_success(client, mock_hat):
    """POST /api/audio/play starts playback and returns playing=true."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_player = MagicMock()
    mock_player.is_playing = False
    mock_player.current_file = "/tmp/clip.wav"
    mock_hat.enable_speaker.return_value = None
    nomothetic.api._audio_player = mock_player
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/audio/play", json={"filename": "clip.wav"})
    assert response.status_code == 200
    data = response.json()
    assert data["playing"] is True
    assert data["filename"] == "clip.wav"
    mock_player.play.assert_called_once_with("clip.wav")

    nomothetic.api._audio_player = None
    nomothetic.api._hat_client = None


def test_play_audio_file_not_found(client):
    """POST /api/audio/play returns 404 when file is missing."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_player = MagicMock()
    mock_player.is_playing = False
    mock_player.play.side_effect = FileNotFoundError("not found")
    nomothetic.api._audio_player = mock_player

    response = client.post("/api/audio/play", json={"filename": "missing.wav"})
    assert response.status_code == 404

    nomothetic.api._audio_player = None


def test_play_audio_already_playing(client):
    """POST /api/audio/play returns 409 when playback is in progress."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_player = MagicMock()
    mock_player.is_playing = True
    nomothetic.api._audio_player = mock_player

    response = client.post("/api/audio/play", json={"filename": "clip.wav"})
    assert response.status_code == 409

    nomothetic.api._audio_player = None


def test_play_audio_no_player(client):
    """POST /api/audio/play returns 503 when audio unavailable."""
    import nomothetic.api

    nomothetic.api._audio_player = None

    response = client.post("/api/audio/play", json={"filename": "clip.wav"})
    assert response.status_code == 503


def test_stop_audio_playback_success(client, mock_hat):
    """POST /api/audio/play/stop returns success=true."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_player = MagicMock()
    mock_hat.disable_speaker.return_value = None
    nomothetic.api._audio_player = mock_player
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/audio/play/stop")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    mock_player.stop.assert_called_once()

    nomothetic.api._audio_player = None
    nomothetic.api._hat_client = None


def test_stop_audio_playback_no_player(client):
    """POST /api/audio/play/stop returns 503 when audio unavailable."""
    import nomothetic.api

    nomothetic.api._audio_player = None

    response = client.post("/api/audio/play/stop")
    assert response.status_code == 503


# ============================================================================
# Audio Files and Status (/api/audio/files, /api/audio/status)
# ============================================================================


def test_list_audio_files_endpoint(client):
    """GET /api/audio/files returns list of WAV filenames."""
    from unittest.mock import patch

    with patch("nomothetic.api.list_audio_files", return_value=["a.wav", "b.wav"]):
        response = client.get("/api/audio/files")

    assert response.status_code == 200
    data = response.json()
    assert data["files"] == ["a.wav", "b.wav"]
    assert "timestamp" in data


def test_get_audio_status_idle(client):
    """GET /api/audio/status returns idle state when not recording or playing."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_recorder = MagicMock()
    mock_recorder.is_recording = False
    mock_recorder.current_file = None
    mock_player = MagicMock()
    mock_player.is_playing = False
    mock_player.current_file = None
    nomothetic.api._audio_recorder = mock_recorder
    nomothetic.api._audio_player = mock_player

    response = client.get("/api/audio/status")
    assert response.status_code == 200
    data = response.json()
    assert data["recording"] is False
    assert data["recording_file"] is None
    assert data["playing"] is False
    assert data["playback_file"] is None

    nomothetic.api._audio_recorder = None
    nomothetic.api._audio_player = None


def test_get_audio_status_active(client):
    """GET /api/audio/status reflects active recorder and player state."""
    from unittest.mock import MagicMock

    import nomothetic.api

    mock_recorder = MagicMock()
    mock_recorder.is_recording = True
    mock_recorder.current_file = "/tmp/rec.wav"
    mock_player = MagicMock()
    mock_player.is_playing = True
    mock_player.current_file = "/tmp/play.wav"
    nomothetic.api._audio_recorder = mock_recorder
    nomothetic.api._audio_player = mock_player

    response = client.get("/api/audio/status")
    assert response.status_code == 200
    data = response.json()
    assert data["recording"] is True
    assert data["recording_file"] == "rec.wav"
    assert data["playing"] is True
    assert data["playback_file"] == "play.wav"

    nomothetic.api._audio_recorder = None
    nomothetic.api._audio_player = None


# ============================================================================
# Audio Level Endpoints (/api/audio/volume, /api/audio/mic-gain)
# ============================================================================


def test_set_volume_success(client, mock_hat):
    """POST /api/audio/volume returns the applied volume_pct and timestamp."""
    import nomothetic.api

    mock_hat.set_volume.return_value = None
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/audio/volume", json={"volume_pct": 75})
    assert response.status_code == 200
    data = response.json()
    assert data["volume_pct"] == 75
    assert "timestamp" in data
    mock_hat.set_volume.assert_called_once_with(75)

    nomothetic.api._hat_client = None


def test_get_volume_success(client, mock_hat):
    """GET /api/audio/volume returns current volume_pct from the mixer."""
    import nomothetic.api

    mock_hat.get_volume.return_value = 65
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/audio/volume")
    assert response.status_code == 200
    data = response.json()
    assert data["volume_pct"] == 65
    assert "timestamp" in data
    mock_hat.get_volume.assert_called_once()

    nomothetic.api._hat_client = None


def test_set_volume_no_client(client):
    """POST /api/audio/volume returns 503 when daemon unavailable."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.post("/api/audio/volume", json={"volume_pct": 80})
    assert response.status_code == 503


def test_get_volume_no_client(client):
    """GET /api/audio/volume returns 503 when daemon unavailable."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.get("/api/audio/volume")
    assert response.status_code == 503


def test_set_volume_connection_error(client, mock_hat):
    """POST /api/audio/volume returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.set_volume.side_effect = HatConnectionError("daemon gone")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/audio/volume", json={"volume_pct": 50})
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_set_volume_hardware_error(client, mock_hat):
    """POST /api/audio/volume returns 500 on HatError."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.set_volume.side_effect = HatError("HARDWARE_ERROR", "amixer failed")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/audio/volume", json={"volume_pct": 50})
    assert response.status_code == 500

    nomothetic.api._hat_client = None


def test_get_volume_hardware_error(client, mock_hat):
    """GET /api/audio/volume returns 500 on HatError."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.get_volume.side_effect = HatError("HARDWARE_ERROR", "amixer failed")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/audio/volume")
    assert response.status_code == 500

    nomothetic.api._hat_client = None


def test_set_mic_gain_success(client, mock_hat):
    """POST /api/audio/mic-gain returns the applied gain_pct and timestamp."""
    import nomothetic.api

    mock_hat.set_mic_gain.return_value = None
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/audio/mic-gain", json={"gain_pct": 60})
    assert response.status_code == 200
    data = response.json()
    assert data["gain_pct"] == 60
    assert "timestamp" in data
    mock_hat.set_mic_gain.assert_called_once_with(60)

    nomothetic.api._hat_client = None


def test_get_mic_gain_success(client, mock_hat):
    """GET /api/audio/mic-gain returns current gain_pct from the mixer."""
    import nomothetic.api

    mock_hat.get_mic_gain.return_value = 45
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/audio/mic-gain")
    assert response.status_code == 200
    data = response.json()
    assert data["gain_pct"] == 45
    assert "timestamp" in data
    mock_hat.get_mic_gain.assert_called_once()

    nomothetic.api._hat_client = None


def test_set_mic_gain_no_client(client):
    """POST /api/audio/mic-gain returns 503 when daemon unavailable."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.post("/api/audio/mic-gain", json={"gain_pct": 50})
    assert response.status_code == 503


def test_get_mic_gain_no_client(client):
    """GET /api/audio/mic-gain returns 503 when daemon unavailable."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.get("/api/audio/mic-gain")
    assert response.status_code == 503


def test_set_mic_gain_connection_error(client, mock_hat):
    """POST /api/audio/mic-gain returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.set_mic_gain.side_effect = HatConnectionError("daemon gone")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/audio/mic-gain", json={"gain_pct": 50})
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_set_mic_gain_hardware_error(client, mock_hat):
    """POST /api/audio/mic-gain returns 500 on HatError."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.set_mic_gain.side_effect = HatError("HARDWARE_ERROR", "amixer failed")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/audio/mic-gain", json={"gain_pct": 50})
    assert response.status_code == 500

    nomothetic.api._hat_client = None


def test_get_mic_gain_hardware_error(client, mock_hat):
    """GET /api/audio/mic-gain returns 500 on HatError."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.get_mic_gain.side_effect = HatError("HARDWARE_ERROR", "amixer failed")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/audio/mic-gain")
    assert response.status_code == 500

    nomothetic.api._hat_client = None


# ===========================================================================
# _parse_int_env helper
# ===========================================================================


def test_parse_int_env_valid_string_is_parsed(monkeypatch):
    """A valid integer string is parsed and returned unchanged."""
    import nomothetic.api

    monkeypatch.setenv("_TEST_INT_ENV", "42")
    result = nomothetic.api._parse_int_env("_TEST_INT_ENV", 10, lo=0, hi=100)
    assert result == 42


def test_parse_int_env_missing_env_returns_default(monkeypatch):
    """When the env var is absent, the default is returned."""
    import nomothetic.api

    monkeypatch.delenv("_TEST_INT_ENV", raising=False)
    result = nomothetic.api._parse_int_env("_TEST_INT_ENV", 77, lo=0, hi=100)
    assert result == 77


def test_parse_int_env_non_integer_falls_back_to_default(monkeypatch, caplog):
    """A non-integer value falls back to the default and emits a WARNING."""
    import logging

    import nomothetic.api

    monkeypatch.setenv("_TEST_INT_ENV", "not_a_number")
    with caplog.at_level(logging.WARNING, logger="nomothetic.api"):
        result = nomothetic.api._parse_int_env("_TEST_INT_ENV", 80, lo=0, hi=100)
    assert result == 80
    assert any("_TEST_INT_ENV" in r.message for r in caplog.records)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_parse_int_env_out_of_range_falls_back_to_default(monkeypatch, caplog):
    """An integer outside the allowed range falls back to the default with a WARNING."""
    import logging

    import nomothetic.api

    monkeypatch.setenv("_TEST_INT_ENV", "150")
    with caplog.at_level(logging.WARNING, logger="nomothetic.api"):
        result = nomothetic.api._parse_int_env("_TEST_INT_ENV", 80, lo=0, hi=100)
    assert result == 80
    assert any("_TEST_INT_ENV" in r.message for r in caplog.records)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_parse_int_env_boundary_values_accepted(monkeypatch):
    """Values at exactly 0 and 100 are accepted (inclusive bounds)."""
    import nomothetic.api

    monkeypatch.setenv("_TEST_INT_ENV", "0")
    assert nomothetic.api._parse_int_env("_TEST_INT_ENV", 50, lo=0, hi=100) == 0

    monkeypatch.setenv("_TEST_INT_ENV", "100")
    assert nomothetic.api._parse_int_env("_TEST_INT_ENV", 50, lo=0, hi=100) == 100


# ===========================================================================
# Calibration API tests
# ===========================================================================


def test_get_calibration_success(client, mock_hat):
    """GET /api/calibration returns full snapshot and timestamp."""
    import nomothetic.api
    from nomothetic.hat import (
        CalibrationSnapshot,
        GrayscaleCalibrationEntry,
        MotorCalibrationEntry,
        ServoCalibrationEntry,
    )

    snap = CalibrationSnapshot(
        motors=[
            MotorCalibrationEntry(channel=0, speed_scale=1.0, deadband_pct=0.0, reversed=False)
        ],
        servos={"steering": ServoCalibrationEntry(servo="steering", trim_us=0)},
        grayscale=[GrayscaleCalibrationEntry(adc_channel=0, white_raw=100, black_raw=3000)],
    )
    mock_hat.get_calibration.return_value = snap
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/calibration")
    assert response.status_code == 200
    data = response.json()
    assert "motors" in data
    assert "servos" in data
    assert "grayscale" in data
    assert "timestamp" in data
    mock_hat.get_calibration.assert_called_once()

    nomothetic.api._hat_client = None


def test_get_calibration_no_client(client):
    """GET /api/calibration returns 503 when daemon unavailable."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.get("/api/calibration")
    assert response.status_code == 503


def test_get_calibration_connection_error(client, mock_hat):
    """GET /api/calibration returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.get_calibration.side_effect = HatConnectionError("daemon gone")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/calibration")
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_put_motor_calibration_success(client, mock_hat):
    """PUT /api/calibration/motor/0 returns updated motor entry."""
    import nomothetic.api
    from nomothetic.hat import MotorCalibrationEntry

    mock_hat.set_motor_calibration.return_value = MotorCalibrationEntry(
        channel=0, speed_scale=1.2, deadband_pct=5.0, reversed=True
    )
    nomothetic.api._hat_client = mock_hat

    response = client.put(
        "/api/calibration/motor/0",
        json={"speed_scale": 1.2, "deadband_pct": 5.0, "reversed": True},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["channel"] == 0
    assert data["speed_scale"] == 1.2
    assert "timestamp" in data
    mock_hat.set_motor_calibration.assert_called_once_with(0, 1.2, 5.0, True)

    nomothetic.api._hat_client = None


def test_put_motor_calibration_no_client(client):
    """PUT /api/calibration/motor/0 returns 503 when daemon unavailable."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.put("/api/calibration/motor/0", json={"speed_scale": 1.0})
    assert response.status_code == 503


def test_put_motor_calibration_invalid_channel(client, mock_hat):
    """PUT /api/calibration/motor/99 returns 422 on INVALID_PARAMS."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.set_motor_calibration.side_effect = HatError("INVALID_PARAMS", "channel out of range")
    nomothetic.api._hat_client = mock_hat

    response = client.put("/api/calibration/motor/99", json={"speed_scale": 1.0})
    assert response.status_code == 422

    nomothetic.api._hat_client = None


def test_put_motor_calibration_body_validation(client, mock_hat):
    """PUT /api/calibration/motor/0 returns 422 on out-of-range speed_scale."""
    import nomothetic.api

    nomothetic.api._hat_client = mock_hat
    response = client.put("/api/calibration/motor/0", json={"speed_scale": 99.9})
    assert response.status_code == 422

    nomothetic.api._hat_client = None


def test_put_servo_calibration_success(client, mock_hat):
    """PUT /api/calibration/servo/steering returns updated servo entry."""
    import nomothetic.api
    from nomothetic.hat import ServoCalibrationEntry

    mock_hat.set_servo_calibration.return_value = ServoCalibrationEntry(
        servo="steering", trim_us=-50
    )
    nomothetic.api._hat_client = mock_hat

    response = client.put("/api/calibration/servo/steering", json={"trim_us": -50})
    assert response.status_code == 200
    data = response.json()
    assert data["servo"] == "steering"
    assert data["trim_us"] == -50
    assert "timestamp" in data
    mock_hat.set_servo_calibration.assert_called_once_with("steering", -50)

    nomothetic.api._hat_client = None


def test_put_servo_calibration_no_client(client):
    """PUT /api/calibration/servo/steering returns 503 when daemon unavailable."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.put("/api/calibration/servo/steering", json={"trim_us": 0})
    assert response.status_code == 503


def test_put_servo_calibration_invalid_name(client, mock_hat):
    """PUT /api/calibration/servo/bad returns 422 on INVALID_PARAMS."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.set_servo_calibration.side_effect = HatError("INVALID_PARAMS", "unrecognised servo")
    nomothetic.api._hat_client = mock_hat

    response = client.put("/api/calibration/servo/bad_servo", json={"trim_us": 0})
    assert response.status_code == 422

    nomothetic.api._hat_client = None


def test_post_calibrate_grayscale_success(client, mock_hat):
    """POST /api/calibration/grayscale/0/capture returns capture result."""
    import nomothetic.api
    from nomothetic.hat import GrayscaleCaptureResult

    mock_hat.calibrate_grayscale.return_value = GrayscaleCaptureResult(
        channel=0, adc_channel=0, surface="white", raw_value=142, stored=True
    )
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/calibration/grayscale/0/capture", json={"surface": "white"})
    assert response.status_code == 200
    data = response.json()
    assert data["channel"] == 0
    assert data["adc_channel"] == 0
    assert data["surface"] == "white"
    assert data["stored"] is True
    assert "timestamp" in data
    mock_hat.calibrate_grayscale.assert_called_once_with(0, "white")

    nomothetic.api._hat_client = None


def test_post_calibrate_grayscale_no_client(client):
    """POST /api/calibration/grayscale/0/capture returns 503 when daemon unavailable."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.post("/api/calibration/grayscale/0/capture", json={"surface": "white"})
    assert response.status_code == 503


def test_post_calibrate_grayscale_constraint_violation(client, mock_hat):
    """POST /api/calibration/grayscale/0/capture returns 422 on constraint violation."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.calibrate_grayscale.side_effect = HatError("INVALID_PARAMS", "white_raw >= black_raw")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/calibration/grayscale/0/capture", json={"surface": "black"})
    assert response.status_code == 422

    nomothetic.api._hat_client = None


def test_post_calibrate_grayscale_invalid_surface(client, mock_hat):
    """POST /api/calibration/grayscale/0/capture returns 422 on invalid surface."""
    import nomothetic.api

    nomothetic.api._hat_client = mock_hat
    response = client.post("/api/calibration/grayscale/0/capture", json={"surface": "grey"})
    assert response.status_code == 422

    nomothetic.api._hat_client = None


def test_post_save_calibration_success(client, mock_hat):
    """POST /api/calibration/save returns saved=true and path."""
    import nomothetic.api
    from nomothetic.hat import SaveCalibrationResult

    mock_hat.save_calibration.return_value = SaveCalibrationResult(
        saved=True, path="/etc/nomopractic/calibration.toml"
    )
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/calibration/save")
    assert response.status_code == 200
    data = response.json()
    assert data["saved"] is True
    assert data["path"] == "/etc/nomopractic/calibration.toml"
    assert "timestamp" in data

    nomothetic.api._hat_client = None


def test_post_save_calibration_no_client(client):
    """POST /api/calibration/save returns 503 when daemon unavailable."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.post("/api/calibration/save")
    assert response.status_code == 503


def test_post_save_calibration_connection_error(client, mock_hat):
    """POST /api/calibration/save returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.save_calibration.side_effect = HatConnectionError("daemon gone")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/calibration/save")
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_post_reset_calibration_success(client, mock_hat):
    """POST /api/calibration/reset returns reset=true."""
    import nomothetic.api

    mock_hat.reset_calibration.return_value = True
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/calibration/reset")
    assert response.status_code == 200
    data = response.json()
    assert data["reset"] is True
    assert "timestamp" in data

    nomothetic.api._hat_client = None


def test_post_reset_calibration_no_client(client):
    """POST /api/calibration/reset returns 503 when daemon unavailable."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.post("/api/calibration/reset")
    assert response.status_code == 503


def test_get_grayscale_normalized_success(client, mock_hat):
    """GET /api/sensor/grayscale/normalized returns channels and normalized values."""
    import nomothetic.api
    from nomothetic.hat import NormalizedGrayscaleResult

    mock_hat.read_grayscale_normalized.return_value = NormalizedGrayscaleResult(
        channels=[0, 1, 2], normalized=[0.04, 0.87, 0.11]
    )
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/sensor/grayscale/normalized")
    assert response.status_code == 200
    data = response.json()
    assert data["channels"] == [0, 1, 2]
    assert len(data["normalized"]) == 3
    assert "timestamp" in data
    mock_hat.read_grayscale_normalized.assert_called_once()

    nomothetic.api._hat_client = None


def test_get_grayscale_normalized_no_client(client):
    """GET /api/sensor/grayscale/normalized returns 503 when daemon unavailable."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.get("/api/sensor/grayscale/normalized")
    assert response.status_code == 503


def test_get_grayscale_normalized_connection_error(client, mock_hat):
    """GET /api/sensor/grayscale/normalized returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.read_grayscale_normalized.side_effect = HatConnectionError("daemon gone")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/sensor/grayscale/normalized")
    assert response.status_code == 503

    nomothetic.api._hat_client = None


# ---------------------------------------------------------------------------
# Routine endpoint tests
# ---------------------------------------------------------------------------


def test_post_start_routine_success(client, mock_hat):
    """POST /api/routine/start returns started routine info."""
    import nomothetic.api
    from nomothetic.hat import RoutineStartResult

    mock_hat.start_routine.return_value = RoutineStartResult(
        name="explore", started_at_uptime_s=1000
    )
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/routine/start", json={"name": "explore"})
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "explore"
    assert data["started_at_uptime_s"] == 1000
    assert "timestamp" in data
    mock_hat.start_routine.assert_called_once_with("explore", None, None, None, None)

    nomothetic.api._hat_client = None


def test_post_start_routine_no_client(client):
    """POST /api/routine/start returns 503 when daemon unavailable."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.post("/api/routine/start", json={"name": "explore"})
    assert response.status_code == 503


def test_post_start_routine_already_running(client, mock_hat):
    """POST /api/routine/start returns 409 when ALREADY_RUNNING."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.start_routine.side_effect = HatError("ALREADY_RUNNING", "routine already running")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/routine/start", json={"name": "explore"})
    assert response.status_code == 409

    nomothetic.api._hat_client = None


def test_post_start_routine_invalid_params(client, mock_hat):
    """POST /api/routine/start returns 422 when INVALID_PARAMS."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.start_routine.side_effect = HatError("INVALID_PARAMS", "unknown routine")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/routine/start", json={"name": "fly"})
    assert response.status_code == 422

    nomothetic.api._hat_client = None


def test_post_stop_routine_success(client, mock_hat):
    """POST /api/routine/stop returns final stats."""
    import nomothetic.api
    from nomothetic.hat import RoutineStopResult

    mock_hat.stop_routine.return_value = RoutineStopResult(
        name="explore",
        ran_for_s=30,
        obstacles_avoided=2,
        cliffs_avoided=1,
        stop_reason="commanded",
    )
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/routine/stop")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "explore"
    assert data["ran_for_s"] == 30
    assert data["obstacles_avoided"] == 2
    assert data["cliffs_avoided"] == 1
    assert data["stop_reason"] == "commanded"
    assert "timestamp" in data

    nomothetic.api._hat_client = None


def test_post_stop_routine_not_running(client, mock_hat):
    """POST /api/routine/stop returns 409 when no routine is running."""
    import nomothetic.api
    from nomothetic.hat import HatError

    mock_hat.stop_routine.side_effect = HatError("INVALID_PARAMS", "no routine running")
    nomothetic.api._hat_client = mock_hat

    response = client.post("/api/routine/stop")
    assert response.status_code == 409

    nomothetic.api._hat_client = None


def test_get_routine_status_running(client, mock_hat):
    """GET /api/routine/status returns running status."""
    import nomothetic.api
    from nomothetic.hat import RoutineStatusResult

    mock_hat.get_routine_status.return_value = RoutineStatusResult(
        running=True,
        name="explore",
        elapsed_s=10,
        obstacles_avoided=0,
        cliffs_avoided=0,
    )
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/routine/status")
    assert response.status_code == 200
    data = response.json()
    assert data["running"] is True
    assert data["name"] == "explore"
    assert data["elapsed_s"] == 10
    assert "timestamp" in data

    nomothetic.api._hat_client = None


def test_get_routine_status_idle(client, mock_hat):
    """GET /api/routine/status returns idle state correctly."""
    import nomothetic.api
    from nomothetic.hat import RoutineStatusResult

    mock_hat.get_routine_status.return_value = RoutineStatusResult(
        running=False, name=None, elapsed_s=None, obstacles_avoided=None, cliffs_avoided=None
    )
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/routine/status")
    assert response.status_code == 200
    data = response.json()
    assert data["running"] is False
    assert data["name"] is None
    assert data["elapsed_s"] is None

    nomothetic.api._hat_client = None


def test_get_routine_status_no_client(client):
    """GET /api/routine/status returns 503 when daemon unavailable."""
    import nomothetic.api

    nomothetic.api._hat_client = None
    response = client.get("/api/routine/status")
    assert response.status_code == 503


def test_get_routine_status_connection_error(client, mock_hat):
    """GET /api/routine/status returns 503 on HatConnectionError."""
    import nomothetic.api
    from nomothetic.hat import HatConnectionError

    mock_hat.get_routine_status.side_effect = HatConnectionError("daemon gone")
    nomothetic.api._hat_client = mock_hat

    response = client.get("/api/routine/status")
    assert response.status_code == 503

    nomothetic.api._hat_client = None


def test_post_start_routine_speed_out_of_range(client):
    """POST /api/routine/start returns 422 when speed_pct is out of valid range."""
    response = client.post("/api/routine/start", json={"name": "explore", "speed_pct": 150})
    assert response.status_code == 422
