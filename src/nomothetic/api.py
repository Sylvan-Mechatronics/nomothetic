"""HTTP REST API for camera control and monitoring.

This module provides a FastAPI-based REST API for remote camera
operations with HTTPS/TLS support and CORS for mobile clients.
Supports two deployment modes via ``NOMON_API_MODE``:

- **device** (default): hardware control endpoints (camera, HAT, audio, etc.)
- **central**: authentication and fleet management endpoints

See ADR-011 for design rationale.

Classes
-------
APIServer
    Manages the FastAPI application and uvicorn server lifecycle.

Functions
---------
create_app
    Factory function to create a configured FastAPI application.
create_self_signed_cert
    Generate self-signed TLS certificates for development/testing.
provision_tls_cert
    Provision TLS certs via Tailscale (preferred) or self-signed fallback.
"""

import asyncio
import importlib.metadata as _meta
import json
import logging
import os
import re
import secrets
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from nomothetic.mode import Mode, get_mode

# Device-mode imports (conditionally used)
try:
    from nomothetic.audio import AudioPlayer, AudioRecorder, list_audio_files
except ImportError:  # pragma: no cover
    AudioPlayer = None  # type: ignore[assignment,misc]
    AudioRecorder = None  # type: ignore[assignment,misc]
    list_audio_files = None  # type: ignore[assignment]

try:
    from nomothetic.camera import Camera
except ImportError:  # pragma: no cover
    Camera = None  # type: ignore[assignment,misc]

try:
    from nomothetic.hat import (
        HatClient,
        HatConnectionError,
        HatError,
    )
except ImportError:  # pragma: no cover
    HatClient = None  # type: ignore[assignment,misc]
    HatConnectionError = Exception  # type: ignore[assignment,misc]
    HatError = Exception  # type: ignore[assignment,misc]

try:
    from nomothetic.streaming import StreamServer
except ImportError:

    class StreamServer:  # type: ignore[no-redef]
        """Placeholder StreamServer used when Flask/web streaming is unavailable.

        This fallback ensures that the API module remains importable even if the
        optional Flask-based streaming stack is not installed. Any attempt to
        instantiate this class will raise a RuntimeError with guidance on how
        to enable streaming support.
        """

        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError(
                "nomothetic.streaming.StreamServer requires Flask. "
                "Install the 'nomothetic[web]' extra to enable streaming endpoints."
            )


logger = logging.getLogger(__name__)

# Module-level set that retains strong references to fire-and-forget asyncio
# Tasks.  Without this, CPython's GC may collect a task before it completes.
# Each task removes itself on completion via the done-callback.
_background_tasks: set["asyncio.Task[None]"] = set()

# ============================================================================
# Data Models
# ============================================================================


class CaptureRequest(BaseModel):
    """Still image capture request."""

    filename: str = Field(..., description="Filename for captured image (no path)")


class CaptureResponse(BaseModel):
    """Successful still image capture response."""

    success: bool
    filename: str
    timestamp: str
    message: str


class RecordRequest(BaseModel):
    """Video recording request."""

    filename: str = Field(..., description="Filename for video (no path)")
    encoder: Optional[str] = Field(default="h264", description="Video encoder: h264 or mjpeg")


class RecordStartResponse(BaseModel):
    """Video recording start response."""

    success: bool
    filename: str
    timestamp: str
    message: str


class RecordStopResponse(BaseModel):
    """Video recording stop response."""

    success: bool
    timestamp: str
    message: str


class CameraStatus(BaseModel):
    """Current camera and recording status."""

    camera_ready: bool
    recording: bool
    resolution: str
    fps: int
    encoder: str
    timestamp: str


class ErrorResponse(BaseModel):
    """Standard error response."""

    success: bool = False
    error: str
    timestamp: str


class BatteryResponse(BaseModel):
    """HAT battery voltage response."""

    voltage_v: float
    timestamp: str


class ServoRequest(BaseModel):
    """Servo angle control request."""

    channel: int = Field(..., ge=0, le=11, description="PWM channel (0–11)")
    angle_deg: float = Field(..., ge=0.0, le=180.0, description="Target angle (0–180°)")
    ttl_ms: int = Field(default=500, ge=100, le=5000, description="Lease TTL in ms")


class ServoResponse(BaseModel):
    """Servo angle control response."""

    channel: int
    angle_deg: float
    timestamp: str


class ResetResponse(BaseModel):
    """MCU reset response."""

    success: bool
    timestamp: str


class ServoLeaseItem(BaseModel):
    """A single active servo TTL lease entry."""

    channel: int
    ttl_remaining_ms: int
    conn_id: int


class ServoStatusResponse(BaseModel):
    """Active servo lease table response."""

    active_leases: list[ServoLeaseItem]
    timestamp: str


class McuStatusResponse(BaseModel):
    """MCU reset statistics response."""

    resets_since_start: int
    last_reset_s_ago: Optional[int]
    timestamp: str


class MotorRequest(BaseModel):
    """DC motor speed control request."""

    channel: int = Field(..., ge=0, le=3, description="Motor channel index (0–3)")
    speed_pct: float = Field(
        ..., ge=-100.0, le=100.0, description="Signed speed (-100 reverse to +100 forward)"
    )
    ttl_ms: int = Field(default=500, ge=100, le=5000, description="Lease TTL in ms")


class MotorResponse(BaseModel):
    """DC motor speed control response."""

    channel: int
    speed_pct: float
    timestamp: str


class StopMotorsResponse(BaseModel):
    """Response for stop_all_motors."""

    stopped: int
    timestamp: str


class MotorLeaseItem(BaseModel):
    """A single active motor TTL lease entry."""

    channel: int
    ttl_remaining_ms: int
    conn_id: int


class MotorStatusResponse(BaseModel):
    """Active motor lease table response."""

    active_leases: list[MotorLeaseItem]
    timestamp: str


# ---------------------------------------------------------------------------
# Vehicle-level request/response models (high-level convenience API)
# ---------------------------------------------------------------------------


class DriveRequest(BaseModel):
    """Coordinated all-motor drive request."""

    speed_pct: float = Field(
        ..., ge=-100.0, le=100.0, description="Signed speed (-100 reverse to +100 forward)"
    )
    ttl_ms: int = Field(default=500, ge=100, le=5000, description="Lease TTL in ms")


class DriveResponse(BaseModel):
    """Coordinated drive response."""

    speed_pct: float
    motors: int
    timestamp: str


class SteerRequest(BaseModel):
    """Steering servo request."""

    angle_deg: float = Field(
        ..., ge=0.0, le=180.0, description="Steering angle degrees (90 = straight)"
    )
    ttl_ms: int = Field(default=500, ge=100, le=5000, description="Lease TTL in ms")


class SteerResponse(BaseModel):
    """Steering response."""

    angle_deg: float
    timestamp: str


class PanRequest(BaseModel):
    """Camera pan servo request."""

    angle_deg: float = Field(..., ge=0.0, le=180.0, description="Pan angle degrees (90 = centre)")
    ttl_ms: int = Field(default=500, ge=100, le=5000, description="Lease TTL in ms")


class PanResponse(BaseModel):
    """Camera pan response."""

    angle_deg: float
    timestamp: str


class TiltRequest(BaseModel):
    """Camera tilt servo request."""

    angle_deg: float = Field(..., ge=0.0, le=180.0, description="Tilt angle degrees (90 = centre)")
    ttl_ms: int = Field(default=500, ge=100, le=5000, description="Lease TTL in ms")


class TiltResponse(BaseModel):
    """Camera tilt response."""

    angle_deg: float
    timestamp: str


class GrayscaleResponse(BaseModel):
    """Grayscale sensor ADC readings."""

    channels: list[int]
    values: list[int]
    timestamp: str


# ---------------------------------------------------------------------------
# Ultrasonic sensor models
# ---------------------------------------------------------------------------


class UltrasonicResponse(BaseModel):
    """Ultrasonic distance sensor reading.

    ``distance_cm`` is ``None`` when no object is detected within the sensor's
    valid range (2–400 cm) or when the echo pulse times out.  Callers should
    treat ``None`` as "no reading available".
    """

    distance_cm: float | None
    timestamp: str


# ---------------------------------------------------------------------------
# Speaker models
# ---------------------------------------------------------------------------


class SpeakerRequest(BaseModel):
    """Speaker amplifier enable/disable request."""

    enabled: bool = Field(..., description="True to enable amplifier, False to disable")


class SpeakerResponse(BaseModel):
    """Speaker amplifier state response."""

    enabled: bool
    timestamp: str


# ---------------------------------------------------------------------------
# Stream start/stop models
# ---------------------------------------------------------------------------


class StreamStartRequest(BaseModel):
    """Stream server start request (all fields optional — defaults from config)."""

    host: Optional[str] = Field(
        default=None, description="Bind host (default: from config or 0.0.0.0)"
    )
    port: Optional[int] = Field(
        default=None, ge=1, le=65535, description="Port (default: from config or 8000)"
    )


class StreamStartResponse(BaseModel):
    """Stream server started successfully."""

    url: str
    host: str
    port: int
    token: Optional[str] = None
    """Access token the client must send as ``?token=`` on every request to
    the stream server (which runs outside the authenticated REST API)."""
    timestamp: str


class StreamStopResponse(BaseModel):
    """Stream server stopped."""

    success: bool
    timestamp: str


class StreamStatusResponse(BaseModel):
    """Current stream server status."""

    running: bool
    url: Optional[str]
    timestamp: str


# ---------------------------------------------------------------------------
# Audio recording models
# ---------------------------------------------------------------------------


class AudioRecordStartRequest(BaseModel):
    """Start audio recording request."""

    filename: Optional[str] = Field(
        default=None,
        description="Output WAV filename (basename only). Auto-generated if absent.",
    )


class AudioRecordStartResponse(BaseModel):
    """Audio recording started."""

    recording: bool
    filename: str
    timestamp: str


class AudioRecordStopResponse(BaseModel):
    """Audio recording stopped."""

    recording: bool
    filename: Optional[str]
    timestamp: str


# ---------------------------------------------------------------------------
# Audio playback models
# ---------------------------------------------------------------------------


class AudioPlayRequest(BaseModel):
    """Start audio playback request."""

    filename: str = Field(..., description="WAV basename to play (no directory components)")


class AudioPlayResponse(BaseModel):
    """Playback started."""

    playing: bool
    filename: str
    timestamp: str


class AudioPlayStopResponse(BaseModel):
    """Playback stopped."""

    success: bool
    timestamp: str


class AudioFilesResponse(BaseModel):
    """List of available audio files."""

    files: list[str]
    timestamp: str


class AudioStatusResponse(BaseModel):
    """Current audio recorder and player state."""

    recording: bool
    recording_file: Optional[str]
    playing: bool
    playback_file: Optional[str]
    timestamp: str


# Audio level models
class VolumeRequest(BaseModel):
    """Request body for setting output volume."""

    volume_pct: int = Field(..., ge=0, le=100, description="Output volume (0–100 %)")


class VolumeResponse(BaseModel):
    """Current or newly applied output volume."""

    volume_pct: int
    timestamp: str


class MicGainRequest(BaseModel):
    """Request body for setting microphone capture gain."""

    gain_pct: int = Field(..., ge=0, le=100, description="Mic capture gain (0–100 %)")


class MicGainResponse(BaseModel):
    """Current or newly applied mic capture gain."""

    gain_pct: int
    timestamp: str


# Calibration models


class MotorCalibrationRequest(BaseModel):
    """Request body for setting motor calibration (all fields optional)."""

    speed_scale: Optional[float] = Field(default=None, ge=0.5, le=2.0)
    deadband_pct: Optional[float] = Field(default=None, ge=0.0, le=20.0)
    reversed: Optional[bool] = None


class MotorCalibrationResponse(BaseModel):
    """Motor calibration entry after update."""

    channel: int
    speed_scale: float
    deadband_pct: float
    reversed: bool
    timestamp: str


class ServoCalibrationRequest(BaseModel):
    """Request body for setting servo trim offset."""

    trim_us: int = Field(..., ge=-500, le=500)


class ServoCalibrationResponse(BaseModel):
    """Servo calibration entry after update."""

    servo: str
    trim_us: int
    timestamp: str


class GrayscaleCaptureRequest(BaseModel):
    """Request body for capturing a grayscale surface reference."""

    surface: Literal["white", "black"]


class GrayscaleCaptureResponse(BaseModel):
    """Result of a grayscale surface capture."""

    channel: int
    adc_channel: int
    surface: str
    raw_value: int
    stored: bool
    timestamp: str


class NormalizedGrayscaleResponse(BaseModel):
    """Per-channel normalised grayscale sensor readings."""

    channels: list[int]
    normalized: list[float]
    timestamp: str


class SaveCalibrationResponse(BaseModel):
    """Result of persisting calibration to disk."""

    saved: bool
    path: str
    timestamp: str


class ResetCalibrationResponse(BaseModel):
    """Result of resetting calibration to defaults."""

    reset: bool
    timestamp: str


# ---------------------------------------------------------------------------
# Routine models
# ---------------------------------------------------------------------------


class RoutineStartRequest(BaseModel):
    """Body for ``POST /api/routine/start``."""

    name: str = Field(description="Routine name.  Currently only 'explore' is supported.")
    speed_pct: Optional[float] = Field(
        default=None, ge=1.0, le=100.0, description="Forward speed override (1–100 %)."
    )
    obstacle_threshold_cm: Optional[float] = Field(
        default=None, gt=0.0, description="Obstacle detection distance in cm."
    )
    cliff_threshold_normalized: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="Normalised grayscale cliff threshold (0–1)."
    )
    max_duration_s: Optional[int] = Field(
        default=None, ge=1, description="Maximum run time in seconds."
    )


class RoutineStartResponse(BaseModel):
    """Result of starting a routine."""

    name: str
    started_at_uptime_s: int
    timestamp: str


class RoutineStopResponse(BaseModel):
    """Result of stopping a routine."""

    name: str
    ran_for_s: int
    obstacles_avoided: int
    cliffs_avoided: int
    stop_reason: str
    timestamp: str


class RoutineStatusResponse(BaseModel):
    """Current routine status."""

    running: bool
    name: Optional[str] = None
    elapsed_s: Optional[int] = None
    obstacles_avoided: Optional[int] = None
    cliffs_avoided: Optional[int] = None
    timestamp: str


class MotorCalibrationItem(BaseModel):
    """Per-channel motor calibration entry in a calibration snapshot."""

    channel: int
    speed_scale: float
    deadband_pct: float
    reversed: bool


class ServoCalibrationItem(BaseModel):
    """Per-servo calibration entry in a calibration snapshot."""

    trim_us: int


class GrayscaleCalibrationItem(BaseModel):
    """Per-sensor grayscale calibration entry in a calibration snapshot."""

    channel: int
    adc_channel: int
    white_raw: int
    black_raw: int


class CalibrationSnapshotResponse(BaseModel):
    """Full calibration snapshot returned by GET /api/calibration."""

    motors: list[MotorCalibrationItem]
    servos: dict[str, ServoCalibrationItem]
    grayscale: list[GrayscaleCalibrationItem]
    timestamp: str


class WifiProvisionRequest(BaseModel):
    """Request body for POST /api/device/network/configure."""

    ssid: str = Field(
        ..., min_length=1, max_length=32, description="Target network SSID (1–32 chars)"
    )
    password: str = Field(
        "",
        max_length=63,
        description="WPA2 passphrase (8–63 chars) or empty string for open networks",
    )

    @field_validator("ssid")
    @classmethod
    def _validate_ssid(cls, v: str) -> str:
        """Reject SSIDs with null bytes, control characters, or a leading dash.

        - Null bytes (\\x00) are invalid in SSIDs per IEEE 802.11.
        - Control characters (\\x01–\\x1f, \\x7f) are rejected for safety.
        - Leading ``-`` is rejected to prevent argument injection into nmcli,
          which could interpret a leading-dash SSID as an option flag.
        """
        if re.search(r"[\x00-\x1f\x7f]", v):
            raise ValueError("SSID must not contain null bytes or control characters")
        if v.startswith("-"):
            raise ValueError("SSID must not start with '-'")
        return v

    @field_validator("password")
    @classmethod
    def _validate_password_length(cls, v: str) -> str:
        """Reject non-empty passwords shorter than 8 characters."""
        if v != "" and len(v) < 8:
            raise ValueError(
                "WPA2 password must be at least 8 characters (or empty for open networks)"
            )
        return v


class WifiProvisionResponse(BaseModel):
    """Response body for POST /api/device/network/configure."""

    status: Literal["connecting"]


class WifiApRequest(BaseModel):
    """Request body for POST /api/device/wifi/ap."""

    enabled: bool = Field(..., description="True to activate the Soft AP, False to deactivate")


class WifiApResponse(BaseModel):
    """Response body for POST /api/device/wifi/ap."""

    status: Literal["up", "down"]
    timestamp: str


# ============================================================================
# Utility Functions
# ============================================================================


def _build_san_entries(IPv4Address):  # noqa: N803
    """Build SAN list from defaults plus NOMON_TLS_EXTRA_HOSTS env var.

    NOMON_TLS_EXTRA_HOSTS is a comma-separated list of hostnames or IPv4
    addresses to include in the certificate's Subject Alternative Names.
    Example: ``NOMON_TLS_EXTRA_HOSTS=desktop-0rkvlns-wsl,100.89.254.25``
    """
    from ipaddress import AddressValueError

    from cryptography import x509 as _x509

    entries = [
        _x509.DNSName("localhost"),
        _x509.IPAddress(IPv4Address("127.0.0.1")),
    ]
    extra = os.environ.get("NOMON_TLS_EXTRA_HOSTS", "")
    for token in (t.strip() for t in extra.split(",") if t.strip()):
        try:
            entries.append(_x509.IPAddress(IPv4Address(token)))
        except (AddressValueError, ValueError):
            entries.append(_x509.DNSName(token))
    return entries


def create_self_signed_cert(cert_path: Path, key_path: Path) -> None:
    """Generate a self-signed certificate for HTTPS.

    Creates a self-signed certificate and key pair suitable for
    development and testing. In production, use proper certificates.

    Parameters
    ----------
    cert_path : Path
        Path where certificate file (.pem) will be saved
    key_path : Path
        Path where private key file (.pem) will be saved

    Raises
    ------
    ImportError
        If cryptography package is not installed
    """
    try:
        from ipaddress import IPv4Address

        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as e:
        raise ImportError(
            "cryptography package required for certificate generation. "
            "Install with: pip install nomothetic[api]"
        ) from e

    # Skip if files already exist
    if cert_path.exists() and key_path.exists():
        return

    # Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )

    # Build certificate subject
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "State"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, "City"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "nomothetic"),
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        ]
    )

    # Create certificate
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365 * 10))
        .add_extension(
            x509.SubjectAlternativeName(_build_san_entries(IPv4Address)),
            critical=False,
        )
        .sign(private_key, hashes.SHA256(), default_backend())
    )

    # Write certificate
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    # Write private key
    key_path.parent.mkdir(parents=True, exist_ok=True)
    with open(key_path, "wb") as f:
        f.write(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )


def provision_tls_cert(cert_path: Path, key_path: Path) -> str:
    """Provision TLS certificates, preferring Tailscale-issued certs.

    Runs on every API startup:

    1. **Existing cert still valid** (>7 days remaining) → reuse as-is.
    2. **Tailscale available** → ``tailscale cert`` issues a Let's Encrypt-
       backed certificate for the node's MagicDNS FQDN, trusted by all
       major browsers and iOS with no manual trust setup.
    3. **Fallback** → generate a self-signed certificate (browsers show a
       security warning).

    .. note::

       All devices (dev machines, mobile, web, vehicles) are assumed to be
       on the same Tailscale tailnet for now.  Public hosting for users
       outside the tailnet is a future consideration — see ADR-001.

    Parameters
    ----------
    cert_path : Path
        Where the certificate PEM file should be written.
    key_path : Path
        Where the private key PEM file should be written.

    Returns
    -------
    str
        Certificate source: ``"existing"``, ``"tailscale"``, or
        ``"self-signed"``.
    """
    # ── 1. Reuse existing cert if still valid ────────────────────────────
    if cert_path.exists() and key_path.exists():
        try:
            from cryptography import x509
            from cryptography.hazmat.backends import default_backend

            with open(cert_path, "rb") as f:
                cert = x509.load_pem_x509_certificate(f.read(), default_backend())
            expiry = getattr(cert, "not_valid_after_utc", None)
            if expiry is None:
                expiry = cert.not_valid_after.replace(tzinfo=timezone.utc)
            remaining = expiry - datetime.now(timezone.utc)
            if remaining > timedelta(days=7):
                logger.info("TLS cert valid for %d more days, reusing.", remaining.days)
                return "existing"
            logger.info("TLS cert expires in %d days, renewing.", remaining.days)
        except Exception:
            logger.debug("Could not check existing cert, will reprovision.")

    # ── 2. Try Tailscale-issued cert ─────────────────────────────────────
    try:
        ts_status = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if ts_status.returncode != 0:
            logger.warning(
                "tailscale status failed (exit %d) — cannot provision Tailscale cert.\n"
                "  stdout: %s\n  stderr: %s",
                ts_status.returncode,
                ts_status.stdout.strip(),
                ts_status.stderr.strip(),
            )
        else:
            status = json.loads(ts_status.stdout)
            fqdn = status.get("Self", {}).get("DNSName", "").rstrip(".")
            if not fqdn:
                logger.warning(
                    "tailscale status returned no DNSName — is MagicDNS enabled "
                    "in the Tailscale admin console?"
                )
            else:
                cert_path.parent.mkdir(parents=True, exist_ok=True)
                ts_cert = subprocess.run(
                    [
                        "tailscale",
                        "cert",
                        "--cert-file",
                        str(cert_path),
                        "--key-file",
                        str(key_path),
                        fqdn,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if ts_cert.returncode == 0:
                    logger.info(
                        "Tailscale TLS cert provisioned for %s "
                        "(Let's Encrypt-backed, browser-trusted).",
                        fqdn,
                    )
                    return "tailscale"
                # Tailscale writes errors to stdout or stderr depending on version
                combined = "\n".join(filter(None, [ts_cert.stdout.strip(), ts_cert.stderr.strip()]))
                logger.warning(
                    "tailscale cert failed (exit %d) for %s:\n  %s\n"
                    "  Hint: the process may need elevated permissions — "
                    "try running as root or add your user to the 'tailscale' group.",
                    ts_cert.returncode,
                    fqdn,
                    combined or "(no output)",
                )
    except FileNotFoundError:
        logger.warning("Tailscale CLI not found — cannot provision Tailscale cert.")
    except subprocess.TimeoutExpired:
        logger.warning("Tailscale CLI timed out during cert provisioning.")
    except Exception as exc:
        logger.warning("Tailscale cert provisioning error: %s", exc)

    # ── 3. Fallback to self-signed ───────────────────────────────────────
    # Remove partial files from a failed Tailscale attempt before generating
    for p in (cert_path, key_path):
        if p.exists():
            p.unlink()
    create_self_signed_cert(cert_path, key_path)
    logger.warning(
        "Using self-signed TLS cert (browsers will show a warning). "
        "To use trusted certs, enable HTTPS certificates in the Tailscale "
        "admin console: https://login.tailscale.com/admin/dns"
    )
    return "self-signed"


# ============================================================================
# Global service instances
# ============================================================================


_camera: Optional[Camera] = None
_hat_client: Optional[HatClient] = None
_stream_server: Optional[StreamServer] = None
_stream_host: str = "0.0.0.0"
_stream_port: int = 8000
# Per-run access token required by the MJPEG stream server (regenerated on
# every stream start; None while no stream is running).
_stream_token: Optional[str] = None
_audio_recorder: Optional[AudioRecorder] = None


def _parse_int_env(name: str, default: int, lo: int = 0, hi: int = 100) -> int:
    """Parse an integer environment variable, falling back to *default* on error.

    Falls back and logs a warning when:
    - the value is not a valid integer, or
    - the value is outside the inclusive [lo, hi] range.
    """
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Environment variable %s=%r is not a valid integer; using default %d",
            name,
            raw,
            default,
        )
        return default
    if not lo <= value <= hi:
        logger.warning(
            "Environment variable %s=%r is outside allowed range %d–%d; using default %d",
            name,
            raw,
            lo,
            hi,
            default,
        )
        return default
    return value


# Default audio levels read from env vars (set by start.sh from config.toml).
_default_volume_pct: int = _parse_int_env("NOMON_AUDIO_VOLUME", 80)
_default_mic_gain_pct: int = _parse_int_env("NOMON_AUDIO_MIC_GAIN", 80)
_audio_player: Optional[AudioPlayer] = None
_media_dir: Path = Path("~/perceptua-nomon/media").expanduser()


# ============================================================================
# Dependency helpers (N1 – N3)
# ============================================================================


def _require_hat() -> "HatClient":
    """Return the HAT client or raise 503."""
    if _hat_client is None:
        raise HTTPException(status_code=503, detail="nomopractic daemon not available")
    return _hat_client


async def _hat_call(method: str, *args, **kwargs):
    """Call a HatClient method in a thread, mapping errors to HTTP responses."""
    hat = _require_hat()
    try:
        return await asyncio.to_thread(getattr(hat, method), *args, **kwargs)
    except HatConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except HatError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _require_audio_recorder():
    """Return the audio recorder or raise 503."""
    if _audio_recorder is None:
        raise HTTPException(status_code=503, detail="audio recording not available")
    return _audio_recorder


def _require_audio_player():
    """Return the audio player or raise 503."""
    if _audio_player is None:
        raise HTTPException(status_code=503, detail="audio playback not available")
    return _audio_player


def _require_camera():
    """Return the camera or raise 503."""
    if not _camera:
        raise HTTPException(status_code=503, detail="Camera not initialized")
    return _camera


def _grab_one_jpeg(cam) -> bytes:
    """Capture a single JPEG frame in memory (no disk write).

    Reuses the camera's MJPEG frame generator, taking exactly one frame and then
    closing the generator so the camera is started and stopped for this one grab
    (the generator stops the camera in its ``finally``). Blocking; call via
    ``asyncio.to_thread``.

    Parameters
    ----------
    cam : Camera
        The initialized camera.

    Returns
    -------
    bytes
        Raw JPEG-encoded frame.
    """
    gen = cam.get_jpeg_frame_generator()
    try:
        frame: bytes = next(gen)
        return frame
    finally:
        close = getattr(gen, "close", None)
        if callable(close):
            close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage camera, HAT client, and audio initialization and cleanup."""
    global _camera, _hat_client, _audio_recorder, _audio_player, _media_dir
    global _stream_host, _stream_port
    # Resolve media directory from environment (set by start.sh from config.toml)
    _media_dir = Path(os.environ.get("NOMON_MEDIA_DIR", "~/perceptua-nomon/media")).expanduser()
    # Resolve stream defaults from environment (set by start.sh from [stream] config)
    _stream_host = os.environ.get("NOM_STREAM_HOST", "0.0.0.0")
    _stream_port = int(os.environ.get("NOM_STREAM_PORT", "8000"))
    # Resolve camera capture settings from environment (set by start.sh, or
    # directly in .env.device, from [stream] config). Defaults match Camera's
    # own hardcoded defaults, so an unconfigured deployment is unchanged.
    _camera_index = int(os.environ.get("NOM_STREAM_CAMERA", "0"))
    _camera_width = int(os.environ.get("NOM_STREAM_WIDTH", "1280"))
    _camera_height = int(os.environ.get("NOM_STREAM_HEIGHT", "720"))
    _camera_fps = int(os.environ.get("NOM_STREAM_FPS", "30"))
    _camera_encoder = os.environ.get("NOM_STREAM_ENCODER", "h264")
    if _camera_encoder not in ("h264", "mjpeg"):
        logger.warning(
            "NOM_STREAM_ENCODER=%r is not 'h264' or 'mjpeg'; using 'h264'", _camera_encoder
        )
        _camera_encoder = "h264"
    # Startup: Initialize camera
    try:
        _camera = Camera(
            camera_index=_camera_index,
            width=_camera_width,
            height=_camera_height,
            fps=_camera_fps,
            encoder=_camera_encoder,
            directory=_media_dir / "videos",
            photo_directory=_media_dir / "photos",
        )
    except RuntimeError as e:
        logger.warning("Camera initialization failed; API will run without camera: %s", e)

    # Startup: Create HAT client (lazy connect — daemon may not be running yet)
    _hat_client = HatClient()

    # Startup: Initialize audio recorder and player
    _audio_recorder = AudioRecorder(audio_dir=_media_dir / "audio")
    _audio_player = AudioPlayer(audio_dir=_media_dir / "audio")

    # Startup: central-mode telemetry consumer (subscribes to MQTT, persists
    # readings). Needs the running event loop to schedule async store writes.
    telemetry_consumer = getattr(app.state, "telemetry_consumer", None)
    if telemetry_consumer is not None:
        try:
            telemetry_consumer.start_background(asyncio.get_running_loop())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to start telemetry consumer: %s", exc)

    # Startup: device-mode autonomy event forwarder (mirrors recorded routine
    # events onto the MQTT autonomy topic, best-effort).
    autonomy_forwarder = getattr(app.state, "autonomy_forwarder", None)
    if autonomy_forwarder is not None:
        try:
            autonomy_forwarder.start_background()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to start autonomy event forwarder: %s", exc)

    # Startup: on-robot wake-word listener (ADR-021). The event loop is
    # attached unconditionally so /api/voice/wake can enable the listener at
    # runtime; it auto-starts only when a wake phrase is configured.
    wake_listener = getattr(app.state, "wake_listener", None)
    if wake_listener is not None:
        wake_listener.attach_loop(asyncio.get_running_loop())
        if wake_listener.phrase:
            try:
                wake_listener.start_background()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to start wake-word listener: %s", exc)

    yield

    # Shutdown: stop telemetry consumer and autonomy forwarder
    telemetry_consumer = getattr(app.state, "telemetry_consumer", None)
    if telemetry_consumer is not None:
        telemetry_consumer.stop()
    autonomy_forwarder = getattr(app.state, "autonomy_forwarder", None)
    if autonomy_forwarder is not None:
        autonomy_forwarder.stop()

    # Shutdown: stop the wake-word listener before tearing down audio so its
    # thread cannot reopen the microphone mid-shutdown.
    wake_listener = getattr(app.state, "wake_listener", None)
    if wake_listener is not None:
        wake_listener.stop()

    # Shutdown: stop any active audio sessions
    if _audio_recorder and _audio_recorder.is_recording:
        _audio_recorder.stop()
    if _audio_player and _audio_player.is_playing:
        _audio_player.stop()

    # Shutdown: stop stream server if running
    if _stream_server is not None:
        _stream_server.close()

    # Shutdown: Cleanup camera and HAT
    if _camera:
        _camera.close()
    if _hat_client:
        _hat_client.close()

    # Shutdown: stop any routines this device launched (no orphaned autonomy)
    routine_manager = getattr(app.state, "routine_manager", None)
    if routine_manager is not None:
        await routine_manager.shutdown()

    # Shutdown: Close ArcadeDB client (central mode)
    db_client = getattr(app.state, "db_client", None)
    if db_client is not None:
        await db_client.close()


# ============================================================================
# FastAPI Application
# ============================================================================


def _setup_cors(app: FastAPI, mode: "Mode") -> None:
    """Add CORS middleware appropriate for the deployment mode."""
    if mode == Mode.CENTRAL:
        cors_origins_raw = os.environ.get("NOMON_CORS_ORIGINS", "http://localhost:8081")
        cors_origins = [o.strip() for o in cors_origins_raw.split(",") if o.strip()]
    else:
        device_cors_raw = os.environ.get("NOMON_CORS_ORIGINS", "https://10.0.0.1:8443")
        cors_origins = [o.strip() for o in device_cors_raw.split(",") if o.strip()]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
    )


def _setup_central_stores(app: FastAPI) -> None:
    """Initialise persistence stores for central mode and register routes."""
    from nomothetic.auth import AuthService, set_auth_service
    from nomothetic.auth_routes import create_auth_router
    from nomothetic.autonomy_store import AutonomyStore, InMemoryAutonomyStore
    from nomothetic.fleet_routes import (
        create_fleet_router,
        set_autonomy_store,
        set_fleet_store,
        set_telemetry_store,
    )
    from nomothetic.fleet_store import FleetStore, InMemoryFleetStore
    from nomothetic.rate_limit import RateLimiter
    from nomothetic.telemetry_store import InMemoryTelemetryStore, TelemetryStore
    from nomothetic.token_store import TokenStore
    from nomothetic.user_store import UserStore

    # Per-app rate limiters so each test client gets fresh instances
    app.state.login_limiter = RateLimiter(max_requests=5, window_seconds=60)
    app.state.register_limiter = RateLimiter(max_requests=10, window_seconds=60)

    # Database-backed stores when ArcadeDB is configured
    user_store: UserStore
    fleet_store: FleetStore
    token_store: TokenStore
    telemetry_store: TelemetryStore
    autonomy_store: AutonomyStore
    arcadedb_host = os.environ.get("ARCADEDB_HOST")
    if arcadedb_host:
        from nomothetic.autonomy_store import SqlAutonomyStore
        from nomothetic.db import DatabaseClient, DatabaseConfig
        from nomothetic.fleet_store import SqlFleetStore
        from nomothetic.telemetry_store import SqlTelemetryStore
        from nomothetic.token_store import SqlTokenStore
        from nomothetic.user_store import SqlUserStore

        db_config = DatabaseConfig.from_env()
        db_client = DatabaseClient(db_config)
        user_store = SqlUserStore(db_client)
        fleet_store = SqlFleetStore(db_client)
        token_store = SqlTokenStore(db_client)
        telemetry_store = SqlTelemetryStore(db_client)
        autonomy_store = SqlAutonomyStore(db_client)
        app.state.db_client = db_client
    else:
        from nomothetic.token_store import InMemoryTokenStore
        from nomothetic.user_store import InMemoryUserStore

        user_store = InMemoryUserStore()
        fleet_store = InMemoryFleetStore()
        token_store = InMemoryTokenStore()
        telemetry_store = InMemoryTelemetryStore()
        autonomy_store = InMemoryAutonomyStore()
        app.state.db_client = None

    auth_service = AuthService(user_store=user_store, token_store=token_store)
    set_auth_service(auth_service)
    app.include_router(create_auth_router())

    set_fleet_store(fleet_store)
    set_telemetry_store(telemetry_store)
    set_autonomy_store(autonomy_store)
    app.include_router(create_fleet_router())

    # Telemetry ingestion: subscribe to the MQTT broker (when configured) and
    # persist readings to the telemetry store — plus forwarded autonomy events
    # to the autonomy store (autonomon Phase 7).  No broker -> no consumer, and
    # telemetry/autonomy history is simply empty.  Started in the lifespan
    # (needs the running event loop); see ``lifespan``.
    app.state.telemetry_consumer = None
    if os.environ.get("NOMON_MQTT_BROKER", "").strip():
        try:
            from nomothetic.telemetry_consumer import TelemetryConsumer

            app.state.telemetry_consumer = TelemetryConsumer.from_env(
                telemetry_store, autonomy_store=autonomy_store
            )
        except ImportError as exc:
            logger.warning("Telemetry consumer unavailable (paho-mqtt missing): %s", exc)


def _register_device_routes(app: FastAPI, mode: "Mode") -> None:
    """Set up device-mode auth and register all hardware endpoint routes."""

    # ========================================================================
    # Device-mode auth (opt-in via NOMON_DEVICE_AUTH env var)
    # ========================================================================

    from fastapi import APIRouter, Depends

    from nomothetic.rate_limit import network_rate_limit

    device_auth_enabled = os.environ.get("NOMON_DEVICE_AUTH", "true").lower() in (
        "1",
        "true",
    )

    if device_auth_enabled:
        from nomothetic.auth import AuthService, jwt_required, set_auth_service
        from nomothetic.device_auth_routes import create_device_auth_router
        from nomothetic.pairing import PairingState
        from nomothetic.rate_limit import RateLimiter
        from nomothetic.token_store import InMemoryTokenStore
        from nomothetic.user_store import InMemoryUserStore

        pairing = PairingState()
        user_store = InMemoryUserStore()
        token_store = InMemoryTokenStore()
        auth_service = AuthService(
            secret=pairing.jwt_secret,
            issuer="nomon-device",
            user_store=user_store,
            token_store=token_store,
        )
        set_auth_service(auth_service)
        app.state.pairing_state = pairing
        app.state.pairing_limiter = RateLimiter(max_requests=3, window_seconds=60)
        app.state.network_limiter = RateLimiter(max_requests=5, window_seconds=60)

        # PairingState is always constructed fresh here, so is_paired() is
        # always False at this point. The guard is retained for clarity and
        # to make the intent explicit should persistent state be added later.
        if not pairing.is_paired():
            secret = pairing.load_or_generate_secret()
            # Write the secret to a file on tmpfs so the operator can read it
            # via SSH without it appearing in the journal.
            # StandardError=journal captures all stderr output, so printing
            # the secret value there would persist it in the journal.
            _secret_display_path = "/run/nomothetic/pairing-secret"
            try:
                import os as _os

                _fd = _os.open(
                    _secret_display_path,
                    _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC,
                    0o600,  # owner-read-only: pairing secret must not be world-readable
                )
                try:
                    _os.write(_fd, secret.encode())
                finally:
                    _os.close(_fd)
                logger.info(
                    "Pairing secret written to %s — read it there to pair",
                    _secret_display_path,
                )
            except OSError:
                logger.warning(
                    "Could not write pairing secret to %s",
                    _secret_display_path,
                    exc_info=True,
                )

        app.include_router(create_device_auth_router())

        # Plugin auth (ADR-019): on-device autonomy plugins obtain a device JWT
        # via Ed25519 challenge-response. These endpoints bootstrap plugin auth,
        # so they are registered on the bare app (not behind jwt_required).
        from nomothetic.plugin_auth import ChallengeStore, PluginKeyStore
        from nomothetic.plugin_auth_routes import create_plugin_auth_router

        app.state.plugin_key_store = PluginKeyStore()
        app.state.plugin_challenge_store = ChallengeStore()
        app.include_router(create_plugin_auth_router())

        device_router = APIRouter(
            dependencies=[Depends(jwt_required)],
        )
    else:
        logger.warning(
            "Device auth is disabled (NOMON_DEVICE_AUTH=false). "
            "All device endpoints are unauthenticated."
        )
        from nomothetic.rate_limit import RateLimiter as _RateLimiter

        app.state.network_limiter = _RateLimiter(max_requests=5, window_seconds=60)
        device_router = APIRouter()

    # Autonomy-routine status/log sink (push model; autonomon ADR-004). The
    # brain (autonomon) reports its own lifecycle events; this gateway only
    # stores and serves them — no interpretation. Included on device_router so
    # the endpoints inherit whatever auth device_router carries.
    from nomothetic.routine_log_store import RoutineLogStore
    from nomothetic.routine_routes import create_routine_router

    # Autonomy telemetry forwarding (autonomon Phase 7): mirror every recorded
    # routine event onto the MQTT autonomy topic so central nomothetic can
    # persist fleet-wide history.  Best-effort — no broker (or no paho-mqtt)
    # means autonomy history simply stays device-local.  Started in ``lifespan``.
    autonomy_forwarder = None
    if os.environ.get("NOMON_MQTT_BROKER", "").strip():
        try:
            from nomothetic.autonomy_forwarder import AutonomyEventForwarder

            autonomy_forwarder = AutonomyEventForwarder.from_env()
        except ImportError as exc:
            logger.warning("Autonomy event forwarding unavailable (paho-mqtt missing): %s", exc)
    app.state.autonomy_forwarder = autonomy_forwarder

    app.state.routine_log_store = RoutineLogStore(
        on_event=autonomy_forwarder.enqueue_event if autonomy_forwarder is not None else None
    )
    device_router.include_router(create_routine_router())

    # Autonomy-routine lifecycle control (start/stop/stop-all). nomothetic
    # supervises the autonomon plugin *process* (ADR-004: no cognition here) and
    # enforces a max-duration guard so a routine cannot run forever if the
    # operator loses connection. Credentials/connection come from config, never
    # the request payload.
    from nomothetic.routine_control_routes import create_routine_control_router
    from nomothetic.routine_manager import RoutineManager, RoutineManagerConfig

    app.state.routine_manager = RoutineManager(
        RoutineManagerConfig.from_env(),
        log_store=app.state.routine_log_store,
    )
    device_router.include_router(create_routine_control_router())

    # AI chat-command relay (Anthropic Claude). Operator chat becomes tool calls
    # onto the same validated device operations the app buttons use — drive/
    # steer/camera under TTL leases, sensor reads, routine start/stop — and the
    # tool surface is destructive-free (no MCU reset, no calibration writes).
    # This is operator convenience, not autonomy: no cognition or robot state
    # lives here (ADR-004). The Anthropic key is user-supplied via /api/ai/key
    # (0600 file on the device) with ANTHROPIC_API_KEY as operator fallback.
    from nomothetic.ai_command import AiCommandService, AiKeyStore
    from nomothetic.ai_routes import create_ai_router
    from nomothetic.rate_limit import RateLimiter as _AiRateLimiter
    from nomothetic.routine_catalog import autonomon_catalog
    from nomothetic.stt import VoskSttEngine
    from nomothetic.tts import EspeakTtsEngine

    app.state.ai_key_store = AiKeyStore()
    app.state.ai_service = AiCommandService(
        hat_call=_hat_call,
        get_routine_manager=lambda: getattr(app.state, "routine_manager", None),
        get_catalog=autonomon_catalog,
    )
    app.state.ai_limiter = _AiRateLimiter(max_requests=10, window_seconds=60)
    # Voice commands: the app uploads a recorded clip, the device transcribes
    # it locally (vosk small model, lazy-loaded on first use), and the text
    # feeds the same /api/ai/command path the keyboard uses. The engine is
    # swappable here without touching the route (see nomothetic.stt).
    app.state.stt_engine = VoskSttEngine()
    app.state.stt_limiter = _AiRateLimiter(max_requests=20, window_seconds=60)
    # Spoken feedback: the wake-word listener echoes the heard transcript back
    # over the speaker (espeak-ng), concurrently with the AI dispatch. Swappable
    # here without touching the listener (see nomothetic.tts). Construction is
    # side-effect free — espeak-ng/ffmpeg availability is checked lazily.
    app.state.tts_engine = EspeakTtsEngine()
    device_router.include_router(create_ai_router())

    # On-robot wake-word voice commands (ADR-021): the robot's own USB mic is
    # watched for a catch phrase, and heard commands run through the same
    # AiCommandService relay as the app's chat bar — still operator control,
    # no cognition here, autonomon uninvolved. The listener is always
    # constructed (construction is side-effect free, even without pyaudio/vosk)
    # so /api/voice/wake can enable it at runtime; it auto-starts in the
    # lifespan when NOMON_WAKE_PHRASE is set. Player/HAT/media dependencies
    # are late-bound lambdas because those globals are created in the lifespan.
    from nomothetic.wake import WakeWordListener
    from nomothetic.wake_routes import create_wake_router

    app.state.wake_listener = WakeWordListener(
        stt_engine=app.state.stt_engine,
        ai_service=app.state.ai_service,
        ai_key_store=app.state.ai_key_store,
        get_player=lambda: _audio_player,
        get_hat=lambda: _hat_client,
        get_chime_dir=lambda: _media_dir / "audio" / "chimes",
        tts_engine=app.state.tts_engine,
    )
    device_router.include_router(create_wake_router())

    # ========================================================================
    # Device-mode endpoints (only registered when NOMON_API_MODE=device)
    # ========================================================================

    @device_router.get("/api/sensor/grayscale", response_model=GrayscaleResponse, tags=["Sensor"])
    async def get_grayscale():
        """Read all three grayscale sensor ADC channels.

        Returns raw 12-bit ADC values for the left, center, and right
        grayscale sensors used for cliff and line detection.

        Returns
        -------
        GrayscaleResponse
            ADC channel numbers and raw readings (0–4095) per channel.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on hardware read failure.
        """
        result = await _hat_call("read_grayscale")
        return GrayscaleResponse(
            channels=result.channels,
            values=result.values,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.get("/api/sensor/ultrasonic", response_model=UltrasonicResponse, tags=["Sensor"])
    async def get_ultrasonic():
        """Trigger the ultrasonic sensor and return the measured distance.

        The nomopractic daemon drives the ultrasonic trigger line for 10 µs and times
        the echo pulse to compute the distance. GPIO mappings for trigger and echo
        are defined in the nomopractic configuration and IPC schema
        (see ``docs/hat_ipc_schema.md``). Valid range is 2–400 cm for
        HC-SR04-compatible sensors.

        Returns
        -------
        UltrasonicResponse
            ``distance_cm``: distance in centimetres, or ``None`` when no object
            is within sensor range or the echo pulse times out.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on hardware error (GPIO failure) or unexpected IPC errors.
        """
        hat = _require_hat()
        ts = datetime.now(timezone.utc).isoformat()
        try:
            result = await asyncio.to_thread(hat.read_ultrasonic)
        except HatConnectionError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        except HatError as e:
            if e.code in ("NO_ECHO", "TIMEOUT"):
                return UltrasonicResponse(distance_cm=None, timestamp=ts)
            raise HTTPException(status_code=500, detail=str(e)) from e
        return UltrasonicResponse(
            distance_cm=result.distance_cm,
            timestamp=ts,
        )

    # ========================================================================
    # Vehicle Endpoints (high-level convenience API)
    # ========================================================================

    @device_router.post("/api/drive", response_model=DriveResponse, tags=["Vehicle"])
    async def drive(request: DriveRequest):
        """Drive all configured DC motors at the same speed simultaneously.

        Sends a single coordinated ``drive`` IPC command that sets all motors
        in one atomic operation, ensuring synchronised wheel movement.

        Parameters
        ----------
        request : DriveRequest
            Signed speed (−100–100) and optional TTL lease.

        Returns
        -------
        DriveResponse
            Echoed speed, number of motors commanded, and UTC timestamp.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on hardware write failure.
        """
        motors = await _hat_call("drive", request.speed_pct, request.ttl_ms)
        return DriveResponse(
            speed_pct=request.speed_pct,
            motors=motors,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post("/api/steer", response_model=SteerResponse, tags=["Vehicle"])
    async def steer(request: SteerRequest):
        """Set the steering servo angle.

        Parameters
        ----------
        request : SteerRequest
            Angle in degrees (0–180, 90 = straight ahead) and optional TTL.

        Returns
        -------
        SteerResponse
            Echoed angle and UTC timestamp.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on hardware write failure.
        """
        await _hat_call("steer", request.angle_deg, request.ttl_ms)
        return SteerResponse(
            angle_deg=request.angle_deg,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post("/api/camera/pan", response_model=PanResponse, tags=["Vehicle"])
    async def pan_camera(request: PanRequest):
        """Set the camera pan (horizontal) servo angle.

        Parameters
        ----------
        request : PanRequest
            Angle in degrees (0–180, 90 = centre) and optional TTL.

        Returns
        -------
        PanResponse
            Echoed angle and UTC timestamp.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on hardware write failure.
        """
        await _hat_call("pan_camera", request.angle_deg, request.ttl_ms)
        return PanResponse(
            angle_deg=request.angle_deg,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post("/api/camera/tilt", response_model=TiltResponse, tags=["Vehicle"])
    async def tilt_camera(request: TiltRequest):
        """Set the camera tilt (vertical) servo angle.

        Parameters
        ----------
        request : TiltRequest
            Angle in degrees (0–180, 90 = centre) and optional TTL.

        Returns
        -------
        TiltResponse
            Echoed angle and UTC timestamp.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on hardware write failure.
        """
        await _hat_call("tilt_camera", request.angle_deg, request.ttl_ms)
        return TiltResponse(
            angle_deg=request.angle_deg,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # ========================================================================
    # Stream Endpoints
    # ========================================================================

    @device_router.post("/api/stream/start", response_model=StreamStartResponse, tags=["Stream"])
    async def start_stream(request: StreamStartRequest):
        """Start the MJPEG stream server in the background.

        Uses the host and port from the request (or defaults from the
        ``NOM_STREAM_HOST`` / ``NOM_STREAM_PORT`` environment variables set
        by ``start.sh`` from ``config.toml``) to start a Flask-based MJPEG
        stream server.  The existing API camera instance is shared with the
        stream server to avoid conflicting camera handles.  If a stream is
        already running the existing URL is returned without restarting.

        Returns
        -------
        StreamStartResponse
            ``url``: base URL of the stream viewer (``http://host:port``).

        Raises
        ------
        HTTPException
            503 if the camera is not available.
            500 if the stream server fails to start.
        """
        global _stream_server, _stream_host, _stream_port, _stream_token
        cam = _require_camera()

        host = request.host or _stream_host
        port = request.port or _stream_port

        if _stream_server is not None:
            url = f"http://{_stream_server.host}:{_stream_server.port}"
            if _stream_token is not None:
                url = f"{url}/?token={_stream_token}"
            return StreamStartResponse(
                url=url,
                host=_stream_server.host,
                port=_stream_server.port,
                token=_stream_token,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        # The MJPEG server runs outside the JWT-authenticated API, so gate it
        # with a per-run bearer token carried in the stream URL (checklist P10).
        token = secrets.token_urlsafe(16)
        try:
            server = StreamServer(host=host, port=port, camera=cam, access_token=token)
            server.start_background()
            _stream_server = server
            _stream_token = token
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to start stream: {e}") from e

        url = f"http://{host}:{port}/?token={token}"
        return StreamStartResponse(
            url=url,
            host=host,
            port=port,
            token=token,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post("/api/stream/stop", response_model=StreamStopResponse, tags=["Stream"])
    async def stop_stream():
        """Stop the MJPEG stream server.

        Stops the background stream server so that the camera is available
        for capture and recording operations again.

        Returns
        -------
        StreamStopResponse
            ``success``: true if a running stream was stopped.
        """
        global _stream_server, _stream_token
        if _stream_server is None:
            return StreamStopResponse(
                success=False,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        server = _stream_server
        _stream_server = None
        _stream_token = None
        try:
            await asyncio.to_thread(server.close)
        except Exception:
            pass
        return StreamStopResponse(
            success=True,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.get("/api/stream/status", response_model=StreamStatusResponse, tags=["Stream"])
    async def get_stream_status():
        """Return the current stream server state."""
        running = _stream_server is not None
        url = (
            f"http://{_stream_server.host}:{_stream_server.port}"
            if _stream_server is not None
            else None
        )
        return StreamStatusResponse(
            running=running,
            url=url,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # ========================================================================
    # Camera Endpoints
    # ========================================================================

    @device_router.get("/api/camera/status", response_model=CameraStatus, tags=["Camera"])
    async def get_camera_status():
        """Get current camera and recording status.

        Returns
        -------
        CameraStatus
            Camera readiness, recording state, resolution, and settings
        """
        cam = _require_camera()

        return CameraStatus(
            camera_ready=True,
            recording=cam._is_recording,
            resolution=f"{cam.width}x{cam.height}",
            fps=cam.fps,
            encoder=cam.encoder,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post("/api/camera/capture", response_model=CaptureResponse, tags=["Camera"])
    async def capture_image(request: CaptureRequest):
        """Capture a still image from the camera.

        Parameters
        ----------
        request : CaptureRequest
            Filename for the captured image (no path components)

        Returns
        -------
        CaptureResponse
            Success status and captured filename

        Raises
        ------
        HTTPException
            If filename is invalid or capture fails
        """
        cam = _require_camera()

        try:
            cam.capture_image(request.filename)
            return CaptureResponse(
                success=True,
                filename=request.filename,
                timestamp=datetime.now(timezone.utc).isoformat(),
                message=f"Image captured: {request.filename}",
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Capture failed: {str(e)}") from e

    @device_router.get("/api/camera/frame", tags=["Camera"])
    async def get_camera_frame():
        """Return a single raw JPEG frame from the camera.

        Unlike ``/api/camera/capture`` (which writes a file to disk and returns
        metadata), this returns the frame **bytes** directly as ``image/jpeg`` —
        a raw input for autonomon's vision perception layer (ADR-004). No
        interpretation is performed here.

        Returns
        -------
        Response
            ``image/jpeg`` with the raw JPEG frame as the body.

        Raises
        ------
        HTTPException
            503 if the camera is not initialized; 500 if frame capture fails.
        """
        cam = _require_camera()
        try:
            jpeg = await asyncio.to_thread(_grab_one_jpeg, cam)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Frame capture failed: {str(e)}") from e
        return Response(content=jpeg, media_type="image/jpeg")

    @device_router.post(
        "/api/camera/record/start", response_model=RecordStartResponse, tags=["Camera"]
    )
    async def start_recording(request: RecordRequest):
        """Start video recording.

        Parameters
        ----------
        request : RecordRequest
            Filename and optional encoder selection

        Returns
        -------
        RecordStartResponse
            Success status and filename

        Raises
        ------
        HTTPException
            If recording is already active, filename is invalid, or start fails
        """
        cam = _require_camera()

        if cam._is_recording:
            raise HTTPException(status_code=409, detail="Recording already in progress")

        try:
            # If encoder is specified, update camera settings
            if request.encoder and request.encoder.lower() in ["h264", "mjpeg"]:
                cam.encoder = request.encoder.lower()

            await asyncio.to_thread(cam.start_recording, request.filename)
            return RecordStartResponse(
                success=True,
                filename=request.filename,
                timestamp=datetime.now(timezone.utc).isoformat(),
                message=f"Recording started: {request.filename}",
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Recording start failed: {str(e)}") from e

    @device_router.post(
        "/api/camera/record/stop", response_model=RecordStopResponse, tags=["Camera"]
    )
    async def stop_recording():
        """Stop the current video recording.

        Returns
        -------
        RecordStopResponse
            Success status and timestamp

        Raises
        ------
        HTTPException
            If no recording is in progress or stop fails
        """
        cam = _require_camera()

        if not cam._is_recording:
            raise HTTPException(status_code=409, detail="No recording in progress")

        try:
            await asyncio.to_thread(cam.stop_recording)
            return RecordStopResponse(
                success=True,
                timestamp=datetime.now(timezone.utc).isoformat(),
                message="Recording stopped",
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Recording stop failed: {str(e)}") from e

    # ========================================================================
    # Audio Endpoints
    # ========================================================================

    @device_router.post(
        "/api/audio/record/start", response_model=AudioRecordStartResponse, tags=["Audio"]
    )
    async def start_audio_recording(request: AudioRecordStartRequest):
        """Start recording audio from the USB microphone.

        Records from the built-in USB microphone (PCM2902 codec) to a WAV
        file in the configured audio directory.  Recording continues until
        ``POST /api/audio/record/stop`` is called.

        Parameters
        ----------
        request : AudioRecordStartRequest
            Optional ``filename`` (basename only).  A timestamped name is
            generated when absent.

        Returns
        -------
        AudioRecordStartResponse
            ``filename``: basename of the output WAV file.

        Raises
        ------
        HTTPException
            400 if ``filename`` contains path components.
            409 if a recording is already in progress.
            500 on hardware error.
        """
        recorder = _require_audio_recorder()
        if recorder.is_recording:
            raise HTTPException(status_code=409, detail="A recording is already in progress")

        # Park the wake-word listener off the microphone for the recording
        # session (ADR-021); /api/audio/record/stop hands it back.
        wake_listener = getattr(app.state, "wake_listener", None)
        if wake_listener is not None:
            await asyncio.to_thread(wake_listener.pause)

        # Apply default mic capture gain before recording (best-effort).
        if _hat_client is not None:
            try:
                await asyncio.to_thread(_hat_client.set_mic_gain, _default_mic_gain_pct)
            except HatConnectionError:
                pass  # Daemon unavailable; record without adjusting gain.
            except HatError as e:
                # A wrong/missing ALSA capture control leaves capture at a stale
                # level — surface it (the daemon's message lists real controls).
                logger.warning("mic gain not applied (%d%%): %s", _default_mic_gain_pct, e)

        try:
            recording_path = await asyncio.to_thread(recorder.start, request.filename)
        except ValueError as e:
            if wake_listener is not None:
                wake_listener.resume()
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            if wake_listener is not None:
                wake_listener.resume()
            raise HTTPException(status_code=500, detail=str(e)) from e
        return AudioRecordStartResponse(
            recording=True,
            filename=Path(recording_path).name,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post(
        "/api/audio/record/stop", response_model=AudioRecordStopResponse, tags=["Audio"]
    )
    async def stop_audio_recording():
        """Stop the active audio recording session.

        Finalises and flushes the WAV file.

        Returns
        -------
        AudioRecordStopResponse
            ``filename``: basename of the completed recording, or null if no
            recording was active.
        """
        recorder = _require_audio_recorder()
        recording_path = await asyncio.to_thread(recorder.stop)
        # Hand the microphone back to the wake-word listener (ADR-021). Safe
        # even if it was never paused — resume is an idempotent event clear.
        wake_listener = getattr(app.state, "wake_listener", None)
        if wake_listener is not None:
            wake_listener.resume()
        return AudioRecordStopResponse(
            recording=False,
            filename=Path(recording_path).name if recording_path else None,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post("/api/audio/play", response_model=AudioPlayResponse, tags=["Audio"])
    async def play_audio(request: AudioPlayRequest):
        """Play a WAV audio file over the speaker.

        Enables the speaker amplifier via the nomopractic HAT daemon, then
        starts playback of the specified WAV file on the resolved output device.

        Parameters
        ----------
        request : AudioPlayRequest
            ``filename``: basename of the WAV file to play (no path components).

        Returns
        -------
        AudioPlayResponse
            ``filename``: basename of the file being played.

        Raises
        ------
        HTTPException
            400 if the filename contains path components.
            404 if the file does not exist.
            409 if playback is already in progress.
            500 on hardware error.
        """
        player = _require_audio_player()
        if player.is_playing:
            raise HTTPException(status_code=409, detail="Playback is already in progress")

        # Enable speaker amplifier before playback (best-effort).
        if _hat_client is not None:
            try:
                await asyncio.to_thread(_hat_client.enable_speaker)
            except (HatError, HatConnectionError):
                pass  # Continue without amplifier enable if daemon unavailable.

        # Apply default output volume before playback (best-effort).
        if _hat_client is not None:
            try:
                await asyncio.to_thread(_hat_client.set_volume, _default_volume_pct)
            except (HatError, HatConnectionError):
                pass  # Continue without volume set if daemon unavailable.

        try:
            await asyncio.to_thread(player.play, request.filename)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except RuntimeError as e:
            message = str(e)
            lower_message = message.lower()
            if "pyaudio not installed" in lower_message or "pyaudio not available" in lower_message:
                raise HTTPException(status_code=503, detail=message) from e
            raise HTTPException(status_code=409, detail=message) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

        current = player.current_file
        return AudioPlayResponse(
            playing=True,
            filename=Path(current).name if current else request.filename,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post(
        "/api/audio/play/stop", response_model=AudioPlayStopResponse, tags=["Audio"]
    )
    async def stop_audio_playback():
        """Stop ongoing audio playback.

        Stops the player and disables the speaker amplifier.

        Returns
        -------
        AudioPlayStopResponse
            ``success``: true.
        """
        player = _require_audio_player()
        await asyncio.to_thread(player.stop)

        # Disable speaker amplifier after playback (best-effort).
        if _hat_client is not None:
            try:
                await asyncio.to_thread(_hat_client.disable_speaker)
            except (HatError, HatConnectionError):
                pass

        return AudioPlayStopResponse(
            success=True,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.get("/api/audio/files", response_model=AudioFilesResponse, tags=["Audio"])
    async def list_audio():
        """List available WAV audio files in the configured audio directory.

        Returns
        -------
        AudioFilesResponse
            ``files``: sorted list of WAV file basenames.
        """
        files = await asyncio.to_thread(list_audio_files, _media_dir / "audio")
        return AudioFilesResponse(
            files=files,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.get("/api/audio/status", response_model=AudioStatusResponse, tags=["Audio"])
    async def get_audio_status():
        """Return current audio recorder and player state."""
        rec_file = _audio_recorder.current_file if _audio_recorder else None
        play_file = _audio_player.current_file if _audio_player else None
        return AudioStatusResponse(
            recording=_audio_recorder.is_recording if _audio_recorder else False,
            recording_file=Path(rec_file).name if rec_file else None,
            playing=_audio_player.is_playing if _audio_player else False,
            playback_file=Path(play_file).name if play_file else None,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post("/api/audio/volume", response_model=VolumeResponse, tags=["Audio"])
    async def set_volume(request: VolumeRequest):
        """Set the speaker output volume via the HAT daemon's ALSA mixer control.

        Parameters
        ----------
        request : VolumeRequest
            ``volume_pct``: target output volume 0–100 (%).

        Returns
        -------
        VolumeResponse
            Applied volume percentage and UTC timestamp.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on hardware error.
        """
        await _hat_call("set_volume", request.volume_pct)
        return VolumeResponse(
            volume_pct=request.volume_pct,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.get("/api/audio/volume", response_model=VolumeResponse, tags=["Audio"])
    async def get_volume():
        """Read the current output volume from the HAT daemon's ALSA mixer control.

        Returns
        -------
        VolumeResponse
            Current volume percentage and UTC timestamp.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on hardware error.
        """
        pct = await _hat_call("get_volume")
        return VolumeResponse(
            volume_pct=pct,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post("/api/audio/mic-gain", response_model=MicGainResponse, tags=["Audio"])
    async def set_mic_gain(request: MicGainRequest):
        """Set the microphone capture gain on the USB mic via ALSA.

        Parameters
        ----------
        request : MicGainRequest
            ``gain_pct``: target mic capture gain 0–100 (%).

        Returns
        -------
        MicGainResponse
            Applied gain percentage and UTC timestamp.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on hardware error.
        """
        await _hat_call("set_mic_gain", request.gain_pct)
        return MicGainResponse(
            gain_pct=request.gain_pct,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.get("/api/audio/mic-gain", response_model=MicGainResponse, tags=["Audio"])
    async def get_mic_gain():
        """Read the current microphone capture gain from the ALSA USB mic mixer.

        Returns
        -------
        MicGainResponse
            Current gain percentage and UTC timestamp.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on hardware error.
        """
        pct = await _hat_call("get_mic_gain")
        return MicGainResponse(
            gain_pct=pct,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # ========================================================================
    # Calibration Endpoints
    # ========================================================================

    @device_router.get(
        "/api/calibration", response_model=CalibrationSnapshotResponse, tags=["Calibration"]
    )
    async def get_calibration():
        """Return a full snapshot of the current runtime calibration."""
        snap = await _hat_call("get_calibration")
        return CalibrationSnapshotResponse(
            motors=[
                MotorCalibrationItem(
                    channel=m.channel,
                    speed_scale=m.speed_scale,
                    deadband_pct=m.deadband_pct,
                    reversed=m.reversed,
                )
                for m in snap.motors
            ],
            servos={
                name: ServoCalibrationItem(trim_us=v.trim_us) for name, v in snap.servos.items()
            },
            grayscale=[
                GrayscaleCalibrationItem(
                    channel=i,
                    adc_channel=g.adc_channel,
                    white_raw=g.white_raw,
                    black_raw=g.black_raw,
                )
                for i, g in enumerate(snap.grayscale)
            ],
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.put(
        "/api/calibration/motor/{channel}",
        response_model=MotorCalibrationResponse,
        tags=["Calibration"],
    )
    async def put_motor_calibration(channel: int, request: MotorCalibrationRequest):
        """Partially update motor calibration for one channel."""
        hat = _require_hat()
        try:
            entry = await asyncio.to_thread(
                hat.set_motor_calibration,
                channel,
                request.speed_scale,
                request.deadband_pct,
                request.reversed,
            )
        except HatConnectionError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        except HatError as e:
            status = 422 if e.code == "INVALID_PARAMS" else 500
            raise HTTPException(status_code=status, detail=str(e)) from e
        return MotorCalibrationResponse(
            channel=entry.channel,
            speed_scale=entry.speed_scale,
            deadband_pct=entry.deadband_pct,
            reversed=entry.reversed,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.put(
        "/api/calibration/servo/{servo_name}",
        response_model=ServoCalibrationResponse,
        tags=["Calibration"],
    )
    async def put_servo_calibration(servo_name: str, request: ServoCalibrationRequest):
        """Set the trim offset (µs) for a named servo."""
        hat = _require_hat()
        try:
            entry = await asyncio.to_thread(hat.set_servo_calibration, servo_name, request.trim_us)
        except HatConnectionError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        except HatError as e:
            status = 422 if e.code == "INVALID_PARAMS" else 500
            raise HTTPException(status_code=status, detail=str(e)) from e
        return ServoCalibrationResponse(
            servo=entry.servo,
            trim_us=entry.trim_us,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post(
        "/api/calibration/grayscale/{channel}/capture",
        response_model=GrayscaleCaptureResponse,
        tags=["Calibration"],
    )
    async def post_calibrate_grayscale(channel: int, request: GrayscaleCaptureRequest):
        """Capture a live ADC reading as the white or black surface reference."""
        hat = _require_hat()
        try:
            result = await asyncio.to_thread(hat.calibrate_grayscale, channel, request.surface)
        except HatConnectionError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        except HatError as e:
            status = 422 if e.code == "INVALID_PARAMS" else 500
            raise HTTPException(status_code=status, detail=str(e)) from e
        return GrayscaleCaptureResponse(
            channel=result.channel,
            adc_channel=result.adc_channel,
            surface=result.surface,
            raw_value=result.raw_value,
            stored=result.stored,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post(
        "/api/calibration/save", response_model=SaveCalibrationResponse, tags=["Calibration"]
    )
    async def post_save_calibration():
        """Persist the current in-memory calibration store to disk."""
        result = await _hat_call("save_calibration")
        return SaveCalibrationResponse(
            saved=result.saved,
            path=result.path,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post(
        "/api/calibration/reset", response_model=ResetCalibrationResponse, tags=["Calibration"]
    )
    async def post_reset_calibration():
        """Revert the in-memory calibration store to factory defaults."""
        reset = await _hat_call("reset_calibration")
        return ResetCalibrationResponse(
            reset=reset,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.get(
        "/api/sensor/grayscale/normalized",
        response_model=NormalizedGrayscaleResponse,
        tags=["Sensor"],
    )
    async def get_grayscale_normalized():
        """Return per-channel normalised grayscale sensor readings (0.0–1.0)."""
        result = await _hat_call("read_grayscale_normalized")
        return NormalizedGrayscaleResponse(
            channels=result.channels,
            normalized=result.normalized,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # ========================================================================
    # HAT Endpoints
    # ========================================================================

    @device_router.get("/api/hat/battery", response_model=BatteryResponse, tags=["HAT"])
    async def get_battery():
        """Read the Robot HAT V4 battery voltage.

        Returns
        -------
        BatteryResponse
            Battery voltage in volts and UTC timestamp.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on hardware read failure.
        """
        voltage = await _hat_call("get_battery_voltage")
        return BatteryResponse(
            voltage_v=voltage,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post("/api/hat/servo", response_model=ServoResponse, tags=["HAT"])
    async def set_servo(request: ServoRequest):
        """Set a servo channel to the requested angle.

        Parameters
        ----------
        request : ServoRequest
            Target channel, angle in degrees, and optional TTL.

        Returns
        -------
        ServoResponse
            Echoed channel, angle, and UTC timestamp.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on hardware write failure.
        """
        await _hat_call(
            "set_servo_angle",
            request.channel,
            request.angle_deg,
            request.ttl_ms,
        )
        return ServoResponse(
            channel=request.channel,
            angle_deg=request.angle_deg,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post("/api/hat/reset", response_model=ResetResponse, tags=["HAT"])
    async def reset_mcu():
        """Assert and release the Robot HAT V4 MCU reset line.

        Returns
        -------
        ResetResponse
            Success status and UTC timestamp.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on GPIO failure.
        """
        await _hat_call("reset_mcu")
        return ResetResponse(
            success=True,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.get("/api/hat/servo/status", response_model=ServoStatusResponse, tags=["HAT"])
    async def get_servo_status():
        """Return the daemon's active servo TTL lease table.

        Returns
        -------
        ServoStatusResponse
            List of active leases with channel, TTL remaining, and connection ID.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on error.
        """
        status = await _hat_call("get_servo_status")
        return ServoStatusResponse(
            active_leases=[
                ServoLeaseItem(
                    channel=e.channel,
                    ttl_remaining_ms=e.ttl_remaining_ms,
                    conn_id=e.conn_id,
                )
                for e in status.active_leases
            ],
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.get("/api/hat/mcu/status", response_model=McuStatusResponse, tags=["HAT"])
    async def get_mcu_status():
        """Return MCU reset statistics tracked by the daemon.

        Returns
        -------
        McuStatusResponse
            Reset count since daemon start and seconds since last reset (null if none).

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on error.
        """
        status = await _hat_call("get_mcu_status")
        return McuStatusResponse(
            resets_since_start=status.resets_since_start,
            last_reset_s_ago=status.last_reset_s_ago,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post("/api/hat/motor", response_model=MotorResponse, tags=["HAT"])
    async def set_motor(request: MotorRequest):
        """Set a DC motor channel to the requested speed.

        Parameters
        ----------
        request : MotorRequest
            Target channel (0–3), signed speed percentage, and optional TTL.

        Returns
        -------
        MotorResponse
            Echoed channel, speed_pct, and UTC timestamp.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on hardware write failure.
        """
        await _hat_call(
            "set_motor_speed",
            request.channel,
            request.speed_pct,
            request.ttl_ms,
        )
        return MotorResponse(
            channel=request.channel,
            speed_pct=request.speed_pct,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post("/api/hat/motor/stop", response_model=StopMotorsResponse, tags=["HAT"])
    async def stop_motors():
        """Immediately stop all DC motors and clear their leases.

        Returns
        -------
        StopMotorsResponse
            Number of motors stopped and UTC timestamp.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on hardware failure.
        """
        stopped = await _hat_call("stop_all_motors")
        return StopMotorsResponse(
            stopped=stopped,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.get("/api/hat/motor/status", response_model=MotorStatusResponse, tags=["HAT"])
    async def get_motor_status():
        """Return the daemon's active motor TTL lease table.

        Returns
        -------
        MotorStatusResponse
            List of active leases with channel, TTL remaining, and connection ID.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on error.
        """
        status = await _hat_call("get_motor_status")
        return MotorStatusResponse(
            active_leases=[
                MotorLeaseItem(
                    channel=e.channel,
                    ttl_remaining_ms=e.ttl_remaining_ms,
                    conn_id=e.conn_id,
                )
                for e in status.active_leases
            ],
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post("/api/hat/speaker", response_model=SpeakerResponse, tags=["HAT"])
    async def set_speaker(request: SpeakerRequest):
        """Enable or disable the speaker amplifier on the Robot HAT V4.

        The request is forwarded to the nomopractic daemon, which controls
        the underlying hardware signal used to power the amplifier on or off.

        Parameters
        ----------
        request : SpeakerRequest
            ``enabled``: true to power the amplifier on, false to power off.

        Returns
        -------
        SpeakerResponse
            Current ``enabled`` state.

        Raises
        ------
        HTTPException
            503 if the nomopractic daemon is unavailable.
            500 on hardware error.
        """
        method = "enable_speaker" if request.enabled else "disable_speaker"
        await _hat_call(method)
        return SpeakerResponse(
            enabled=request.enabled,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # ========================================================================
    # Routine Endpoints
    # ========================================================================

    @device_router.post("/api/routine/start", response_model=RoutineStartResponse, tags=["Routine"])
    async def start_routine(request: RoutineStartRequest):
        """Start an autonomous routine.

        Returns 409 if a routine is already running.
        Returns 422 if the routine name is unknown or a parameter is out of range.
        """
        hat = _require_hat()
        try:
            result = await asyncio.to_thread(
                hat.start_routine,
                request.name,
                request.speed_pct,
                request.obstacle_threshold_cm,
                request.cliff_threshold_normalized,
                request.max_duration_s,
            )
        except HatConnectionError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        except HatError as e:
            if e.code == "ALREADY_RUNNING":
                raise HTTPException(status_code=409, detail=str(e)) from e
            if e.code == "INVALID_PARAMS":
                raise HTTPException(status_code=422, detail=str(e)) from e
            raise HTTPException(status_code=500, detail=str(e)) from e
        return RoutineStartResponse(
            name=result.name,
            started_at_uptime_s=result.started_at_uptime_s,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post("/api/routine/stop", response_model=RoutineStopResponse, tags=["Routine"])
    async def stop_routine():
        """Stop the currently running routine.

        Returns 409 if no routine is running.
        """
        hat = _require_hat()
        try:
            result = await asyncio.to_thread(hat.stop_routine)
        except HatConnectionError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        except HatError as e:
            if e.code == "INVALID_PARAMS":
                raise HTTPException(status_code=409, detail=str(e)) from e
            raise HTTPException(status_code=500, detail=str(e)) from e
        return RoutineStopResponse(
            name=result.name,
            ran_for_s=result.ran_for_s,
            obstacles_avoided=result.obstacles_avoided,
            cliffs_avoided=result.cliffs_avoided,
            stop_reason=result.stop_reason,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.get(
        "/api/routine/status", response_model=RoutineStatusResponse, tags=["Routine"]
    )
    async def get_routine_status():
        """Return the current routine status snapshot."""
        result = await _hat_call("get_routine_status")
        return RoutineStatusResponse(
            running=result.running,
            name=result.name,
            elapsed_s=result.elapsed_s,
            obstacles_avoided=result.obstacles_avoided,
            cliffs_avoided=result.cliffs_avoided,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @device_router.post(
        "/api/device/network/configure",
        response_model=WifiProvisionResponse,
        status_code=200,
        tags=["Network"],
        dependencies=[Depends(network_rate_limit)],
    )
    async def configure_wifi(request_body: WifiProvisionRequest) -> WifiProvisionResponse:
        """Provision home Wi-Fi credentials and connect in the background.

        Stores a persistent NetworkManager connection profile for the supplied
        SSID and initiates an association attempt on ``wlan0``. Returns
        immediately — the association runs as a background task. The Soft AP
        watchdog will call ``ap-mode.sh down`` automatically once the device
        achieves full internet connectivity.

        Parameters
        ----------
        request_body : WifiProvisionRequest
            Target SSID (1–32 chars) and WPA2 password (8–63 chars, or empty
            for open networks).

        Returns
        -------
        WifiProvisionResponse
            ``{"status": "connecting"}`` — the device has accepted the
            credentials and is attempting to associate in the background.

        Raises
        ------
        HTTPException
            422 if SSID or password fail Pydantic validation.
            500 if the ``nmcli`` subprocess cannot be launched (OS error).
            429 if the rate limit is exceeded.

        Notes
        -----
        The WPA2 password is passed to ``nmcli`` via stdin (``--ask`` mode),
        never as a CLI argument, to prevent credential exposure in process
        listings and system logs.
        """
        ssid = request_body.ssid
        password = request_body.password

        # --ask makes nmcli read credentials from stdin so the password is
        # never visible in the process argument list.
        cmd = ["nmcli", "--ask", "--terse", "device", "wifi", "connect", ssid, "ifname", "wlan0"]
        stdin_data = f"{password}\n".encode() if password else b"\n"

        async def _connect() -> None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(input=stdin_data),
                        timeout=30,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    logger.warning("nmcli wifi connect timed out for SSID %r", ssid)
                    return
                if proc.returncode != 0:
                    logger.warning(
                        "nmcli wifi connect failed (rc=%d): %s",
                        proc.returncode,
                        stderr_bytes.decode(errors="replace").strip(),
                    )
                else:
                    logger.info("nmcli wifi connect succeeded for SSID %r", ssid)
            except OSError:
                logger.warning("nmcli wifi connect OSError for SSID %r", ssid, exc_info=True)

        _t = asyncio.create_task(_connect())
        _background_tasks.add(_t)
        _t.add_done_callback(_background_tasks.discard)
        return WifiProvisionResponse(status="connecting")

    @device_router.post(
        "/api/device/wifi/ap",
        response_model=WifiApResponse,
        status_code=200,
        tags=["Network"],
        dependencies=[Depends(network_rate_limit)],
    )
    async def set_wifi_ap(request_body: WifiApRequest) -> WifiApResponse:
        """Enable or disable the nomon Wi-Fi Soft AP.

        Invokes ``ap-mode.sh up`` or ``ap-mode.sh down`` synchronously in a
        thread-pool executor. The script is idempotent.

        Parameters
        ----------
        request_body : WifiApRequest
            ``enabled``: true to bring the AP up, false to bring it down.

        Returns
        -------
        WifiApResponse
            ``status``: ``"up"`` or ``"down"`` confirming the new state.

        Raises
        ------
        HTTPException
            503 if the ap-mode script is not found (OS not configured).
            500 if the script exits non-zero (nmcli error).
            429 if the rate limit is exceeded.
        """
        subcommand: Literal["up", "down"] = "up" if request_body.enabled else "down"
        script_path = os.environ.get(
            "NOMON_AP_MODE_SCRIPT",
            "/usr/local/bin/ap-mode.sh",
        )

        def _run_script() -> Literal["up", "down"]:
            try:
                result = subprocess.run(
                    [script_path, subcommand],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except FileNotFoundError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="ap-mode.sh not found (check NOMON_AP_MODE_SCRIPT)",
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise HTTPException(
                    status_code=500,
                    detail="ap-mode.sh timed out",
                ) from exc
            if result.returncode != 0:
                logger.warning(
                    "ap-mode.sh %s failed (rc=%d): %s",
                    subcommand,
                    result.returncode,
                    result.stderr.strip(),
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"AP mode toggle failed: {result.stderr.strip() or result.stdout.strip()}",
                )
            logger.info("ap-mode.sh %s succeeded: %s", subcommand, result.stdout.strip())
            return subcommand

        ap_status = await asyncio.to_thread(_run_script)
        return WifiApResponse(
            status=ap_status,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # Include all device endpoints (with or without auth dependency)
    app.include_router(device_router)

    # Warn if Tailscale is not detected and auth is disabled
    if not device_auth_enabled:
        import shutil

        if not shutil.which("tailscale"):
            logger.warning(
                "Tailscale not detected. Device-mode endpoints are unauthenticated — "
                "network-level access control is required. See ADR-010."
            )


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Route registration is determined by the ``NOMON_API_MODE`` environment
    variable (see :func:`nomothetic.mode.get_mode`):

    - **device** — hardware endpoints (camera, HAT, vehicle, sensor, stream,
      audio, calibration, routine).
    - **central** — auth and fleet management endpoints.

    The health endpoint is available in both modes.

    Returns
    -------
    FastAPI
        Configured FastAPI application with CORS and mode-specific endpoints.
    """
    # Under `uvicorn --factory` (the systemd unit) nothing configures the
    # root logger, so the app's own INFO records — wake-word events (ADR-021),
    # transcriptions, AI command outcomes — would be dropped and never reach
    # the journal. Mirror the Server wrapper: attach a basic stderr handler
    # only when nothing else has claimed the root logger.
    if not logging.root.handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    mode = get_mode()

    app = FastAPI(
        title="nomon Camera API" if mode == Mode.DEVICE else "nomon Central API",
        description=(
            "HTTP REST API for Raspberry Pi camera control"
            if mode == Mode.DEVICE
            else "HTTP REST API for fleet management and authentication"
        ),
        version=_meta.version("nomothetic"),
        lifespan=lifespan,
    )

    _setup_cors(app, mode)

    @app.get("/", tags=["Health"])
    async def health():
        """Health check endpoint."""
        return {
            "status": "ok",
            "service": "nomon-camera-api" if mode == Mode.DEVICE else "nomon-central-api",
            "version": _meta.version("nomothetic"),
            "mode": mode.value,
        }

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc):
        """Format HTTP exceptions as JSON."""
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error=exc.detail, timestamp=datetime.now(timezone.utc).isoformat()
            ).model_dump(),
        )

    if mode == Mode.CENTRAL:
        _setup_central_stores(app)
    else:
        _register_device_routes(app, mode)

    return app


# ============================================================================
# Server Wrapper
# ============================================================================


class APIServer:
    """Manages the FastAPI application and uvicorn server lifecycle.

    Parameters
    ----------
    host : str, optional
        Bind address (default: "127.0.0.1" for local only)
    port : int, optional
        Listen port (default: 8443)
    use_ssl : bool, optional
        Enable HTTPS with self-signed certificate (default: True)
    cert_dir : Path, optional
        Directory for certificates (default: ".certs")
    reload : bool, optional
        Auto-reload on code changes (default: False)

    Raises
    ------
    ValueError
        If port is out of valid range (1-65535)
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8443,
        use_ssl: bool = True,
        cert_dir: Optional[Path] = None,
        reload: bool = False,
    ):
        if not 1 <= port <= 65535:
            raise ValueError(f"Invalid port {port}: must be between 1 and 65535")

        self.host = host
        self.port = port
        self.reload = reload
        self.use_ssl = use_ssl
        self.cert_dir = Path(cert_dir or ".certs")
        self.app = create_app()

        # Ensure cert provisioning logs are visible before uvicorn takes over
        # logging configuration.  No-op if a handler is already attached.
        if not logging.root.handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(levelname)s: %(message)s",
            )

        if self.use_ssl:
            self.cert_file = self.cert_dir / "cert.pem"
            self.key_file = self.cert_dir / "key.pem"
            provision_tls_cert(self.cert_file, self.key_file)

    def get_config(self) -> dict:
        """Get uvicorn configuration dictionary.

        Returns
        -------
        dict
            Configuration for uvicorn.run()
        """
        config = {
            "app": self.app,
            "host": self.host,
            "port": self.port,
            "reload": self.reload,
            "log_level": "info",
        }

        if self.use_ssl:
            config["ssl_certfile"] = str(self.cert_file)
            config["ssl_keyfile"] = str(self.key_file)

        return config

    def run(self) -> None:
        """Start the API server (blocking).

        Raises
        ------
        ImportError
            If uvicorn is not installed
        """
        try:
            import uvicorn
        except ImportError as e:
            raise ImportError(
                "uvicorn package required to run API server. "
                "Install with: pip install nomothetic[api]"
            ) from e

        config = self.get_config()
        protocol = "https" if self.use_ssl else "http"
        logger.info("Starting API server at %s://%s:%s", protocol, self.host, self.port)
        uvicorn.run(**config)

    def start_background(self):
        """Start the API server in a background thread.

        Returns
        -------
        threading.Thread
            The server thread (daemon thread)

        Raises
        ------
        ImportError
            If uvicorn is not installed
        """
        import threading

        try:
            import uvicorn
        except ImportError as e:
            raise ImportError(
                "uvicorn package required to run API server. "
                "Install with: pip install nomothetic[api]"
            ) from e

        config = self.get_config()

        def run_server():
            uvicorn.run(**config)

        thread = threading.Thread(target=run_server, daemon=True)
        thread.start()
        return thread
