"""Autonomy run/event persistence layer with in-memory and ArcadeDB backends.

Protocol-based store abstraction for fleet-wide autonomy history (autonomon
Phase 7).  Mirrors the :mod:`nomothetic.telemetry_store` pattern (Pydantic
model -> ``Protocol`` -> ``InMemory*`` / ``Sql*``).  Runs are linked to a
:class:`Vehicle` vertex by the ``PerformedBy`` edge and events to their run by
the ``PartOf`` edge, both defined in the nomographic central V4 schema.

This is **operational telemetry, not interpretation** (autonomon ADR-004): the
store persists exactly what the brain reported — the run's coarse status is
derived from the lifecycle event type, the same mapping the device-local
:class:`~nomothetic.routine_log_store.RoutineLogStore` uses.
"""

import json
import logging
from typing import TYPE_CHECKING, Any, runtime_checkable

from pydantic import BaseModel, Field
from typing_extensions import Protocol

from nomothetic.db_utils import db_datetime_to_iso, to_db_datetime

if TYPE_CHECKING:
    from nomothetic.db import DatabaseClient

logger = logging.getLogger(__name__)

# Coarse run status derived from the most recent lifecycle event type.  Event
# types not listed here (e.g. ``log``) record an event without changing the
# run's status.  Mirrors ``routine_log_store._STATUS_BY_TYPE``.
_STATUS_BY_TYPE = {
    "starting": "starting",
    "running": "running",
    "stopping": "stopped",
    "error": "error",
}

# Event types that mark the end of a run (set ``ended_at``).
_TERMINAL_TYPES = frozenset({"stopping", "error"})

# Bounds for the in-memory backend (a dev/test convenience, not fleet scale).
_MAX_RUNS_PER_VIN = 100
_MAX_EVENTS_PER_RUN = 500


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class AutonomyEventItem(BaseModel):
    """A single lifecycle event reported during an autonomy run.

    Mirrors the ``AutonomyEvent`` vertex type (nomographic central V4);
    ``data`` is stored there as a verbatim JSON string (``data_json``).
    """

    event_type: str
    data: dict[str, Any] = Field(default_factory=dict)
    recorded_at: str


class AutonomyRunItem(BaseModel):
    """One run of an autonomy routine on a device.

    Mirrors the ``AutonomyRun`` vertex type (nomographic central V4).
    ``event_count`` is derived (``in('PartOf').size()`` on the SQL backend).
    """

    run_id: str
    routine: str
    status: str
    started_at: str
    updated_at: str
    ended_at: str | None = None
    event_count: int = 0


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AutonomyStore(Protocol):
    """Abstract interface for autonomy run/event persistence."""

    async def record_event(
        self, vin: str, routine: str, run_id: str, item: AutonomyEventItem
    ) -> None:
        """Persist one event, creating/updating its run as a side effect.

        Parameters
        ----------
        vin : str
            Vehicle identification number the run executed on.
        routine : str
            Autonomy routine name (e.g. ``explore``).
        run_id : str
            Per-run identifier assigned by the brain.
        item : AutonomyEventItem
            The event to store.
        """
        ...  # pragma: no cover

    async def get_runs(
        self, vin: str, limit: int = 50, since: str | None = None
    ) -> list[AutonomyRunItem]:
        """Return runs for a device, most recently started first.

        Parameters
        ----------
        vin : str
            Vehicle identification number.
        limit : int, optional
            Maximum number of runs to return (default 50).
        since : str, optional
            ISO-8601 lower bound (inclusive) on ``started_at``.

        Returns
        -------
        list[AutonomyRunItem]
        """
        ...  # pragma: no cover

    async def get_events(self, vin: str, run_id: str, limit: int = 200) -> list[AutonomyEventItem]:
        """Return one run's events in chronological order.

        Parameters
        ----------
        vin : str
            Vehicle identification number (scopes the run lookup).
        run_id : str
            Per-run identifier.
        limit : int, optional
            Maximum number of events to return (default 200).

        Returns
        -------
        list[AutonomyEventItem]
            Empty when the run is unknown for this device.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class _RunRecord:
    """Mutable in-memory run state plus its bounded event list."""

    __slots__ = ("run", "events")

    def __init__(self, run: AutonomyRunItem) -> None:
        self.run = run
        self.events: list[AutonomyEventItem] = []


class InMemoryAutonomyStore:
    """Dict-backed autonomy store for testing and development.

    Keeps a bounded per-VIN dict of runs (oldest-started dropped past the cap)
    and a bounded per-run event list.

    Parameters
    ----------
    max_runs_per_vin : int, optional
        Cap on retained runs per device (default 100).
    max_events_per_run : int, optional
        Cap on retained events per run (default 500); oldest are dropped.
    """

    def __init__(
        self,
        max_runs_per_vin: int = _MAX_RUNS_PER_VIN,
        max_events_per_run: int = _MAX_EVENTS_PER_RUN,
    ) -> None:
        self._runs: dict[str, dict[str, _RunRecord]] = {}
        self._max_runs_per_vin = max_runs_per_vin
        self._max_events_per_run = max_events_per_run

    async def record_event(
        self, vin: str, routine: str, run_id: str, item: AutonomyEventItem
    ) -> None:
        """Append an event, creating or updating its run record."""
        bucket = self._runs.setdefault(vin, {})
        record = bucket.get(run_id)
        status = _STATUS_BY_TYPE.get(item.event_type)
        if record is None:
            record = _RunRecord(
                AutonomyRunItem(
                    run_id=run_id,
                    routine=routine,
                    status=status or "unknown",
                    started_at=item.recorded_at,
                    updated_at=item.recorded_at,
                )
            )
            bucket[run_id] = record
            if len(bucket) > self._max_runs_per_vin:
                oldest = min(bucket.values(), key=lambda r: r.run.started_at)
                del bucket[oldest.run.run_id]

        record.events.append(item)
        if len(record.events) > self._max_events_per_run:
            del record.events[0 : len(record.events) - self._max_events_per_run]

        record.run.updated_at = item.recorded_at
        if status is not None:
            record.run.status = status
        if item.event_type in _TERMINAL_TYPES:
            record.run.ended_at = item.recorded_at
        record.run.event_count = len(record.events)

    async def get_runs(
        self, vin: str, limit: int = 50, since: str | None = None
    ) -> list[AutonomyRunItem]:
        """Return up to ``limit`` runs for the device, newest-started first."""
        records = list(self._runs.get(vin, {}).values())
        runs = [r.run for r in records]
        if since is not None:
            runs = [r for r in runs if r.started_at >= since]
        # Newest first; ISO-8601 strings sort chronologically.
        ordered = sorted(runs, key=lambda r: r.started_at, reverse=True)
        return [r.model_copy() for r in ordered[: max(limit, 0)]]

    async def get_events(self, vin: str, run_id: str, limit: int = 200) -> list[AutonomyEventItem]:
        """Return the run's events oldest-first (chronological log order)."""
        record = self._runs.get(vin, {}).get(run_id)
        if record is None:
            return []
        return list(record.events[: max(limit, 0)])


# ---------------------------------------------------------------------------
# SQL implementation
# ---------------------------------------------------------------------------


class SqlAutonomyStore:
    """ArcadeDB-backed autonomy store using parameterized SQL queries.

    Runs are ``AutonomyRun`` vertices linked to their ``Vehicle`` by a
    ``PerformedBy`` edge; events are ``AutonomyEvent`` vertices linked to their
    run by a ``PartOf`` edge (nomographic central V4).  ``AutonomyRun.vin`` is
    denormalised so run/event queries are ownership-scoped without a traversal.

    Parameters
    ----------
    db : DatabaseClient
        An initialised database client.
    """

    def __init__(self, db: "DatabaseClient") -> None:
        self._db = db

    async def record_event(
        self, vin: str, routine: str, run_id: str, item: AutonomyEventItem
    ) -> None:
        """Insert an AutonomyEvent, creating/updating its AutonomyRun first."""
        status = _STATUS_BY_TYPE.get(item.event_type)
        # DATETIME columns need the ArcadeDB format, not a raw ISO offset string.
        ts = to_db_datetime(item.recorded_at)
        existing = await self._db.execute_sql(
            "SELECT run_id FROM AutonomyRun WHERE run_id = :run_id AND vin = :vin",
            {"run_id": run_id, "vin": vin},
        )
        if not existing:
            # The inserted run is the edge's source vertex, the owning Vehicle
            # (matched by vin) the target — same shape as SqlTelemetryStore.
            create = (
                "CREATE EDGE PerformedBy FROM ("
                "INSERT INTO AutonomyRun SET run_id = :run_id, vin = :vin,"
                " routine = :routine, status = :status, started_at = :ts,"
                " updated_at = :ts"
                + (", ended_at = :ts" if self._is_terminal(item) else "")
                + ") TO (SELECT FROM Vehicle WHERE vin = :vin)"
            )
            await self._db.execute_sql(
                create,
                {
                    "run_id": run_id,
                    "vin": vin,
                    "routine": routine,
                    "status": status or "unknown",
                    "ts": ts,
                },
            )
        else:
            set_clauses = ["updated_at = :ts"]
            params: dict[str, object] = {"run_id": run_id, "vin": vin, "ts": ts}
            if status is not None:
                set_clauses.append("status = :status")
                params["status"] = status
            if self._is_terminal(item):
                set_clauses.append("ended_at = :ts")
            await self._db.execute_sql(
                "UPDATE AutonomyRun SET "
                + ", ".join(set_clauses)
                + " WHERE run_id = :run_id AND vin = :vin",
                params,
            )

        await self._db.execute_sql(
            "CREATE EDGE PartOf FROM ("
            "INSERT INTO AutonomyEvent SET event_type = :event_type,"
            " data_json = :data_json, recorded_at = :recorded_at"
            ") TO (SELECT FROM AutonomyRun WHERE run_id = :run_id AND vin = :vin)",
            {
                "event_type": item.event_type,
                "data_json": json.dumps(item.data),
                "recorded_at": ts,
                "run_id": run_id,
                "vin": vin,
            },
        )

    async def get_runs(
        self, vin: str, limit: int = 50, since: str | None = None
    ) -> list[AutonomyRunItem]:
        """Return runs for the device, newest-started first."""
        params: dict[str, object] = {"vin": vin, "limit": max(limit, 0)}
        since_clause = ""
        if since is not None:
            since_clause = " AND started_at >= :since"
            params["since"] = to_db_datetime(since)
        query = (
            "SELECT run_id, routine, status, started_at, updated_at, ended_at,"
            " in('PartOf').size() AS event_count"
            " FROM AutonomyRun WHERE vin = :vin"
            + since_clause
            + " ORDER BY started_at DESC LIMIT :limit"
        )
        rows = await self._db.execute_sql(query, params)
        return [_row_to_run(row) for row in rows]

    async def get_events(self, vin: str, run_id: str, limit: int = 200) -> list[AutonomyEventItem]:
        """Traverse AutonomyRun.in('PartOf'), oldest-first (log order)."""
        query = (
            "SELECT event_type, data_json, recorded_at FROM ("
            "SELECT expand(in('PartOf')) FROM AutonomyRun"
            " WHERE run_id = :run_id AND vin = :vin"
            ") ORDER BY recorded_at ASC LIMIT :limit"
        )
        rows = await self._db.execute_sql(
            query, {"run_id": run_id, "vin": vin, "limit": max(limit, 0)}
        )
        return [_row_to_event(row) for row in rows]

    @staticmethod
    def _is_terminal(item: AutonomyEventItem) -> bool:
        """Return whether this event type ends a run."""
        return item.event_type in _TERMINAL_TYPES


def _row_to_run(row: dict) -> AutonomyRunItem:
    """Coerce a DB row to an :class:`AutonomyRunItem` with safe defaults.

    DATETIME columns come back in ArcadeDB format and are converted to ISO-8601
    for the API contract (the in-memory store carries ISO strings throughout).
    """
    return AutonomyRunItem(
        run_id=str(row.get("run_id", "")),
        routine=str(row.get("routine", "")),
        status=str(row.get("status", "unknown")),
        started_at=db_datetime_to_iso(row.get("started_at")) or "",
        updated_at=db_datetime_to_iso(row.get("updated_at")) or "",
        ended_at=db_datetime_to_iso(row.get("ended_at")),
        event_count=int(row.get("event_count", 0) or 0),
    )


def _row_to_event(row: dict) -> AutonomyEventItem:
    """Coerce a DB row to an :class:`AutonomyEventItem`; bad JSON becomes {}."""
    raw = row.get("data_json")
    data: dict[str, Any] = {}
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            logger.debug("Undecodable AutonomyEvent.data_json; returning empty data")
    return AutonomyEventItem(
        event_type=str(row.get("event_type", "")),
        data=data,
        recorded_at=db_datetime_to_iso(row.get("recorded_at")) or "",
    )
