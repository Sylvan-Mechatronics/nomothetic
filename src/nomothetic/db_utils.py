"""Shared database utility functions.

Small helpers reused across multiple store modules (UserStore,
TokenStore, FleetStore).
"""

from datetime import UTC, datetime
from typing import Any

# ArcadeDB's ``dateTimeFormat`` (millisecond precision, no timezone). A
# DATETIME column silently stores ``null`` for any value that does not match
# this pattern — notably ISO-8601 strings with a ``+00:00`` offset or ``Z``.
# So values crossing into a DATETIME column must be formatted with this pattern;
# values read back are parsed from it. This mirrors the long-standing convention
# in ``token_store``/``auth`` (``strftime`` on write). Timestamps are treated as
# UTC on both sides, so the round-trip is lossless apart from sub-millisecond
# precision (dropped by the millisecond-precision DB format).
_DB_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
# Millisecond-precision format (ArcadeDB 2+; per V5 migration).
# Python's strftime uses %f for microseconds; we format and then divide.
_DB_DATETIME_FORMAT_SECONDS = "%Y-%m-%d %H:%M:%S"
# Try parsing both old (second-precision) and new (millisecond-precision) formats.
_DB_DATETIME_FORMATS = ["%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"]


def coerce_count(rows: list[Any]) -> int:
    """Coerce an ArcadeDB ``SELECT count(*)`` result to a plain int.

    ArcadeDB may return the count as an ``int``, ``float``, or a
    ``{"count": <number>}`` dict depending on the query shape and driver
    version.  This helper normalises all variants to ``int``.

    Parameters
    ----------
    rows : list[Any]
        Result rows from :meth:`DatabaseClient.execute_sql`.

    Returns
    -------
    int
        Coerced count, or ``0`` when *rows* is empty or unparseable.
    """
    if not rows:
        return 0
    first = rows[0]
    if isinstance(first, int):
        return first
    if isinstance(first, float):
        return int(first)
    if isinstance(first, dict):
        val = first.get("count", 0)
        if isinstance(val, (int, float)):
            return int(val)
    return 0


def to_db_datetime(value: str | datetime) -> str:
    """Format an ISO-8601 string or ``datetime`` for an ArcadeDB DATETIME column.

    The result is a UTC wall-clock timestamp in ArcadeDB's
    ``yyyy-MM-dd HH:mm:ss.SSS`` format (millisecond precision). Aware
    datetimes/strings are converted to UTC first; naive ones are assumed to
    already be UTC. Sub-millisecond precision is dropped (the DB format is
    millisecond-precision).

    Parameters
    ----------
    value : str or datetime
        An ISO-8601 timestamp string (e.g. ``"2026-07-05T12:34:56+00:00"``) or a
        :class:`datetime.datetime`.

    Returns
    -------
    str
        ``"YYYY-MM-DD HH:MM:SS.sss"`` suitable for binding to a DATETIME column.

    Raises
    ------
    ValueError
        If *value* is a string that cannot be parsed as ISO-8601.
    """
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    # Format seconds, then append milliseconds (microseconds // 1000).
    ms = dt.microsecond // 1000
    return dt.strftime(_DB_DATETIME_FORMAT_SECONDS) + f".{ms:03d}"


def db_datetime_to_iso(value: Any) -> str | None:
    """Convert a value read from an ArcadeDB DATETIME column back to ISO-8601.

    Inverse of :func:`to_db_datetime`. The stored wall-clock is interpreted as
    UTC, so the returned string carries a ``+00:00`` offset. Handles both
    old (second-precision) and new (millisecond-precision) ArcadeDB formats.

    Parameters
    ----------
    value : Any
        The raw value from a result row (an ArcadeDB string in
        ``YYYY-MM-DD HH:MM:SS.sss`` or ``YYYY-MM-DD HH:MM:SS`` format, or a
        ``datetime``). ``None`` or an empty string yields ``None`` (for nullable
        columns).

    Returns
    -------
    str or None
        An ISO-8601 UTC string, or ``None`` when there is no value.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value)
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            # Try both old (second) and new (millisecond) formats.
            parse_error: ValueError | None = None
            dt = None
            for fmt in _DB_DATETIME_FORMATS:
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError as e:
                    parse_error = e
            if dt is None:
                raise ValueError(f"Could not parse datetime: {text}") from parse_error
    assert isinstance(dt, datetime)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()
