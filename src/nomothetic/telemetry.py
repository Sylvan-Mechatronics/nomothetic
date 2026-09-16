"""MQTT telemetry publisher for nomon fleet devices.

This module provides a background publisher that periodically sends structured
JSON telemetry to an MQTT broker. Designed to run as a daemon thread alongside
the REST API without any coupling to the APIServer lifecycle.

Classes
-------
TelemetryPublisher
    Publishes device and camera telemetry over MQTT with reconnect/retry logic.
"""

import json
import logging
import os
import socket
import threading
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
    mqtt = None  # type: ignore
    CallbackAPIVersion = None  # type: ignore

logger = logging.getLogger(__name__)


def mqtt_security_from_env() -> dict[str, Any]:
    """Read the shared MQTT credential/TLS settings from the environment.

    Environment variables (review finding S-4 — the broker link was plaintext
    and unauthenticated by default):

    ``NOMON_MQTT_USERNAME`` / ``NOMON_MQTT_PASSWORD``
        Broker credentials (both required to enable auth).
    ``NOMON_MQTT_TLS``
        ``1``/``true`` to connect over TLS using the system CA bundle.
    ``NOMON_MQTT_CA_CERT``
        Path to a CA certificate for a private broker CA (implies TLS).

    Returns
    -------
    dict
        ``{"username", "password", "tls", "ca_cert"}`` suitable for the
        keyword arguments of the three MQTT client classes.
    """
    username = os.environ.get("NOMON_MQTT_USERNAME", "").strip() or None
    password = os.environ.get("NOMON_MQTT_PASSWORD", "") or None
    ca_cert = os.environ.get("NOMON_MQTT_CA_CERT", "").strip() or None
    tls = os.environ.get("NOMON_MQTT_TLS", "").strip().lower() in ("1", "true", "yes")
    return {
        "username": username,
        "password": password,
        "tls": tls or ca_cert is not None,
        "ca_cert": ca_cert,
    }


def mqtt_port_from_env(tls: bool) -> int:
    """Return ``NOMON_MQTT_PORT``, defaulting to 8883 under TLS and 1883 otherwise."""
    raw = os.environ.get("NOMON_MQTT_PORT", "").strip()
    if raw:
        return int(raw)
    return 8883 if tls else 1883


def apply_mqtt_security(
    client: Any,
    *,
    username: Optional[str],
    password: Optional[str],
    tls: bool,
    ca_cert: Optional[str],
    role: str,
) -> None:
    """Configure credentials and TLS on a paho client; warn when neither is set.

    Parameters
    ----------
    client : paho.mqtt.client.Client
        The client to configure (before ``connect``).
    username, password : str or None
        Broker credentials; applied when *username* is set.
    tls : bool
        Enable TLS (system CA bundle unless *ca_cert* is given).
    ca_cert : str or None
        Private CA certificate path.
    role : str
        Label for the warning log (``"telemetry"``, ``"autonomy"``, ...).
    """
    if username:
        client.username_pw_set(username, password)
    if tls:
        client.tls_set(ca_certs=ca_cert) if ca_cert else client.tls_set()
    if not username and not tls:
        logger.warning(
            "MQTT %s link is unauthenticated and cleartext; set NOMON_MQTT_USERNAME/"
            "NOMON_MQTT_PASSWORD and NOMON_MQTT_TLS (review finding S-4)",
            role,
        )


_BACKOFF_BASE: float = 1.0
_BACKOFF_CAP: float = 60.0


class TelemetryPublisher:
    """Publishes structured JSON telemetry to an MQTT broker.

    Runs as a daemon background thread.  Handles broker unavailability
    with exponential back-off reconnect logic.  The publisher is fully
    independent of ``nomothetic.api`` — start it alongside the API server at
    application startup.

    Parameters
    ----------
    broker : str
        Hostname or IP address of the MQTT broker.
    port : int, optional
        TCP port of the MQTT broker (default: 1883).
    topic : str, optional
        MQTT topic to publish to (default: ``"nomon/telemetry"``).
    device_id : str, optional
        Identifier for this device.  Auto-detected if ``None``:
        checks ``NOMON_DEVICE_ID`` env var, then ``/proc/cpuinfo``
        (Raspberry Pi serial), then ``socket.gethostname()``.
    camera : Camera, optional
        Live camera instance whose status is included in the payload.
        When ``None``, the ``"camera"`` field is published as ``null``.
    hat_client : HatClient, optional
        Live HAT client used to read battery voltage for the payload.  When
        ``None``, ``battery_voltage`` is published as ``0.0`` (best-effort).
    interval : float, optional
        Seconds between publishes (default: 30.0).
    qos : int, optional
        MQTT QoS level — 0, 1, or 2 (default: 1).

    Raises
    ------
    ImportError
        If ``paho-mqtt`` is not installed.

    Examples
    --------
    >>> pub = TelemetryPublisher(broker="192.168.1.100")
    >>> thread = pub.start_background()
    >>> # ... later ...
    >>> pub.stop()
    >>> thread.join()
    """

    _stop_event: threading.Event
    _connected: bool
    _client: Any

    def __init__(
        self,
        broker: str,
        port: int = 1883,
        topic: str = "nomon/telemetry",
        device_id: Optional[str] = None,
        camera: Optional[Any] = None,
        hat_client: Optional[Any] = None,
        interval: float = 30.0,
        qos: int = 1,
        username: Optional[str] = None,
        password: Optional[str] = None,
        tls: bool = False,
        ca_cert: Optional[str] = None,
    ) -> None:
        if mqtt is None:
            raise ImportError(
                "paho-mqtt is required for telemetry. "
                "Install with: pip install 'nomothetic[telemetry]'"
            )

        self.broker = broker
        self.port = port
        self.topic = topic
        self.device_id = device_id or self.get_device_id()
        self.camera = camera
        self.hat_client = hat_client
        self.interval = interval
        self.qos = qos
        self.username = username
        self.password = password
        self.tls = tls
        self.ca_cert = ca_cert

        self._stop_event: threading.Event = threading.Event()
        self._connected: bool = False
        self._client: Any = self._create_client()

    # -------------------------------------------------------------------------
    # Construction helpers
    # -------------------------------------------------------------------------

    @classmethod
    def from_env(
        cls, camera: Optional[Any] = None, hat_client: Optional[Any] = None
    ) -> "TelemetryPublisher":
        """Create a ``TelemetryPublisher`` from environment variables.

        Reads configuration from the process environment (or a ``.env``
        file if ``python-dotenv`` has already been loaded).

        Environment variables
        ---------------------
        NOMON_MQTT_BROKER : str
            Broker hostname or IP (required).
        NOMON_MQTT_PORT : int
            Broker port (default: ``1883``).
        NOMON_MQTT_TOPIC : str
            Publish topic (default: ``"nomon/telemetry"``).
        NOMON_MQTT_INTERVAL : float
            Seconds between publishes (default: ``30.0``).
        NOMON_DEVICE_ID : str
            Device identifier (default: auto-detected).

        Parameters
        ----------
        camera : Camera, optional
            Live camera instance to include in payloads.
        hat_client : HatClient, optional
            Live HAT client for battery readings.  When ``None``, one is
            constructed best-effort (lazy connect); failures are ignored and
            ``battery_voltage`` is then reported as ``0.0``.

        Returns
        -------
        TelemetryPublisher
            Configured publisher instance.

        Raises
        ------
        ValueError
            If ``NOMON_MQTT_BROKER`` is not set.
        """
        broker = os.environ.get("NOMON_MQTT_BROKER", "").strip()
        if not broker:
            raise ValueError("NOMON_MQTT_BROKER environment variable is required for telemetry.")

        security = mqtt_security_from_env()
        port = mqtt_port_from_env(security["tls"])
        topic = os.environ.get("NOMON_MQTT_TOPIC", "nomon/telemetry")
        interval = float(os.environ.get("NOMON_MQTT_INTERVAL", "30.0"))
        device_id = os.environ.get("NOMON_DEVICE_ID") or None

        if hat_client is None:
            try:
                from nomothetic.hat import HatClient

                hat_client = HatClient()
            except Exception as exc:  # noqa: BLE001
                logger.debug("HAT client unavailable for telemetry battery reads: %s", exc)

        return cls(
            broker=broker,
            port=port,
            topic=topic,
            device_id=device_id,
            camera=camera,
            hat_client=hat_client,
            interval=interval,
            **security,
        )

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def start_background(self) -> threading.Thread:
        """Start the telemetry publisher in a daemon background thread.

        Returns
        -------
        threading.Thread
            The daemon thread running the publisher loop.
        """
        self._stop_event.clear()
        thread = threading.Thread(target=self._run_loop, name="nomon-telemetry", daemon=True)
        thread.start()
        logger.info(
            "Telemetry publisher started (broker=%s:%d topic=%s interval=%.1fs)",
            self.broker,
            self.port,
            self.topic,
            self.interval,
        )
        return thread

    def stop(self) -> None:
        """Signal the publisher loop to stop.

        Sets the internal stop event; the background thread will exit
        after the current sleep interval.  Call ``thread.join()`` after
        this to wait for a clean shutdown.
        """
        self._stop_event.set()
        try:
            self._client.disconnect()
        except Exception as exc:
            logger.debug(
                "Error while disconnecting MQTT client during telemetry stop: %s",
                exc,
                exc_info=True,
            )
        logger.info("Telemetry publisher stop requested.")

    def publish_now(self) -> bool:
        """Publish a single telemetry payload immediately.

        Connects to the broker if not already connected, publishes
        one payload, then returns.

        Returns
        -------
        bool
            ``True`` if the payload was published successfully,
            ``False`` on any error.
        """
        try:
            if not self._connected:
                self._client.connect(self.broker, self.port, keepalive=60)
                self._client.loop_start()
                self._connected = True

            payload = json.dumps(self.build_payload())
            result = self._client.publish(self.topic, payload, qos=self.qos)
            result.wait_for_publish()
            logger.debug("Telemetry published to %s", self.topic)
            return True
        except Exception as exc:
            logger.warning("Telemetry publish failed: %s", exc)
            self._connected = False
            return False

    def build_payload(self) -> dict[str, Any]:
        """Build the JSON-serialisable telemetry payload dict.

        Returns
        -------
        dict[str, Any]
            Payload with device ID, timestamp, nomon version, and
            optional camera status.
        """
        from nomothetic import __version__

        camera_data: Optional[dict[str, Any]] = None
        if self.camera is not None:
            try:
                camera_data = {
                    "ready": True,
                    "recording": bool(self.camera._is_recording),
                    "resolution": f"{self.camera.width}x{self.camera.height}",
                    "fps": int(self.camera.fps),
                    "encoder": str(self.camera.encoder),
                }
            except Exception as exc:
                logger.warning("Could not read camera status for payload: %s", exc)
                camera_data = {"ready": False, "error": str(exc)}

        return {
            "device_id": self.device_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "nomon_version": __version__,
            "battery_voltage": self._read_battery_voltage(),
            "cpu_temp_c": self._read_cpu_temp_c(),
            "uptime_seconds": self._read_uptime_seconds(),
            "camera": camera_data,
        }

    def _read_battery_voltage(self) -> float:
        """Read battery voltage via the HAT client (best-effort, 0.0 on failure)."""
        if self.hat_client is None:
            return 0.0
        try:
            return float(self.hat_client.get_battery_voltage())
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not read battery voltage for payload: %s", exc)
            return 0.0

    @staticmethod
    def _read_cpu_temp_c() -> float:
        """Read CPU temperature in °C from sysfs (best-effort, 0.0 on failure)."""
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                return int(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            return 0.0

    @staticmethod
    def _read_uptime_seconds() -> int:
        """Read system uptime in seconds from ``/proc/uptime`` (0 on failure)."""
        try:
            with open("/proc/uptime") as f:
                return int(float(f.read().split()[0]))
        except (OSError, ValueError, IndexError):
            return 0

    @staticmethod
    def get_device_id() -> str:
        """Determine a unique device identifier for this Pi.

        Resolution order:
        1. ``NOMON_DEVICE_ID`` environment variable.
        2. Raspberry Pi serial number from ``/proc/cpuinfo``.
        3. ``socket.gethostname()``.

        Returns
        -------
        str
            Device identifier string.
        """
        env_id = os.environ.get("NOMON_DEVICE_ID", "").strip()
        if env_id:
            return env_id

        # Try Raspberry Pi serial number
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("Serial"):
                        serial = line.split(":")[-1].strip().lstrip("0")
                        if serial:
                            return f"pi-{serial}"
        except OSError:
            pass

        return socket.gethostname()

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _create_client(self) -> Any:
        """Create and configure a paho-mqtt Client instance.

        Returns
        -------
        paho.mqtt.client.Client
            Configured MQTT client.
        """
        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=f"nomon-{self.device_id}",
        )
        apply_mqtt_security(
            client,
            username=self.username,
            password=self.password,
            tls=self.tls,
            ca_cert=self.ca_cert,
            role="telemetry",
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        return client

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        connect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        """Handle successful broker connection."""
        if reason_code.is_failure:
            logger.warning("MQTT connect refused: %s", reason_code)
            self._connected = False
        else:
            logger.info("MQTT connected to %s:%d", self.broker, self.port)
            self._connected = True

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        """Handle broker disconnection."""
        logger.info("MQTT disconnected (reason: %s)", reason_code)
        self._connected = False

    def _run_loop(self) -> None:
        """Background thread: connect, publish periodically, reconnect on failure."""
        backoff = _BACKOFF_BASE

        while not self._stop_event.is_set():
            if not self._connected:
                try:
                    self._client.connect(self.broker, self.port, keepalive=60)
                    self._client.loop_start()
                    self._connected = True
                    backoff = _BACKOFF_BASE  # reset on successful connect
                except Exception as exc:
                    logger.warning("MQTT connect failed (%s). Retrying in %.1fs.", exc, backoff)
                    self._stop_event.wait(timeout=backoff)
                    backoff = min(backoff * 2, _BACKOFF_CAP)
                    continue

            # Publish telemetry
            try:
                payload = json.dumps(self.build_payload())
                self._client.publish(self.topic, payload, qos=self.qos)
                logger.debug("Telemetry published to %s", self.topic)
            except Exception as exc:
                logger.warning("Telemetry publish error: %s", exc)
                self._connected = False

            # Wait for next publish (interruptible by stop())
            self._stop_event.wait(timeout=self.interval)

        self._client.loop_stop()
        logger.info("Telemetry publisher stopped.")
