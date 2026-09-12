"""IPC client for the nomopractic Rust HAT daemon.

Communicates over a Unix domain socket using NDJSON framing.
Full IPC schema: docs/hat_ipc_schema.md

Classes
-------
HatClient
    Thin client wrapping the Unix socket connection.
HatError
    Base exception for HAT IPC errors.
HatConnectionError
    Raised on socket connection failures.
HatTimeoutError
    Raised on per-request read timeout.
HatHealthResult
    Dataclass for the health IPC method result.
GrayscaleResult
    Dataclass for the read_grayscale IPC method result.
"""

from __future__ import annotations

import io
import json
import os
import socket
import threading
from dataclasses import dataclass
from pathlib import Path  # noqa: F401 – re-exported for callers
from typing import cast

_DEFAULT_SOCKET_PATH = "/run/nomopractic/nomopractic.sock"


class HatError(Exception):
    """Base exception for all nomothetic.hat errors.

    Attributes
    ----------
    code : str
        Machine-readable error code (e.g. ``"HARDWARE_ERROR"``).
    message : str
        Human-readable description.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class HatConnectionError(HatError):
    """Raised when the socket connection cannot be established or is lost.

    ``code`` is always ``"CONNECTION_ERROR"``.
    """

    def __init__(self, message: str) -> None:
        super().__init__("CONNECTION_ERROR", message)


class HatTimeoutError(HatError):
    """Raised when a request does not receive a response within *timeout_s*.

    ``code`` is always ``"TIMEOUT"``.
    """

    def __init__(self, message: str) -> None:
        super().__init__("TIMEOUT", message)


@dataclass
class HatHealthResult:
    """Result payload from the ``health`` IPC method."""

    status: str
    version: str
    hat_address: str
    i2c_bus: int
    uptime_s: int


@dataclass
class ServoLeaseEntry:
    """A single active servo TTL lease."""

    channel: int
    ttl_remaining_ms: int
    conn_id: int


@dataclass
class ServoStatusResult:
    """Result payload from the ``get_servo_status`` IPC method."""

    active_leases: list[ServoLeaseEntry]


@dataclass
class McuStatusResult:
    """Result payload from the ``get_mcu_status`` IPC method."""

    resets_since_start: int
    last_reset_s_ago: int | None


@dataclass
class MotorLeaseEntry:
    """A single active motor TTL lease."""

    channel: int
    ttl_remaining_ms: int
    conn_id: int


@dataclass
class MotorStatusResult:
    """Result payload from the ``get_motor_status`` IPC method."""

    active_leases: list[MotorLeaseEntry]


@dataclass
class GrayscaleResult:
    """Result payload from the ``read_grayscale`` IPC method.

    Attributes
    ----------
    channels : list[int]
        ADC channel numbers used [left, center, right].
    values : list[int]
        Raw 12-bit ADC readings (0–4095) for each channel.
    """

    channels: list[int]
    values: list[int]


@dataclass
class UltrasonicResult:
    """Result payload from the ``read_ultrasonic`` IPC method.

    Attributes
    ----------
    distance_cm : float
        Measured distance in centimetres (2–400 cm).
    """

    distance_cm: float


class HatClient:
    """Client for the nomopractic Unix domain socket IPC daemon.

    Parameters
    ----------
    socket_path : str | Path | None
        Path to the Unix domain socket. Defaults to
        ``/run/nomopractic/nomopractic.sock`` or the value of the
        ``NOMON_HAT_SOCKET_PATH`` environment variable.
    timeout_s : float
        Per-request read timeout in seconds. Default: 2.0.
    """

    def __init__(
        self,
        socket_path: str | Path | None = None,
        timeout_s: float = 2.0,
    ) -> None:
        if socket_path is None:
            socket_path = os.environ.get("NOMON_HAT_SOCKET_PATH", _DEFAULT_SOCKET_PATH)
        self._socket_path = str(socket_path)
        self._timeout_s = timeout_s
        self._sock: socket.socket | None = None
        self._rfile: io.BufferedReader | None = None
        self._lock = threading.Lock()
        self._req_counter = 0

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the socket connection to nomopractic.

        Raises
        ------
        HatConnectionError
            If the socket file does not exist or the connection is refused.
        """
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self._timeout_s)
            sock.connect(self._socket_path)
            self._sock = sock
            self._rfile = sock.makefile("rb")
        except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
            sock.close()
            raise HatConnectionError(
                f"Cannot connect to nomopractic at {self._socket_path}: {e}"
            ) from e

    def close(self) -> None:
        """Close the socket connection. Safe to call when already closed."""
        if self._rfile is not None:
            try:
                self._rfile.close()
            except OSError:
                pass
            self._rfile = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> HatClient:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal request machinery
    # ------------------------------------------------------------------

    def _next_id(self) -> str:
        self._req_counter += 1
        return str(self._req_counter)

    def _ensure_connected(self) -> None:
        if self._sock is None:
            self.connect()

    def _send_request(self, method: str, params: dict, req_id: str) -> dict:
        """Send one NDJSON request and return the result dict.

        Caller must hold ``_lock`` and ensure the socket is connected.

        Raises
        ------
        HatTimeoutError
            On read timeout.
        HatConnectionError
            On any OS-level socket error (broken pipe, connection reset,
            ENOTCONN, EBADF, etc.) or empty response.
        HatError
            When the daemon returns ``ok=false``.
        """
        req = {"id": req_id, "method": method, "params": params}
        line = json.dumps(req) + "\n"
        try:
            if self._sock is None or self._rfile is None:
                raise HatConnectionError("Socket is not connected")
            self._sock.sendall(line.encode())
            resp_line = self._rfile.readline()
        except socket.timeout as e:
            raise HatTimeoutError(f"Request timed out after {self._timeout_s}s") from e
        except OSError as e:
            # Catches BrokenPipeError, ConnectionResetError, ENOTCONN, EBADF, etc.
            raise HatConnectionError(f"Connection lost: {e}") from e

        if not resp_line:
            raise HatConnectionError("Connection closed by daemon")

        try:
            resp = json.loads(resp_line)
        except json.JSONDecodeError as e:
            raise HatConnectionError(f"Malformed response from daemon: {e}") from e

        if not resp.get("ok"):
            error = resp.get("error", {})
            raise HatError(
                error.get("code", "UNKNOWN_ERROR"),
                error.get("message", "Unknown error"),
            )

        return cast(dict, resp.get("result", {}))

    def _request(self, method: str, params: dict) -> dict:
        """Thread-safe request with one reconnect retry on connection loss."""
        with self._lock:
            self._ensure_connected()
            req_id = self._next_id()
            try:
                return self._send_request(method, params, req_id)
            except HatConnectionError:
                # Reconnect once and retry with a fresh request ID.
                self.close()
                self.connect()
                req_id = self._next_id()
                return self._send_request(method, params, req_id)

    # ------------------------------------------------------------------
    # Public IPC methods
    # ------------------------------------------------------------------

    def health(self) -> HatHealthResult:
        """Return daemon liveness and hardware status.

        Raises
        ------
        HatError
            If the daemon returns ok=false.
        HatConnectionError
            If the socket connection cannot be established or is lost.
        """
        result = self._request("health", {})
        return HatHealthResult(
            status=result["status"],
            version=result["version"],
            hat_address=result["hat_address"],
            i2c_bus=result["i2c_bus"],
            uptime_s=result["uptime_s"],
        )

    def get_battery_voltage(self) -> float:
        """Read battery voltage in volts via Robot HAT V4 ADC channel A4.

        Returns
        -------
        float
            Battery voltage in volts.

        Raises
        ------
        HatError
            On hardware read failure.
        HatConnectionError
            If the socket connection is lost.
        """
        result = self._request("get_battery_voltage", {})
        return float(result["voltage_v"])

    def set_servo_pulse_us(
        self,
        channel: int,
        pulse_us: int,
        ttl_ms: int = 500,
    ) -> None:
        """Set a PWM channel to a specific pulse width in microseconds.

        Parameters
        ----------
        channel : int
            PWM channel number (0–11).
        pulse_us : int
            Pulse width in microseconds (500–2500).
        ttl_ms : int
            Lease TTL in milliseconds. Default: 500.

        Raises
        ------
        ValueError
            If channel or pulse_us is out of range.
        HatError
            On hardware write failure.
        """
        if not 0 <= channel <= 11:
            raise ValueError(f"channel must be 0–11, got {channel}")
        if not 500 <= pulse_us <= 2500:
            raise ValueError(f"pulse_us must be 500–2500, got {pulse_us}")
        self._request(
            "set_servo_pulse_us",
            {"channel": channel, "pulse_us": pulse_us, "ttl_ms": ttl_ms},
        )

    def set_servo_angle(
        self,
        channel: int,
        angle_deg: float,
        ttl_ms: int = 500,
    ) -> None:
        """Set a servo to an angle in degrees (0.0–180.0).

        Parameters
        ----------
        channel : int
            PWM channel number (0–11).
        angle_deg : float
            Target angle in degrees.
        ttl_ms : int
            Lease TTL in milliseconds. Default: 500.

        Raises
        ------
        ValueError
            If channel or angle_deg is out of range.
        HatError
            On hardware write failure.
        """
        if not 0 <= channel <= 11:
            raise ValueError(f"channel must be 0–11, got {channel}")
        if not 0.0 <= angle_deg <= 180.0:
            raise ValueError(f"angle_deg must be 0.0–180.0, got {angle_deg}")
        self._request(
            "set_servo_angle",
            {"channel": channel, "angle_deg": angle_deg, "ttl_ms": ttl_ms},
        )

    def reset_mcu(self) -> None:
        """Assert and release the Robot HAT V4 MCU reset line.

        Raises
        ------
        HatError
            On GPIO failure.
        HatConnectionError
            If the socket connection is lost.
        """
        self._request("reset_mcu", {})

    def get_servo_status(self) -> ServoStatusResult:
        """Return the daemon's active servo TTL lease table.

        Each entry reflects a channel that has an unexpired lease, with the
        time remaining on that lease and the connection that owns it.

        Returns
        -------
        ServoStatusResult
            Active lease list (may be empty).

        Raises
        ------
        HatError
            If the daemon returns ok=false.
        HatConnectionError
            If the socket connection is lost.
        """
        result = self._request("get_servo_status", {})
        leases = [
            ServoLeaseEntry(
                channel=entry["channel"],
                ttl_remaining_ms=entry["ttl_remaining_ms"],
                conn_id=entry["conn_id"],
            )
            for entry in result.get("active_leases", [])
        ]
        return ServoStatusResult(active_leases=leases)

    def get_mcu_status(self) -> McuStatusResult:
        """Return MCU reset statistics tracked by the daemon.

        Returns
        -------
        McuStatusResult
            ``resets_since_start``: count of successful resets since daemon start.
            ``last_reset_s_ago``: seconds since the last reset, or ``None`` if
            no reset has occurred in this daemon session.

        Raises
        ------
        HatError
            If the daemon returns ok=false.
        HatConnectionError
            If the socket connection is lost.
        """
        result = self._request("get_mcu_status", {})
        return McuStatusResult(
            resets_since_start=result["resets_since_start"],
            last_reset_s_ago=result.get("last_reset_s_ago"),
        )

    def set_motor_speed(
        self,
        channel: int,
        speed_pct: float,
        ttl_ms: int = 500,
    ) -> None:
        """Set a DC motor's speed as a signed percentage.

        Parameters
        ----------
        channel : int
            IPC motor index (0-based position in daemon ``config.motors``).
            Supported range: 0–3.
        speed_pct : float
            Signed speed: −100.0 (full reverse) to +100.0 (full forward).
            0.0 stops the motor.
        ttl_ms : int
            Lease TTL in milliseconds. Motor is stopped if not refreshed.
            Default: 500.

        Raises
        ------
        ValueError
            If ``channel`` or ``speed_pct`` is out of range.
        HatError
            On hardware write failure.
        HatConnectionError
            If the socket connection is lost.
        """
        if not 0 <= channel <= 3:
            raise ValueError(f"channel must be 0–3, got {channel}")
        if not -100.0 <= speed_pct <= 100.0:
            raise ValueError(f"speed_pct must be -100.0–100.0, got {speed_pct}")
        self._request(
            "set_motor_speed",
            {"channel": channel, "speed_pct": speed_pct, "ttl_ms": ttl_ms},
        )

    def stop_all_motors(self) -> int:
        """Immediately stop all configured DC motors and clear their leases.

        Returns
        -------
        int
            Number of motors commanded to stop.

        Raises
        ------
        HatError
            If the daemon returns ok=false.
        HatConnectionError
            If the socket connection is lost.
        """
        result = self._request("stop_all_motors", {})
        return int(result["stopped"])

    def get_motor_status(self) -> MotorStatusResult:
        """Return the daemon's active motor TTL lease table.

        Returns
        -------
        MotorStatusResult
            Active lease list (may be empty).

        Raises
        ------
        HatError
            If the daemon returns ok=false.
        HatConnectionError
            If the socket connection is lost.
        """
        result = self._request("get_motor_status", {})
        leases = [
            MotorLeaseEntry(
                channel=entry["channel"],
                ttl_remaining_ms=entry["ttl_remaining_ms"],
                conn_id=entry["conn_id"],
            )
            for entry in result.get("active_leases", [])
        ]
        return MotorStatusResult(active_leases=leases)

    # ------------------------------------------------------------------
    # Convenience / coordinated methods
    # ------------------------------------------------------------------

    def drive(self, speed_pct: float, ttl_ms: int = 500) -> int:
        """Set all configured DC motors to the same speed simultaneously.

        Sends a single ``drive`` IPC request that commands all motors in the
        daemon in one atomic Rust call, ensuring the motors start in sync
        without per-motor round-trip latency.

        Parameters
        ----------
        speed_pct : float
            Signed speed: −100.0 (full reverse) to +100.0 (full forward).
            0.0 stops all motors.
        ttl_ms : int
            Lease TTL in milliseconds. Motors stop if not refreshed.
            Default: 500.

        Returns
        -------
        int
            Number of motors commanded.

        Raises
        ------
        ValueError
            If ``speed_pct`` is out of range.
        HatError
            On hardware write failure.
        HatConnectionError
            If the socket connection is lost.
        """
        if not -100.0 <= speed_pct <= 100.0:
            raise ValueError(f"speed_pct must be -100.0–100.0, got {speed_pct}")
        result = self._request("drive", {"speed_pct": speed_pct, "ttl_ms": ttl_ms})
        return int(result["motors"])

    def steer(self, angle_deg: float, ttl_ms: int = 500) -> None:
        """Set the steering servo to the requested angle.

        Parameters
        ----------
        angle_deg : float
            Target angle in degrees (0.0–180.0). 90° is centre / straight.
        ttl_ms : int
            Lease TTL in milliseconds. Default: 500.

        Raises
        ------
        ValueError
            If ``angle_deg`` is out of range.
        HatError
            On hardware write failure or if steering servo is not configured.
        HatConnectionError
            If the socket connection is lost.
        """
        if not 0.0 <= angle_deg <= 180.0:
            raise ValueError(f"angle_deg must be 0.0–180.0, got {angle_deg}")
        self._request("steer", {"angle_deg": angle_deg, "ttl_ms": ttl_ms})

    def pan_camera(self, angle_deg: float, ttl_ms: int = 500) -> None:
        """Set the camera pan (horizontal, left/right) servo angle.

        Parameters
        ----------
        angle_deg : float
            Target angle in degrees (0.0–180.0). 90° is centred.
        ttl_ms : int
            Lease TTL in milliseconds. Default: 500.

        Raises
        ------
        ValueError
            If ``angle_deg`` is out of range.
        HatError
            On hardware write failure or if camera_pan servo is not configured.
        HatConnectionError
            If the socket connection is lost.
        """
        if not 0.0 <= angle_deg <= 180.0:
            raise ValueError(f"angle_deg must be 0.0–180.0, got {angle_deg}")
        self._request("pan_camera", {"angle_deg": angle_deg, "ttl_ms": ttl_ms})

    def tilt_camera(self, angle_deg: float, ttl_ms: int = 500) -> None:
        """Set the camera tilt (vertical, up/down) servo angle.

        Parameters
        ----------
        angle_deg : float
            Target angle in degrees (0.0–180.0). 90° is centred.
        ttl_ms : int
            Lease TTL in milliseconds. Default: 500.

        Raises
        ------
        ValueError
            If ``angle_deg`` is out of range.
        HatError
            On hardware write failure or if camera_tilt servo is not configured.
        HatConnectionError
            If the socket connection is lost.
        """
        if not 0.0 <= angle_deg <= 180.0:
            raise ValueError(f"angle_deg must be 0.0–180.0, got {angle_deg}")
        self._request("tilt_camera", {"angle_deg": angle_deg, "ttl_ms": ttl_ms})

    def read_grayscale(self) -> GrayscaleResult:
        """Read all three grayscale sensor ADC channels.

        Returns raw 12-bit ADC values for the left, center, and right
        grayscale sensors (cliff / line detection).  Channel assignments are
        configured in the daemon's ``[sensors]`` config table.

        Returns
        -------
        GrayscaleResult
            ``channels``: ADC channel numbers [left, center, right].
            ``values``: raw 12-bit readings (0–4095) for each channel.

        Raises
        ------
        HatError
            On hardware read failure.
        HatConnectionError
            If the socket connection is lost.
        """
        result = self._request("read_grayscale", {})
        return GrayscaleResult(
            channels=list(result["channels"]),
            values=list(result["values"]),
        )

    def read_ultrasonic(self) -> UltrasonicResult:
        """Trigger the ultrasonic sensor and return the measured distance.

        Sends a ``read_ultrasonic`` IPC request.  The daemon drives the TRIG
        GPIO pin, measures the ECHO pulse width, and returns the computed
        distance (2–400 cm range for HC-SR04-compatible sensors).

        Returns
        -------
        UltrasonicResult
            ``distance_cm``: distance in centimetres.

        Raises
        ------
        HatError
            On measurement timeout, no-echo (object out of range), or GPIO
            failure.
        HatConnectionError
            If the socket connection is lost.
        """
        result = self._request("read_ultrasonic", {})
        return UltrasonicResult(distance_cm=float(result["distance_cm"]))

    def enable_speaker(self) -> None:
        """Enable the on-board speaker amplifier.

        Sends an ``enable_speaker`` IPC request to the daemon, which turns on
        the amplifier so that audio playback is audible.

        Raises
        ------
        HatError
            If the daemon reports a failure enabling the speaker.
        HatConnectionError
            If the socket connection is lost.
        """
        self._request("enable_speaker", {})

    def disable_speaker(self) -> None:
        """Disable the on-board speaker amplifier.

        Sends a ``disable_speaker`` IPC request to the daemon, which turns off
        the amplifier after audio playback to save power.

        Raises
        ------
        HatError
            If the daemon reports a failure disabling the speaker.
        HatConnectionError
            If the socket connection is lost.
        """
        self._request("disable_speaker", {})

    def set_volume(self, volume_pct: int) -> None:
        """Set the speaker output volume via the daemon's ALSA mixer control.

        Parameters
        ----------
        volume_pct:
            Target volume, 0–100 (%).

        Raises
        ------
        ValueError
            If *volume_pct* is outside 0–100.
        HatError
            If the daemon reports a hardware error setting volume.
        HatConnectionError
            If the socket connection is lost.
        """
        if not 0 <= volume_pct <= 100:
            raise ValueError(f"volume_pct must be 0–100, got {volume_pct!r}")
        self._request("set_volume", {"volume_pct": volume_pct})

    def get_volume(self) -> int:
        """Return the current output volume (0–100 %) from the ALSA mixer.

        Returns
        -------
        int
            Current output volume percentage.

        Raises
        ------
        HatError
            If the daemon reports a hardware error reading volume.
        HatConnectionError
            If the socket connection is lost.
        """
        result = self._request("get_volume", {})
        return int(result["volume_pct"])

    def set_mic_gain(self, gain_pct: int) -> None:
        """Set the microphone capture gain on the USB mic via ALSA.

        Parameters
        ----------
        gain_pct:
            Target capture gain, 0–100 (%).

        Raises
        ------
        ValueError
            If *gain_pct* is outside 0–100.
        HatError
            If the daemon reports a hardware error setting mic gain.
        HatConnectionError
            If the socket connection is lost.
        """
        if not 0 <= gain_pct <= 100:
            raise ValueError(f"gain_pct must be 0–100, got {gain_pct!r}")
        self._request("set_mic_gain", {"gain_pct": gain_pct})

    def get_mic_gain(self) -> int:
        """Return the current microphone capture gain (0–100 %) from ALSA.

        Returns
        -------
        int
            Current mic capture gain percentage.

        Raises
        ------
        HatError
            If the daemon reports a hardware error reading mic gain.
        HatConnectionError
            If the socket connection is lost.
        """
        result = self._request("get_mic_gain", {})
        return int(result["gain_pct"])

    # -----------------------------------------------------------------------
    # Calibration methods
    # -----------------------------------------------------------------------

    def get_calibration(self) -> CalibrationSnapshot:
        """Return a full snapshot of the daemon's in-memory calibration store.

        Returns
        -------
        CalibrationSnapshot

        Raises
        ------
        HatError
            On daemon error.
        HatConnectionError
            If the socket is unavailable.
        """
        result = self._request("get_calibration", {})
        motors = [
            MotorCalibrationEntry(
                channel=m["channel"],
                speed_scale=float(m["speed_scale"]),
                deadband_pct=float(m["deadband_pct"]),
                reversed=bool(m["reversed"]),
            )
            for m in result["motors"]
        ]
        servos = {
            name: ServoCalibrationEntry(servo=name, trim_us=int(v["trim_us"]))
            for name, v in result["servos"].items()
        }
        grayscale = [
            GrayscaleCalibrationEntry(
                adc_channel=int(g["adc_channel"]),
                white_raw=int(g["white_raw"]),
                black_raw=int(g["black_raw"]),
            )
            for g in result["grayscale"]
        ]
        return CalibrationSnapshot(motors=motors, servos=servos, grayscale=grayscale)

    def set_motor_calibration(
        self,
        channel: int,
        speed_scale: float | None = None,
        deadband_pct: float | None = None,
        reversed: bool | None = None,
    ) -> MotorCalibrationEntry:
        """Adjust calibration values for one motor channel (partial update).

        Parameters
        ----------
        channel:
            Motor index (0-based).
        speed_scale:
            New speed scale multiplier (0.5–2.0). Omit to leave unchanged.
        deadband_pct:
            New deadband percentage (0.0–20.0). Omit to leave unchanged.
        reversed:
            New direction flip flag. Omit to leave unchanged.

        Raises
        ------
        HatError
            ``INVALID_PARAMS`` if channel is out of range or a value is invalid.
        """
        params: dict = {"channel": channel}
        if speed_scale is not None:
            params["speed_scale"] = speed_scale
        if deadband_pct is not None:
            params["deadband_pct"] = deadband_pct
        if reversed is not None:
            params["reversed"] = reversed
        result = self._request("set_motor_calibration", params)
        return MotorCalibrationEntry(
            channel=int(result["channel"]),
            speed_scale=float(result["speed_scale"]),
            deadband_pct=float(result["deadband_pct"]),
            reversed=bool(result["reversed"]),
        )

    def set_servo_calibration(self, servo: str, trim_us: int) -> ServoCalibrationEntry:
        """Set the trim offset (µs) for a named servo.

        Parameters
        ----------
        servo:
            Logical servo name: ``"steering"``, ``"camera_pan"``, or ``"camera_tilt"``.
        trim_us:
            Signed trim in microseconds (−500–+500).

        Raises
        ------
        HatError
            ``INVALID_PARAMS`` if *servo* is unrecognised or *trim_us* out of range.
        """
        result = self._request("set_servo_calibration", {"servo": servo, "trim_us": trim_us})
        return ServoCalibrationEntry(servo=str(result["servo"]), trim_us=int(result["trim_us"]))

    def calibrate_grayscale(self, channel: int, surface: str) -> GrayscaleCaptureResult:
        """Capture a live ADC reading and store it as the white or black reference.

        Parameters
        ----------
        channel:
            Sensor position index: 0 = left, 1 = center, 2 = right.
        surface:
            ``"white"`` or ``"black"``.

        Raises
        ------
        HatError
            ``INVALID_PARAMS`` if the capture would violate ``white_raw < black_raw``.
            ``HARDWARE_ERROR`` on ADC read failure.
        """
        result = self._request("calibrate_grayscale", {"channel": channel, "surface": surface})
        return GrayscaleCaptureResult(
            channel=int(result["channel"]),
            adc_channel=int(result["adc_channel"]),
            surface=str(result["surface"]),
            raw_value=int(result["raw_value"]),
            stored=bool(result["stored"]),
        )

    def save_calibration(self) -> SaveCalibrationResult:
        """Persist the current in-memory calibration store to disk.

        Raises
        ------
        HatError
            ``HARDWARE_ERROR`` on filesystem write failure.
        """
        result = self._request("save_calibration", {})
        return SaveCalibrationResult(saved=bool(result["saved"]), path=str(result["path"]))

    def reset_calibration(self) -> bool:
        """Revert the in-memory calibration store to factory defaults.

        The file on disk is NOT overwritten; call ``save_calibration`` afterwards
        to make the reset permanent.

        Returns
        -------
        bool
            Always ``True`` on success.
        """
        result = self._request("reset_calibration", {})
        return bool(result["reset"])

    def read_grayscale_normalized(self) -> NormalizedGrayscaleResult:
        """Read all three grayscale sensors and return normalised values.

        Values are normalised against the captured surface calibration:
        0.0 = white/reflective, 1.0 = black/non-reflective.

        Raises
        ------
        HatError
            ``HARDWARE_ERROR`` on ADC read failure.
        """
        result = self._request("read_grayscale_normalized", {})
        return NormalizedGrayscaleResult(
            channels=[int(c) for c in result["channels"]],
            normalized=[float(v) for v in result["normalized"]],
        )

    # -----------------------------------------------------------------------
    # Routine methods
    # -----------------------------------------------------------------------

    def start_routine(
        self,
        name: str,
        speed_pct: float | None = None,
        obstacle_threshold_cm: float | None = None,
        cliff_threshold_normalized: float | None = None,
        max_duration_s: int | None = None,
    ) -> RoutineStartResult:
        """Start an autonomous routine by *name*.

        Parameters
        ----------
        name:
            Routine identifier.  Currently only ``"explore"`` is supported.
        speed_pct:
            Forward drive speed override (1–100 %).
        obstacle_threshold_cm:
            Distance at which an obstacle triggers avoidance (cm).
        cliff_threshold_normalized:
            Normalised grayscale value above which a cliff is detected (0–1).
        max_duration_s:
            Maximum run time in seconds.

        Raises
        ------
        HatError
            ``ALREADY_RUNNING`` if a routine is already active.
            ``INVALID_PARAMS`` if *name* is unknown or a parameter is out of range.
        """
        params: dict = {"name": name}
        if speed_pct is not None:
            params["speed_pct"] = speed_pct
        if obstacle_threshold_cm is not None:
            params["obstacle_threshold_cm"] = obstacle_threshold_cm
        if cliff_threshold_normalized is not None:
            params["cliff_threshold_normalized"] = cliff_threshold_normalized
        if max_duration_s is not None:
            params["max_duration_s"] = max_duration_s
        result = self._request("start_routine", params)
        return RoutineStartResult(
            name=result["name"],
            started_at_uptime_s=int(result["started_at_uptime_s"]),
        )

    def stop_routine(self) -> RoutineStopResult:
        """Stop the currently running routine and return final statistics.

        Raises
        ------
        HatError
            ``INVALID_PARAMS`` if no routine is running.
        """
        result = self._request("stop_routine", {})
        return RoutineStopResult(
            name=result["name"],
            ran_for_s=int(result["ran_for_s"]),
            obstacles_avoided=int(result["obstacles_avoided"]),
            cliffs_avoided=int(result["cliffs_avoided"]),
            stop_reason=result["stop_reason"],
        )

    def get_routine_status(self) -> RoutineStatusResult:
        """Return the current routine status snapshot.

        When no routine is running, ``running`` is ``False`` and all other
        fields are ``None``.
        """
        result = self._request("get_routine_status", {})
        return RoutineStatusResult(
            running=bool(result["running"]),
            name=result.get("name"),
            elapsed_s=result.get("elapsed_s"),
            obstacles_avoided=result.get("obstacles_avoided"),
            cliffs_avoided=result.get("cliffs_avoided"),
        )


# ---------------------------------------------------------------------------
# Calibration dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MotorCalibrationEntry:
    """Per-motor calibration entry."""

    channel: int
    speed_scale: float
    deadband_pct: float
    reversed: bool


@dataclass
class ServoCalibrationEntry:
    """Per-servo calibration entry."""

    servo: str
    trim_us: int


@dataclass
class GrayscaleCalibrationEntry:
    """Per-sensor grayscale calibration entry.

    Attributes
    ----------
    adc_channel : int
        ADC bus channel number (from ``config.sensors.grayscale``).
    white_raw : int
        Raw ADC value captured from a white/reflective surface.
    black_raw : int
        Raw ADC value captured from a black/non-reflective surface.
    """

    adc_channel: int
    white_raw: int
    black_raw: int


@dataclass
class CalibrationSnapshot:
    """Full calibration snapshot from ``get_calibration``."""

    motors: list[MotorCalibrationEntry]
    servos: dict[str, ServoCalibrationEntry]
    grayscale: list[GrayscaleCalibrationEntry]


@dataclass
class GrayscaleCaptureResult:
    """Result from ``calibrate_grayscale``."""

    channel: int
    adc_channel: int
    surface: str
    raw_value: int
    stored: bool


@dataclass
class NormalizedGrayscaleResult:
    """Result from ``read_grayscale_normalized``."""

    channels: list[int]
    normalized: list[float]


@dataclass
class SaveCalibrationResult:
    """Result from ``save_calibration``."""

    saved: bool
    path: str


# ---------------------------------------------------------------------------
# Routine dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RoutineStartResult:
    """Result from ``start_routine``."""

    name: str
    started_at_uptime_s: int


@dataclass
class RoutineStatusResult:
    """Result from ``get_routine_status``."""

    running: bool
    name: str | None
    elapsed_s: int | None
    obstacles_avoided: int | None
    cliffs_avoided: int | None


@dataclass
class RoutineStopResult:
    """Result from ``stop_routine``."""

    name: str
    ran_for_s: int
    obstacles_avoided: int
    cliffs_avoided: int
    stop_reason: str
