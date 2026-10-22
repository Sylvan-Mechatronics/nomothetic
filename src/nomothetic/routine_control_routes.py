"""Autonomy-routine lifecycle control endpoints (start / stop / stop-all / heartbeat).

These let an operator launch, renew, and stop autonomy routines on the device.
They are the *control* counterpart to the status/log sink in
:mod:`nomothetic.routine_routes`: control here starts and stops the plugin process
via a :class:`~nomothetic.routine_manager.RoutineManager`; that module's endpoints
receive and serve the lifecycle events the running plugin reports.

Launching the plugin is process supervision, not cognition — all autonomy logic
stays in the launched ``autonomon`` process (ADR-004). A routine runs under a
*renewable lease*: the operator sends heartbeats to keep it alive, and it is
auto-stopped if they stop arriving (contact lost) — so a routine continues while
the user maintains contact but cannot run forever once they don't.

This router shares the ``/api/routines`` prefix and inherits the device router's
auth. Endpoints:

* ``GET  /api/routines/available`` — list the routines this device can launch.
* ``POST /api/routines/start``     — launch a routine from a JSON payload.
* ``POST /api/routines/heartbeat`` — renew a running routine's lease (keep-alive).
* ``POST /api/routines/stop``      — stop one running routine.
* ``POST /api/routines/stop-all``  — stop every running routine.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from nomothetic.routine_catalog import autonomon_catalog
from nomothetic.routine_log_store import InvalidRoutineName
from nomothetic.routine_manager import (
    RoutineAlreadyRunning,
    RoutineCredentialsError,
    RoutineLaunchError,
    RoutineManager,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RoutineStartRequest(BaseModel):
    """Payload to launch a routine: which routine, its parameters, and its lease."""

    routine: str = Field(..., min_length=1, max_length=64, description="Routine name to launch")
    params: dict[str, Any] = Field(
        default_factory=dict, description="Routine parameters forwarded to the plugin"
    )
    heartbeat_timeout_s: float | None = Field(
        default=None,
        ge=5,
        le=86400,
        description=(
            "Max seconds without a heartbeat before the routine is auto-stopped "
            "(minimum 5). Defaults to the device's configured value; clamped to "
            "its ceiling. Send POST /api/routines/heartbeat within this window."
        ),
    )
    max_duration_s: float | None = Field(
        default=None,
        gt=0,
        le=86400,
        description=(
            "Optional absolute cap on total runtime regardless of heartbeats. "
            "Defaults to the device's configured value (often none)."
        ),
    )


class RoutineStopRequest(BaseModel):
    """Payload to stop one running routine."""

    routine: str = Field(..., min_length=1, max_length=64, description="Routine name to stop")


class RoutineHeartbeatRequest(BaseModel):
    """Payload to renew a running routine's lease."""

    routine: str = Field(..., min_length=1, max_length=64, description="Routine name to renew")


class RoutineRunInfo(BaseModel):
    """Status snapshot of one launched routine run."""

    routine: str
    launch_id: str
    pid: int
    started_at: str
    heartbeat_timeout_s: float
    max_duration_s: float | None
    status: str


class RoutineStartResponse(RoutineRunInfo):
    """Run info for a freshly launched routine."""

    timestamp: str


class RoutineStopResponse(RoutineRunInfo):
    """Run info for a routine that was stopped."""

    timestamp: str


class RoutineStopAllResponse(BaseModel):
    """The set of routines stopped by a stop-all request."""

    stopped: list[RoutineRunInfo]
    timestamp: str


class RoutineHeartbeatResponse(BaseModel):
    """Lease status after a successful heartbeat."""

    routine: str
    status: str
    heartbeat_timeout_s: float
    seconds_remaining: float
    timestamp: str


class RoutineCatalogResponse(BaseModel):
    """The autonomy routines this device can launch (from autonomon's catalogue)."""

    routines: list[str]
    params_schema: dict[str, Any]
    version: str | None
    timestamp: str


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_routine_control_router() -> APIRouter:
    """Build the routine lifecycle-control router.

    The :class:`~nomothetic.routine_manager.RoutineManager` is read from
    ``request.app.state.routine_manager`` at call time so test fixtures can
    inject one (with a fake launcher).

    Returns
    -------
    APIRouter
        Router with ``/api/routines/available``, ``/start``, ``/heartbeat``,
        ``/stop``, and ``/stop-all``.
    """
    router = APIRouter(prefix="/api/routines", tags=["Routines"])

    def _manager(request: Request) -> RoutineManager:
        manager: RoutineManager | None = getattr(request.app.state, "routine_manager", None)
        if manager is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Routine manager not configured",
            )
        return manager

    @router.get("/available", response_model=RoutineCatalogResponse)
    async def available_routines():
        """List the routines this device can launch, from autonomon's catalogue.

        Returns an empty list when autonomon is not installed on the device. The
        catalogue read imports autonomon lazily, so it runs in a worker thread to
        keep the first call off the event loop.
        """
        catalog = await asyncio.to_thread(autonomon_catalog)
        return RoutineCatalogResponse(timestamp=_now(), **catalog)

    @router.post("/start", response_model=RoutineStartResponse, status_code=status.HTTP_201_CREATED)
    async def start_routine(body: RoutineStartRequest, request: Request):
        """Launch a routine from the request payload.

        Raises
        ------
        HTTPException
            400 invalid name/params, 409 already running, 503 no credentials
            configured, 500 if the subprocess could not be spawned.
        """
        manager = _manager(request)
        try:
            info = await manager.start(
                body.routine,
                body.params,
                heartbeat_timeout_s=body.heartbeat_timeout_s,
                max_duration_s=body.max_duration_s,
            )
        except RoutineAlreadyRunning as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except RoutineCredentialsError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        except (InvalidRoutineName, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except RoutineLaunchError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
            ) from exc
        logger.info("routine %r started via API", body.routine)
        return RoutineStartResponse(timestamp=_now(), **info)

    @router.post("/stop", response_model=RoutineStopResponse)
    async def stop_routine(body: RoutineStopRequest, request: Request):
        """Stop one running routine.

        Raises
        ------
        HTTPException
            400 if the name is invalid, 404 if the routine is not running.
        """
        manager = _manager(request)
        try:
            info = await manager.stop(body.routine)
        except InvalidRoutineName as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if info is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"routine {body.routine!r} is not running",
            )
        return RoutineStopResponse(timestamp=_now(), **info)

    @router.post("/stop-all", response_model=RoutineStopAllResponse)
    async def stop_all_routines(request: Request):
        """Stop every running routine and return what was stopped."""
        manager = _manager(request)
        stopped = await manager.stop_all()
        return RoutineStopAllResponse(
            stopped=[RoutineRunInfo(**info) for info in stopped],
            timestamp=_now(),
        )

    @router.post("/heartbeat", response_model=RoutineHeartbeatResponse)
    async def heartbeat_routine(body: RoutineHeartbeatRequest, request: Request):
        """Renew a running routine's lease (keep-alive).

        The operator calls this on an interval shorter than the routine's
        ``heartbeat_timeout_s`` to signal that contact is maintained; the routine
        keeps running. Stop calling it (lost connection, app closed) and the lease
        lapses and the routine is auto-stopped.

        Raises
        ------
        HTTPException
            400 if the name is invalid, 404 if the routine is not running.
        """
        manager = _manager(request)
        try:
            info = await manager.heartbeat(body.routine)
        except InvalidRoutineName as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if info is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"routine {body.routine!r} is not running",
            )
        return RoutineHeartbeatResponse(timestamp=_now(), **info)

    return router
