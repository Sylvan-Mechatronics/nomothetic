"""Telemetry reading persistence layer with in-memory and ArcadeDB backends.

Protocol-based store abstraction for fleet telemetry history.  Mirrors the
:mod:`nomothetic.fleet_store` pattern (Pydantic model -> ``Protocol`` ->
``InMemory*`` / ``Sql*``).  Readings are linked to a :class:`Vehicle` vertex by
the ``ReadFrom`` edge defined in the nomographic central V1 schema; the store is
the read/write counterpart that the central API and MQTT consumer use.
"""

import logging
from typing import TYPE_CHECKING, runtime_checkable

from pydantic import BaseModel
from typing_extensions import Protocol

from nomothetic.db_utils import db_datetime_to_iso, to_db_datetime

if TYPE_CHECKING:
    from nomothetic.db import DatabaseClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class TelemetryReadingItem(BaseModel):
    """A single telemetry snapshot from a device.

    Mirrors the ``TelemetryReading`` vertex type (nomographic central V1):
    ``battery_voltage``, ``cpu_temp_c``, ``uptime_seconds``, ``recorded_at``.
    """

    battery_voltage: float
    cpu_temp_c: float
    uptime_seconds: int
    recorded_at: str


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class TelemetryStore(Protocol):
    """Abstract interface for telemetry reading persistence."""

    async def record_reading(self, vin: str, item: TelemetryReadingItem) -> None:
        """Persist one reading and link it to the device's Vehicle vertex.

        Parameters
        ----------
        vin : str
            Vehicle identification number the reading was read from.
        item : TelemetryReadingItem
            The reading to store.
        """
        ...  # pragma: no cover

    async def get_history(
        self, vin: str, limit: int = 100, since: str | None = None
    ) -> list[TelemetryReadingItem]:
        """Return readings for a device, most recent first.

        Parameters
        ----------
        vin : str
            Vehicle identification number.
        limit : int, optional
            Maximum number of readings to return (default 100).
        since : str, optional
            ISO-8601 lower bound (inclusive) on ``recorded_at``; when given,
            only readings at or after this time are returned.

        Returns
        -------
        list[TelemetryReadingItem]
        """
        ...  # pragma: no cover

    async def get_latest(self, vin: str) -> TelemetryReadingItem | None:
        """Return the most recent reading for a device, or ``None``.

        Parameters
        ----------
        vin : str
            Vehicle identification number.

        Returns
        -------
        TelemetryReadingItem or None
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryTelemetryStore:
    """Dict-backed telemetry store for testing and development.

    Keeps a bounded per-VIN list of readings (newest appended last).

    Parameters
    ----------
    max_per_vin : int, optional
        Cap on retained readings per device (default 1000); oldest are dropped.
    """

    def __init__(self, max_per_vin: int = 1000) -> None:
        self._readings: dict[str, list[TelemetryReadingItem]] = {}
        self._max_per_vin = max_per_vin

    async def record_reading(self, vin: str, item: TelemetryReadingItem) -> None:
        """Append a reading, dropping the oldest past the per-VIN cap."""
        bucket = self._readings.setdefault(vin, [])
        bucket.append(item)
        if len(bucket) > self._max_per_vin:
            del bucket[0 : len(bucket) - self._max_per_vin]

    async def get_history(
        self, vin: str, limit: int = 100, since: str | None = None
    ) -> list[TelemetryReadingItem]:
        """Return up to ``limit`` readings for the device, newest first."""
        bucket = self._readings.get(vin, [])
        rows = bucket
        if since is not None:
            rows = [r for r in rows if r.recorded_at >= since]
        # Newest first; ISO-8601 strings sort chronologically.
        ordered = sorted(rows, key=lambda r: r.recorded_at, reverse=True)
        return ordered[: max(limit, 0)]

    async def get_latest(self, vin: str) -> TelemetryReadingItem | None:
        """Return the most recent reading for the device, or ``None``."""
        history = await self.get_history(vin, limit=1)
        return history[0] if history else None


# ---------------------------------------------------------------------------
# SQL implementation
# ---------------------------------------------------------------------------


class SqlTelemetryStore:
    """ArcadeDB-backed telemetry store using parameterized SQL queries.

    Readings are stored as ``TelemetryReading`` vertices linked to their
    ``Vehicle`` by a ``ReadFrom`` edge (``TelemetryReading --ReadFrom--> Vehicle``,
    per nomographic central V1).

    Parameters
    ----------
    db : DatabaseClient
        An initialised database client.
    """

    def __init__(self, db: "DatabaseClient") -> None:
        self._db = db

    async def record_reading(self, vin: str, item: TelemetryReadingItem) -> None:
        """Insert a TelemetryReading and link it to the Vehicle via ReadFrom."""
        # Single statement: the inserted reading is the edge's source vertex,
        # the owning Vehicle (matched by vin) is the target.
        query = (
            "CREATE EDGE ReadFrom FROM ("
            "INSERT INTO TelemetryReading SET battery_voltage = :battery_voltage,"
            " cpu_temp_c = :cpu_temp_c, uptime_seconds = :uptime_seconds,"
            " recorded_at = :recorded_at"
            ") TO (SELECT FROM Vehicle WHERE vin = :vin)"
        )
        await self._db.execute_sql(
            query,
            {
                "battery_voltage": item.battery_voltage,
                "cpu_temp_c": item.cpu_temp_c,
                "uptime_seconds": item.uptime_seconds,
                # recorded_at is a DATETIME column — format for ArcadeDB (a raw
                # ISO-8601 offset string would be stored as null).
                "recorded_at": to_db_datetime(item.recorded_at),
                "vin": vin,
            },
        )

    async def get_history(
        self, vin: str, limit: int = 100, since: str | None = None
    ) -> list[TelemetryReadingItem]:
        """Traverse Vehicle.in('ReadFrom') and return readings newest first."""
        params: dict[str, object] = {"vin": vin, "limit": max(limit, 0)}
        since_clause = ""
        if since is not None:
            since_clause = " WHERE recorded_at >= :since"
            params["since"] = to_db_datetime(since)
        query = (
            "SELECT battery_voltage, cpu_temp_c, uptime_seconds, recorded_at FROM ("
            "SELECT expand(in('ReadFrom')) FROM Vehicle WHERE vin = :vin"
            ")" + since_clause + " ORDER BY recorded_at DESC LIMIT :limit"
        )
        rows = await self._db.execute_sql(query, params)
        return [_row_to_item(row) for row in rows]

    async def get_latest(self, vin: str) -> TelemetryReadingItem | None:
        """Return the most recent reading for the device, or ``None``."""
        history = await self.get_history(vin, limit=1)
        return history[0] if history else None


def _row_to_item(row: dict) -> TelemetryReadingItem:
    """Coerce a DB row to a :class:`TelemetryReadingItem` with safe defaults.

    ``recorded_at`` comes back in ArcadeDB DATETIME format and is converted to
    ISO-8601 for the API contract (the in-memory store carries ISO throughout).
    """
    return TelemetryReadingItem(
        battery_voltage=float(row.get("battery_voltage", 0.0) or 0.0),
        cpu_temp_c=float(row.get("cpu_temp_c", 0.0) or 0.0),
        uptime_seconds=int(row.get("uptime_seconds", 0) or 0),
        recorded_at=db_datetime_to_iso(row.get("recorded_at")) or "",
    )
