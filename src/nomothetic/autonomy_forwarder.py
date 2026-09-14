"""Best-effort MQTT forwarding of autonomy routine events (device mode).

The brain (autonomon) reports lifecycle events to the device's routine status
sink (:mod:`nomothetic.routine_routes` -> :class:`~nomothetic.routine_log_store.
RoutineLogStore`).  When an MQTT broker is configured, this forwarder mirrors
every recorded event onto an autonomy topic so central nomothetic can persist
fleet-wide autonomy history (autonomon Phase 7).  The broker is the
device->central transport, exactly as for periodic device telemetry — no new
device-authenticated REST ingestion is introduced.

Forwarding is best-effort by design: events are queued to a bounded buffer and
published from a daemon thread with reconnect back-off.  A full buffer or a
publish failure drops events (with a log) rather than ever blocking the
request path or crashing the API.
"""

import json
import logging
import os
import queue
import threading
from typing import TYPE_CHECKING, Any, Optional

try:
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
    mqtt = None  # type: ignore
    CallbackAPIVersion = None  # type: ignore

if TYPE_CHECKING:
    from nomothetic.routine_log_store import RoutineEvent

logger = logging.getLogger(__name__)

_BACKOFF_BASE: float = 1.0
_BACKOFF_CAP: float = 60.0
_QUEUE_POLL_S: float = 0.5
_PUBLISH_TIMEOUT_S: float = 5.0
_MAX_QUEUE_DEFAULT: int = 256

DEFAULT_AUTONOMY_TOPIC = "nomon/autonomy"


class AutonomyEventForwarder:
    """Forwards recorded routine events to an MQTT autonomy topic.

    Attach :meth:`enqueue_event` as the ``on_event`` callback of a
    :class:`~nomothetic.routine_log_store.RoutineLogStore`; every event the
    device records (brain-reported or supervisor-recorded) is then queued and
    published in the background.

    Parameters
    ----------
    broker : str
        Hostname or IP of the MQTT broker.
    port : int, optional
        Broker TCP port (default 1883).
    topic : str, optional
        Topic to publish to (default ``"nomon/autonomy"``).
    device_id : str, optional
        Fallback device identifier stamped on events that carry none.
        Auto-detected when ``None`` (same resolution as telemetry).
    qos : int, optional
        Publish QoS (default 1).
    max_queue : int, optional
        Bounded buffer size; excess events are dropped (default 256).

    Raises
    ------
    ImportError
        If ``paho-mqtt`` is not installed.
    """

    def __init__(
        self,
        broker: str,
        port: int = 1883,
        topic: str = DEFAULT_AUTONOMY_TOPIC,
        device_id: Optional[str] = None,
        qos: int = 1,
        max_queue: int = _MAX_QUEUE_DEFAULT,
        username: Optional[str] = None,
        password: Optional[str] = None,
        tls: bool = False,
        ca_cert: Optional[str] = None,
    ) -> None:
        if mqtt is None:
            raise ImportError(
                "paho-mqtt is required for autonomy event forwarding. "
                "Install with: pip install 'nomothetic[telemetry]'"
            )
        from nomothetic.telemetry import TelemetryPublisher

        self.broker = broker
        self.port = port
        self.topic = topic
        self.device_id = device_id or TelemetryPublisher.get_device_id()
        self.qos = qos
        self.username = username
        self.password = password
        self.tls = tls
        self.ca_cert = ca_cert

        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue)
        self._stop_event = threading.Event()
        self._connected = False
        self._thread: Optional[threading.Thread] = None
        self._client: Any = self._create_client()

    # -------------------------------------------------------------------------
    # Construction helpers
    # -------------------------------------------------------------------------

    @classmethod
    def from_env(cls, device_id: Optional[str] = None) -> Optional["AutonomyEventForwarder"]:
        """Build a forwarder from environment, or ``None`` when no broker is set.

        Reads ``NOMON_MQTT_BROKER`` (required), ``NOMON_MQTT_PORT`` (default
        1883), and ``NOMON_MQTT_AUTONOMY_TOPIC`` (default ``"nomon/autonomy"``).
        Returns ``None`` when ``NOMON_MQTT_BROKER`` is unset so devices without
        a broker simply keep autonomy history device-local.

        Parameters
        ----------
        device_id : str, optional
            Fallback device identifier (auto-detected when ``None``).

        Returns
        -------
        AutonomyEventForwarder or None
        """
        from nomothetic.telemetry import mqtt_port_from_env, mqtt_security_from_env

        broker = os.environ.get("NOMON_MQTT_BROKER", "").strip()
        if not broker:
            return None
        security = mqtt_security_from_env()
        port = mqtt_port_from_env(security["tls"])
        topic = os.environ.get("NOMON_MQTT_AUTONOMY_TOPIC", DEFAULT_AUTONOMY_TOPIC)
        return cls(broker=broker, port=port, topic=topic, device_id=device_id, **security)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def enqueue_event(self, routine: str, event: "RoutineEvent") -> bool:
        """Queue one recorded event for forwarding.  Never raises.

        Matches the :class:`~nomothetic.routine_log_store.RoutineLogStore`
        ``on_event`` callback signature.

        Parameters
        ----------
        routine : str
            Routine name the event was recorded for.
        event : RoutineEvent
            The stored event.

        Returns
        -------
        bool
            ``True`` if queued, ``False`` if dropped (buffer full).
        """
        payload = {
            "device_id": event.device_id or self.device_id,
            "routine": routine,
            "run_id": event.run_id,
            "type": event.type,
            "data": event.data,
            "timestamp": event.timestamp,
        }
        try:
            self._queue.put_nowait(payload)
            return True
        except queue.Full:
            logger.debug(
                "Autonomy forward queue full; dropping %r event for %r", event.type, routine
            )
            return False

    def start_background(self) -> threading.Thread:
        """Start the forwarding loop in a daemon thread.

        Returns
        -------
        threading.Thread
        """
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._run_loop, name="nomon-autonomy-forwarder", daemon=True
        )
        thread.start()
        self._thread = thread
        logger.info(
            "Autonomy event forwarder started (broker=%s:%d topic=%s)",
            self.broker,
            self.port,
            self.topic,
        )
        return thread

    def stop(self) -> None:
        """Signal the forwarding loop to stop and disconnect the client."""
        self._stop_event.set()
        try:
            self._client.disconnect()
        except Exception as exc:
            logger.debug("Error disconnecting autonomy forwarder: %s", exc, exc_info=True)
        logger.info("Autonomy event forwarder stop requested.")

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _create_client(self) -> Any:
        """Create and configure a paho-mqtt client."""
        from nomothetic.telemetry import apply_mqtt_security

        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=f"nomon-autonomy-{self.device_id}",
        )
        apply_mqtt_security(
            client,
            username=self.username,
            password=self.password,
            tls=self.tls,
            ca_cert=self.ca_cert,
            role="autonomy",
        )
        client.on_disconnect = self._on_disconnect
        return client

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        """Mark the connection lost so the loop reconnects before publishing."""
        self._connected = False

    def _publish(self, payload: dict[str, Any]) -> bool:
        """Publish one payload, connecting lazily.  Returns success."""
        try:
            if not self._connected:
                self._client.connect(self.broker, self.port, keepalive=60)
                self._client.loop_start()
                self._connected = True
            result = self._client.publish(self.topic, json.dumps(payload), qos=self.qos)
            result.wait_for_publish(timeout=_PUBLISH_TIMEOUT_S)
            if not result.is_published():
                raise TimeoutError(f"publish not acknowledged in {_PUBLISH_TIMEOUT_S}s")
            logger.debug("Autonomy event published to %s", self.topic)
            return True
        except Exception as exc:  # noqa: BLE001 — forwarding is best-effort
            logger.warning("Autonomy event publish failed: %s", exc)
            self._connected = False
            return False

    def _run_loop(self) -> None:
        """Background thread: drain the queue, publishing with back-off."""
        backoff = _BACKOFF_BASE
        while not self._stop_event.is_set():
            try:
                payload = self._queue.get(timeout=_QUEUE_POLL_S)
            except queue.Empty:
                continue
            # Retry the current event with back-off; the bounded queue caps
            # memory (enqueue drops when full) while a broker outage lasts.
            while not self._stop_event.is_set() and not self._publish(payload):
                self._stop_event.wait(timeout=backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP)
            backoff = _BACKOFF_BASE
        try:
            self._client.loop_stop()
        except Exception as exc:
            logger.debug("Error stopping autonomy forwarder loop: %s", exc, exc_info=True)
        logger.info("Autonomy event forwarder stopped.")
