"""MQTT telemetry consumer for the central nomon API.

The device-side :class:`nomothetic.telemetry.TelemetryPublisher` publishes
structured JSON telemetry to an MQTT broker.  In **central mode** this consumer
subscribes to that topic and persists each reading via a
:class:`~nomothetic.telemetry_store.TelemetryStore`, so the fleet API can serve
telemetry history and the latest snapshot per device.

The broker is the device->central transport: no new device-authenticated REST
ingestion endpoint is introduced (that would re-open the deferred device->central
auth design).  A device's ``device_id`` in the payload is its VIN (the same value
used at fleet registration — see ``NOMON_DEVICE_ID`` / Phase 23 ``_derive_vin``).

The parse step is a pure function (:func:`reading_from_payload`) so it is unit
testable without a broker; :meth:`TelemetryConsumer.ingest` is an awaitable that
parses and stores, and the MQTT callback simply schedules it on the API event
loop.

The same broker connection also ingests **autonomy events** (autonomon Phase 7):
devices forward the lifecycle events their routine sink records onto an autonomy
topic (:mod:`nomothetic.autonomy_forwarder`); when an
:class:`~nomothetic.autonomy_store.AutonomyStore` is provided, this consumer
subscribes to that topic too and persists runs/events for the fleet API.
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from nomothetic.autonomy_store import AutonomyEventItem, AutonomyStore
from nomothetic.telemetry_store import TelemetryReadingItem, TelemetryStore

try:
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
    mqtt = None  # type: ignore
    CallbackAPIVersion = None  # type: ignore

logger = logging.getLogger(__name__)

_BACKOFF_BASE: float = 1.0
_BACKOFF_CAP: float = 60.0


def reading_from_payload(
    payload: dict[str, Any],
) -> Optional[tuple[str, TelemetryReadingItem]]:
    """Map a telemetry payload dict to ``(vin, TelemetryReadingItem)``.

    Returns ``None`` for an unusable payload (no ``device_id``), so a malformed
    or partial message is skipped rather than raising.  Numeric fields default to
    ``0`` when absent, since the ``TelemetryReading`` schema marks them NOTNULL.

    Parameters
    ----------
    payload : dict
        Decoded JSON telemetry payload (see ``TelemetryPublisher.build_payload``).

    Returns
    -------
    tuple[str, TelemetryReadingItem] or None
    """
    vin = payload.get("device_id")
    if not isinstance(vin, str) or not vin.strip():
        return None

    def _num(key: str) -> float:
        val = payload.get(key)
        return float(val) if isinstance(val, (int, float)) else 0.0

    recorded_at = payload.get("timestamp")
    if not isinstance(recorded_at, str) or not recorded_at:
        recorded_at = datetime.now(timezone.utc).isoformat()

    item = TelemetryReadingItem(
        battery_voltage=_num("battery_voltage"),
        cpu_temp_c=_num("cpu_temp_c"),
        uptime_seconds=int(_num("uptime_seconds")),
        recorded_at=recorded_at,
    )
    return vin.strip(), item


def autonomy_event_from_payload(
    payload: dict[str, Any],
) -> Optional[tuple[str, str, str, AutonomyEventItem]]:
    """Map a forwarded autonomy payload to ``(vin, routine, run_id, event)``.

    Returns ``None`` for an unusable payload — one lacking a ``device_id``,
    ``routine``, ``run_id``, or ``type`` — so partial messages are skipped
    rather than raising.  ``run_id`` is required because central history is
    segmented per run (unattributable events are not stored).

    Parameters
    ----------
    payload : dict
        Decoded JSON autonomy payload (see
        ``AutonomyEventForwarder.enqueue_event``).

    Returns
    -------
    tuple[str, str, str, AutonomyEventItem] or None
    """

    def _req(key: str) -> Optional[str]:
        val = payload.get(key)
        if not isinstance(val, str) or not val.strip():
            return None
        return val.strip()

    vin = _req("device_id")
    routine = _req("routine")
    run_id = _req("run_id")
    event_type = _req("type")
    if vin is None or routine is None or run_id is None or event_type is None:
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}

    recorded_at = payload.get("timestamp")
    if not isinstance(recorded_at, str) or not recorded_at:
        recorded_at = datetime.now(timezone.utc).isoformat()

    item = AutonomyEventItem(event_type=event_type, data=data, recorded_at=recorded_at)
    return vin, routine, run_id, item


class TelemetryConsumer:
    """Subscribes to the telemetry topic and persists readings to a store.

    Runs as a daemon background thread (paho's network loop).  The ``store`` is
    written via the API event loop captured at :meth:`start_background` so the
    async ArcadeDB client is used from its own loop.

    Parameters
    ----------
    store : TelemetryStore
        Persistence backend for parsed readings.
    broker : str
        Hostname or IP of the MQTT broker.
    port : int, optional
        Broker TCP port (default 1883).
    topic : str, optional
        Topic to subscribe to (default ``"nomon/telemetry"``).
    qos : int, optional
        Subscription QoS (default 1).
    autonomy_store : AutonomyStore, optional
        When given, the consumer also subscribes to *autonomy_topic* and
        persists forwarded autonomy events there.
    autonomy_topic : str, optional
        Autonomy event topic (default ``"nomon/autonomy"``).

    Raises
    ------
    ImportError
        If ``paho-mqtt`` is not installed.
    """

    def __init__(
        self,
        store: TelemetryStore,
        broker: str,
        port: int = 1883,
        topic: str = "nomon/telemetry",
        qos: int = 1,
        autonomy_store: Optional[AutonomyStore] = None,
        autonomy_topic: str = "nomon/autonomy",
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
        self._store = store
        self._autonomy_store = autonomy_store
        self.broker = broker
        self.port = port
        self.topic = topic
        self.autonomy_topic = autonomy_topic
        self.qos = qos
        self.username = username
        self.password = password
        self.tls = tls
        self.ca_cert = ca_cert

        self._stop_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._client: Any = self._create_client()

    # -------------------------------------------------------------------------
    # Construction helpers
    # -------------------------------------------------------------------------

    @classmethod
    def from_env(
        cls, store: TelemetryStore, autonomy_store: Optional[AutonomyStore] = None
    ) -> Optional["TelemetryConsumer"]:
        """Build a consumer from environment, or ``None`` when no broker is set.

        Reads ``NOMON_MQTT_BROKER`` (required), ``NOMON_MQTT_PORT`` (default
        1883), ``NOMON_MQTT_TOPIC`` (default ``"nomon/telemetry"``), and
        ``NOMON_MQTT_AUTONOMY_TOPIC`` (default ``"nomon/autonomy"``).  Returns
        ``None`` when ``NOMON_MQTT_BROKER`` is unset so central mode runs without
        telemetry ingestion (history is simply empty).

        Parameters
        ----------
        store : TelemetryStore
            Persistence backend for device telemetry readings.
        autonomy_store : AutonomyStore, optional
            Persistence backend for forwarded autonomy events; when ``None``
            the autonomy topic is not subscribed.

        Returns
        -------
        TelemetryConsumer or None
        """
        from nomothetic.telemetry import mqtt_port_from_env, mqtt_security_from_env

        broker = os.environ.get("NOMON_MQTT_BROKER", "").strip()
        if not broker:
            return None
        security = mqtt_security_from_env()
        port = mqtt_port_from_env(security["tls"])
        topic = os.environ.get("NOMON_MQTT_TOPIC", "nomon/telemetry")
        autonomy_topic = os.environ.get("NOMON_MQTT_AUTONOMY_TOPIC", "nomon/autonomy")
        return cls(
            store=store,
            broker=broker,
            port=port,
            topic=topic,
            autonomy_store=autonomy_store,
            autonomy_topic=autonomy_topic,
            **security,
        )

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def ingest(self, payload: dict[str, Any]) -> bool:
        """Parse a payload and persist the reading.

        Parameters
        ----------
        payload : dict
            Decoded telemetry payload.

        Returns
        -------
        bool
            ``True`` if a reading was stored, ``False`` if the payload was
            unusable and skipped.
        """
        parsed = reading_from_payload(payload)
        if parsed is None:
            return False
        vin, item = parsed
        await self._store.record_reading(vin, item)
        return True

    async def ingest_autonomy(self, payload: dict[str, Any]) -> bool:
        """Parse a forwarded autonomy payload and persist the event.

        Parameters
        ----------
        payload : dict
            Decoded autonomy event payload.

        Returns
        -------
        bool
            ``True`` if an event was stored, ``False`` if no autonomy store is
            configured or the payload was unusable and skipped.
        """
        if self._autonomy_store is None:
            return False
        parsed = autonomy_event_from_payload(payload)
        if parsed is None:
            return False
        vin, routine, run_id, item = parsed
        await self._autonomy_store.record_event(vin, routine, run_id, item)
        return True

    def start_background(self, loop: asyncio.AbstractEventLoop) -> threading.Thread:
        """Start the consumer loop in a daemon thread.

        Parameters
        ----------
        loop : asyncio.AbstractEventLoop
            The API event loop on which store coroutines are scheduled.

        Returns
        -------
        threading.Thread
        """
        self._loop = loop
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._run_loop, name="nomon-telemetry-consumer", daemon=True
        )
        thread.start()
        self._thread = thread
        logger.info(
            "Telemetry consumer started (broker=%s:%d topic=%s)",
            self.broker,
            self.port,
            self.topic,
        )
        return thread

    def stop(self) -> None:
        """Signal the consumer loop to stop and disconnect the client."""
        self._stop_event.set()
        try:
            self._client.disconnect()
        except Exception as exc:
            logger.debug("Error disconnecting telemetry consumer: %s", exc, exc_info=True)
        logger.info("Telemetry consumer stop requested.")

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _create_client(self) -> Any:
        """Create and configure a paho-mqtt client."""
        from nomothetic.telemetry import apply_mqtt_security

        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id="nomon-central-telemetry",
        )
        apply_mqtt_security(
            client,
            username=self.username,
            password=self.password,
            tls=self.tls,
            ca_cert=self.ca_cert,
            role="central consumer",
        )
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        return client

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        connect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        """Subscribe to the telemetry (and autonomy) topics on (re)connect."""
        if reason_code.is_failure:
            logger.warning("Telemetry consumer connect refused: %s", reason_code)
            return
        client.subscribe(self.topic, qos=self.qos)
        logger.info("Telemetry consumer subscribed to %s", self.topic)
        if self._autonomy_store is not None:
            client.subscribe(self.autonomy_topic, qos=self.qos)
            logger.info("Telemetry consumer subscribed to %s", self.autonomy_topic)

    def _ingest_for_topic(self, topic: str, payload: dict[str, Any]) -> Any:
        """Return the ingest coroutine matching *topic* (routing helper)."""
        if topic == self.autonomy_topic and self._autonomy_store is not None:
            return self.ingest_autonomy(payload)
        return self.ingest(payload)

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        """Decode a message and schedule ingestion on the API event loop."""
        loop = self._loop
        if loop is None:
            return
        try:
            payload = json.loads(message.payload)
        except (ValueError, TypeError) as exc:
            logger.warning("Telemetry consumer dropped malformed payload: %s", exc)
            return
        if not isinstance(payload, dict):
            return
        future = asyncio.run_coroutine_threadsafe(
            self._ingest_for_topic(message.topic, payload), loop
        )

        def _log_result(fut: "concurrent.futures.Future[bool]") -> None:
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Telemetry ingest failed: %s", exc)

        future.add_done_callback(_log_result)

    def _run_loop(self) -> None:
        """Background thread: connect with back-off, run the network loop."""
        backoff = _BACKOFF_BASE
        while not self._stop_event.is_set():
            try:
                self._client.connect(self.broker, self.port, keepalive=60)
                self._client.loop_start()
                backoff = _BACKOFF_BASE
                break
            except Exception as exc:
                logger.warning(
                    "Telemetry consumer connect failed (%s). Retrying in %.1fs.",
                    exc,
                    backoff,
                )
                self._stop_event.wait(timeout=backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP)

        # Block until stop() is signalled; paho's loop runs on its own thread.
        self._stop_event.wait()
        try:
            self._client.loop_stop()
        except Exception as exc:
            logger.debug("Error stopping telemetry consumer loop: %s", exc, exc_info=True)
        logger.info("Telemetry consumer stopped.")
