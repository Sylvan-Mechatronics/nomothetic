"""Tests for shared DB helpers (count coercion + datetime formatting)."""

from datetime import UTC, datetime

import pytest

from nomothetic.db_utils import coerce_count, db_datetime_to_iso, to_db_datetime


class TestCoerceCount:
    def test_variants(self):
        assert coerce_count([]) == 0
        assert coerce_count([5]) == 5
        assert coerce_count([5.0]) == 5
        assert coerce_count([{"count": 7}]) == 7
        assert coerce_count([{"count": 7.0}]) == 7
        assert coerce_count([{"nope": 1}]) == 0


class TestToDbDatetime:
    def test_iso_with_offset_becomes_arcadedb_format(self):
        assert to_db_datetime("2026-07-05T12:34:56+00:00") == "2026-07-05 12:34:56.000"

    def test_iso_with_z_offset(self):
        # fromisoformat handles ...+00:00; a trailing Z is normalised by callers,
        # but an explicit offset other than UTC is converted to UTC.
        assert to_db_datetime("2026-07-05T14:34:56+02:00") == "2026-07-05 12:34:56.000"

    def test_subsecond_precision_to_milliseconds(self):
        # Sub-millisecond precision is dropped (rounded down).
        assert to_db_datetime("2026-07-05T12:34:56.789123+00:00") == "2026-07-05 12:34:56.789"
        assert to_db_datetime("2026-07-05T12:34:56.123456+00:00") == "2026-07-05 12:34:56.123"

    def test_naive_iso_assumed_utc(self):
        assert to_db_datetime("2026-07-05T12:34:56") == "2026-07-05 12:34:56.000"

    def test_accepts_datetime_object(self):
        dt = datetime(2026, 7, 5, 12, 34, 56, tzinfo=UTC)
        assert to_db_datetime(dt) == "2026-07-05 12:34:56.000"

    def test_datetime_with_microseconds(self):
        dt = datetime(2026, 7, 5, 12, 34, 56, 789123, tzinfo=UTC)
        assert to_db_datetime(dt) == "2026-07-05 12:34:56.789"

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError):
            to_db_datetime("not a date")


class TestDbDatetimeToIso:
    def test_arcadedb_millisecond_format_to_iso(self):
        # New format: millisecond precision.
        assert db_datetime_to_iso("2026-07-05 12:34:56.789") == "2026-07-05T12:34:56.789000+00:00"

    def test_arcadedb_second_format_to_iso(self):
        # Old format: second precision (compatibility).
        assert db_datetime_to_iso("2026-07-05 12:34:56") == "2026-07-05T12:34:56+00:00"

    def test_iso_input_passes_through_as_utc(self):
        # Read helper is lenient: an already-ISO value round-trips unchanged.
        assert db_datetime_to_iso("2026-07-05T12:34:56+00:00") == "2026-07-05T12:34:56+00:00"

    def test_none_and_empty_yield_none(self):
        assert db_datetime_to_iso(None) is None
        assert db_datetime_to_iso("") is None

    def test_datetime_object_input(self):
        dt = datetime(2026, 7, 5, 12, 34, 56, tzinfo=UTC)
        assert db_datetime_to_iso(dt) == "2026-07-05T12:34:56+00:00"

    def test_datetime_with_microseconds(self):
        dt = datetime(2026, 7, 5, 12, 34, 56, 789000, tzinfo=UTC)
        assert db_datetime_to_iso(dt) == "2026-07-05T12:34:56.789000+00:00"

    def test_round_trip_is_stable(self):
        iso = "2026-07-05T12:34:56+00:00"
        assert db_datetime_to_iso(to_db_datetime(iso)) == iso
