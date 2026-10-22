"""In-memory store for autonomy-routine status and lifecycle logs.

Part of the routine status/log feature (push model). The *brain* (autonomon)
reports its own lifecycle events — ``starting`` / ``running`` / ``stopping`` /
``error`` plus free-form ``log`` lines — to nomothetic, which stores them here
and serves them back via :mod:`nomothetic.routine_routes`.

This is **operational telemetry, not interpretation** (autonomon ADR-004):
nomothetic stores and returns exactly what the brain reports and derives a
coarse status from the event type. It performs no perception, fusion, or
modelling of its own.

The store is in-memory and bounded (a ring buffer per routine) — appropriate
for a Pi Zero 2W and for the device-local, single-run view the API exposes.
Events are segmented by ``run_id``: when a new run reports in, the previous
run's events are cleared so a query returns the *current* run's logs.
"""

import logging
import re
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Bounded per-routine history: enough to capture a startup failure and recent
# activity without unbounded growth on a memory-constrained device.
_MAX_EVENTS_DEFAULT = 200

# Routine names mirror the autonomon registry catalogue (e.g. ``explore``,
# ``follow-user``). Constrain to a safe, path-friendly charset.
_ROUTINE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Coarse status derived from the most recent lifecycle event type. Event types
# not listed here (e.g. ``log``) record an entry without changing the status.
_STATUS_BY_TYPE = {
    "starting": "starting",
    "running": "running",
    "stopping": "stopped",
    "error": "error",
}


class InvalidRoutineName(ValueError):
    """Raised when a routine name fails validation."""


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def validate_routine_name(name: str) -> str:
    """Validate and return a routine name.

    Parameters
    ----------
    name : str
        Candidate routine name.

    Returns
    -------
    str
        The validated name, unchanged.

    Raises
    ------
    InvalidRoutineName
        If *name* is not a non-empty string of up to 64 characters drawn from
        ``[A-Za-z0-9._-]`` (and not leading with ``.``/``-``).
    """
    if not isinstance(name, str) or not _ROUTINE_NAME_RE.match(name):
        raise InvalidRoutineName(
            "routine name must be 1-64 chars of letters, digits, '.', '_' or '-'"
        )
    return name


@dataclass
class RoutineEvent:
    """A single reported lifecycle event or log line for a routine."""

    timestamp: str
    type: str
    data: dict[str, Any]
    run_id: str | None = None
    device_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict of this event."""
        return {
            "timestamp": self.timestamp,
            "type": self.type,
            "data": self.data,
            "run_id": self.run_id,
            "device_id": self.device_id,
        }


@dataclass
class RoutineRecord:
    """Current status and bounded event history for one routine."""

    routine: str
    events: deque[RoutineEvent]
    status: str = "unknown"
    run_id: str | None = None
    started_at: str | None = None
    updated_at: str | None = None


class RoutineLogStore:
    """Thread-safe, in-memory store of routine status and recent events.

    Parameters
    ----------
    max_events : int, optional
        Per-routine ring-buffer capacity (default 200). The oldest events are
        discarded once the buffer is full.
    on_event : callable, optional
        ``(routine, event)`` observer invoked after every recorded event —
        both brain-reported (via the events endpoint) and supervisor-recorded
        (via :class:`~nomothetic.routine_manager.RoutineManager`) events pass
        through here. Called outside the store lock; exceptions are swallowed
        so an observer can never break recording (used for best-effort MQTT
        forwarding to central).
    """

    def __init__(
        self,
        max_events: int = _MAX_EVENTS_DEFAULT,
        on_event: Callable[[str, RoutineEvent], Any] | None = None,
    ) -> None:
        self._max_events = max_events
        self._records: dict[str, RoutineRecord] = {}
        self._lock = threading.Lock()
        self._on_event = on_event

    def record(
        self,
        routine: str,
        event_type: str,
        data: dict[str, Any] | None = None,
        run_id: str | None = None,
        device_id: str | None = None,
        timestamp: str | None = None,
    ) -> RoutineEvent:
        """Append a lifecycle event for *routine* and update its status.

        A new ``run_id`` (different from the routine's current run) starts a
        fresh run: the event buffer is cleared and ``started_at`` is reset, so a
        subsequent query returns only the current run's logs.

        Parameters
        ----------
        routine : str
            Routine name (validated).
        event_type : str
            Lifecycle event type (``starting``/``running``/``stopping``/
            ``error``) or a free-form type such as ``log``.
        data : dict, optional
            Event payload (the autonomon NDJSON ``data`` object).
        run_id : str, optional
            Per-run identifier; segments runs of the same routine.
        device_id : str, optional
            Reporting device identifier (forward-compat for fleet aggregation).
        timestamp : str, optional
            Event time (ISO 8601). Defaults to now.

        Returns
        -------
        RoutineEvent
            The stored event.

        Raises
        ------
        InvalidRoutineName
            If *routine* is not a valid routine name.
        """
        validate_routine_name(routine)
        ts = timestamp or _now_iso()
        event = RoutineEvent(
            timestamp=ts,
            type=event_type,
            data=dict(data or {}),
            run_id=run_id,
            device_id=device_id,
        )

        with self._lock:
            record = self._records.get(routine)
            if record is None:
                record = RoutineRecord(
                    routine=routine,
                    events=deque(maxlen=self._max_events),
                )
                self._records[routine] = record

            # A new run resets the per-run view.
            if run_id is not None and run_id != record.run_id:
                record.run_id = run_id
                record.started_at = ts
                record.events.clear()

            record.events.append(event)
            record.updated_at = ts
            if record.started_at is None:
                record.started_at = ts

            status = _STATUS_BY_TYPE.get(event_type)
            if status is not None:
                record.status = status

        if self._on_event is not None:
            try:
                self._on_event(routine, event)
            except Exception as exc:  # noqa: BLE001 — observers are best-effort
                logger.debug("routine event observer failed: %s", exc, exc_info=True)

        return event

    def get(self, routine: str, limit: int | None = None) -> dict[str, Any] | None:
        """Return a status + log snapshot for *routine*, or ``None`` if unknown.

        Parameters
        ----------
        routine : str
            Routine name.
        limit : int, optional
            If given, return only the most recent *limit* events.

        Returns
        -------
        dict or None
            Snapshot dict (``routine``, ``status``, ``run_id``, ``started_at``,
            ``updated_at``, ``event_count``, ``events``) or ``None`` if the
            routine has never reported.
        """
        with self._lock:
            record = self._records.get(routine)
            if record is None:
                return None
            return self._snapshot(record, limit)

    def list_routines(self) -> list[dict[str, Any]]:
        """Return a status summary (no events) for every known routine."""
        with self._lock:
            return [self._summary(record) for record in self._records.values()]

    # -- internal helpers (call with the lock held) -------------------------

    @staticmethod
    def _summary(record: RoutineRecord) -> dict[str, Any]:
        return {
            "routine": record.routine,
            "status": record.status,
            "run_id": record.run_id,
            "started_at": record.started_at,
            "updated_at": record.updated_at,
            "event_count": len(record.events),
        }

    @classmethod
    def _snapshot(cls, record: RoutineRecord, limit: int | None) -> dict[str, Any]:
        events = list(record.events)
        if limit is not None:
            # Guard the limit==0 case: events[-0:] would return everything.
            events = events[-limit:] if limit > 0 else []
        snapshot = cls._summary(record)
        snapshot["events"] = [event.to_dict() for event in events]
        return snapshot
