"""Autonomy-routine status/log endpoints (push model).

The *brain* (autonomon) reports its own lifecycle events to these endpoints;
nomothetic stores them in a :class:`~nomothetic.routine_log_store.RoutineLogStore`
and serves them back. This keeps cognition in autonomon (ADR-004) while giving
operators a device-local view of *what a routine is doing and why it stopped* —
the failure case that is otherwise only visible on the plugin's stdout.

These are **autonomy routines** (host-side cognitive pipelines), deliberately
distinct from the firmware **HAT routines** under ``/api/routine/*`` (singular).
This router owns ``/api/routines/*`` (plural).

Endpoints (all inherit the device router's auth — a device JWT in device mode):

* ``POST /api/routines/{routine}/events`` — append a reported lifecycle event.
* ``GET  /api/routines/{routine}/logs``   — current status + recent events.
* ``GET  /api/routines``                  — status summary of every routine.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, Field

from nomothetic.rate_limit import events_rate_limit
from nomothetic.routine_log_store import InvalidRoutineName, RoutineLogStore

logger = logging.getLogger(__name__)

# Upper bound on how many events a single /logs query may return.
_MAX_LOG_LIMIT = 500


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RoutineEventRequest(BaseModel):
    """One reported lifecycle event for a routine (autonomon NDJSON shape)."""

    type: str = Field(..., min_length=1, max_length=32, description="Lifecycle event type")
    data: dict[str, Any] = Field(default_factory=dict, description="Event payload")
    run_id: Optional[str] = Field(default=None, max_length=64, description="Per-run identifier")
    device_id: Optional[str] = Field(default=None, max_length=128)
    timestamp: Optional[str] = Field(default=None, description="Event time (ISO 8601)")


class RoutineEventAck(BaseModel):
    """Acknowledgement of a recorded event, with the resulting status."""

    routine: str
    status: str
    run_id: Optional[str]
    timestamp: str


class RoutineLogEvent(BaseModel):
    """A single stored lifecycle event."""

    timestamp: str
    type: str
    data: dict[str, Any]
    run_id: Optional[str] = None
    device_id: Optional[str] = None


class RoutineLogResponse(BaseModel):
    """Current status and recent event history for one routine."""

    routine: str
    status: str
    run_id: Optional[str]
    started_at: Optional[str]
    updated_at: Optional[str]
    event_count: int
    events: list[RoutineLogEvent]
    timestamp: str


class RoutineSummary(BaseModel):
    """Status summary for one routine (no events)."""

    routine: str
    status: str
    run_id: Optional[str]
    started_at: Optional[str]
    updated_at: Optional[str]
    event_count: int


class RoutineListResponse(BaseModel):
    """Status summary of every known routine."""

    routines: list[RoutineSummary]
    timestamp: str


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_routine_router() -> APIRouter:
    """Build the autonomy-routine status/log router.

    The :class:`~nomothetic.routine_log_store.RoutineLogStore` is read from
    ``request.app.state.routine_log_store`` at call time, so test fixtures can
    inject a fresh instance.

    Returns
    -------
    APIRouter
        Router with ``/api/routines`` (list), ``/api/routines/{routine}/logs``,
        and ``/api/routines/{routine}/events`` endpoints.
    """
    router = APIRouter(prefix="/api/routines", tags=["Routines"])

    def _store(request: Request) -> RoutineLogStore:
        store: Optional[RoutineLogStore] = getattr(request.app.state, "routine_log_store", None)
        if store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Routine log store not configured",
            )
        return store

    @router.post(
        "/{routine}/events",
        response_model=RoutineEventAck,
        status_code=200,
        dependencies=[Depends(events_rate_limit)],
    )
    async def report_event(
        body: RoutineEventRequest,
        request: Request,
        routine: str = Path(..., min_length=1, max_length=64),
    ):
        """Append a reported lifecycle event for *routine*.

        Raises
        ------
        HTTPException
            400 if the routine name is invalid.
        """
        store = _store(request)
        try:
            store.record(
                routine,
                body.type,
                data=body.data,
                run_id=body.run_id,
                device_id=body.device_id,
                timestamp=body.timestamp,
            )
        except InvalidRoutineName as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        snapshot = store.get(routine, limit=0)
        assert snapshot is not None  # just recorded
        logger.debug("routine %r event %r recorded", routine, body.type)
        return RoutineEventAck(
            routine=routine,
            status=snapshot["status"],
            run_id=snapshot["run_id"],
            timestamp=_now(),
        )

    @router.get("/{routine}/logs", response_model=RoutineLogResponse)
    async def get_logs(
        request: Request,
        routine: str = Path(..., min_length=1, max_length=64),
        limit: Optional[int] = Query(default=None, ge=1, le=_MAX_LOG_LIMIT),
    ):
        """Return the current status and recent events for *routine*.

        Raises
        ------
        HTTPException
            404 if the routine has never reported any events.
        """
        store = _store(request)
        snapshot = store.get(routine, limit=limit)
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"routine {routine!r} has no reported activity",
            )
        return RoutineLogResponse(timestamp=_now(), **snapshot)

    @router.get("", response_model=RoutineListResponse)
    async def list_routines(request: Request):
        """Return a status summary of every routine that has reported."""
        store = _store(request)
        summaries = [RoutineSummary(**summary) for summary in store.list_routines()]
        return RoutineListResponse(routines=summaries, timestamp=_now())

    return router
