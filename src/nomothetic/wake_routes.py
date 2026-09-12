"""Wake-word listener endpoints (status + runtime updates), ADR-021.

Mounted on the device router (so both endpoints inherit device JWT auth):

* ``GET /api/voice/wake`` — listener status (enabled/running/state/phrase).
* ``PUT /api/voice/wake`` — enable/disable the listener and/or update the
  catch phrase and its variants at runtime.

Runtime updates are **in-memory only**: the persistent boot configuration
lives in the environment (``NOMON_WAKE_*`` via ``.env.device`` /
``/etc/nomothetic/nomothetic.env``), so a service restart reverts to it.
That makes ``PUT`` the tuning loop for finding phrase variants the Vosk model
actually hears — once a variant works, persist it in the env file.

Disabling (as opposed to restarting for a phrase/variant change) also
releases the shared Vosk model's memory
(:meth:`~nomothetic.wake.WakeWordListener.unload_model`) — the next
transcription anywhere (this listener restarting, or an app-initiated
``/api/ai/transcribe`` upload) reloads it lazily at the usual multi-second
cost.

The heavy lifting lives in :mod:`nomothetic.wake`; this module only maps HTTP
shapes onto the listener, mirroring :mod:`nomothetic.ai_routes`.
"""

import asyncio
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from nomothetic.wake import WakeWordListener


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class WakeStatusResponse(BaseModel):
    """Current wake-word listener state (never any captured audio or text)."""

    enabled: bool
    running: bool
    paused: bool
    state: str
    phrase: Optional[str] = Field(default=None, description="null when unconfigured")
    variants: list[str]
    followup_window_s: float
    timestamp: str


class WakeUpdateRequest(BaseModel):
    """Runtime update: enable/disable and/or change the phrase or variants.

    Every field is optional; omitted fields are left unchanged.  An empty
    request is a no-op that returns the current status.
    """

    enabled: Optional[bool] = None
    phrase: Optional[Annotated[str, Field(min_length=1, max_length=64)]] = None
    variants: Optional[
        Annotated[list[Annotated[str, Field(min_length=1, max_length=64)]], Field(max_length=8)]
    ] = None


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_wake_router() -> APIRouter:
    """Build the wake-word listener router.

    The :class:`~nomothetic.wake.WakeWordListener` is read from ``app.state``
    at call time so test fixtures can inject fakes.

    Returns
    -------
    APIRouter
        Router with ``GET``/``PUT`` ``/api/voice/wake``.
    """
    router = APIRouter(prefix="/api/voice/wake", tags=["Voice"])

    def _listener(request: Request) -> WakeWordListener:
        listener: Optional[WakeWordListener] = getattr(request.app.state, "wake_listener", None)
        if listener is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Wake-word listening is not available on this device",
            )
        return listener

    def _status(listener: WakeWordListener) -> WakeStatusResponse:
        return WakeStatusResponse(
            enabled=listener.enabled,
            running=listener.is_running,
            paused=listener.is_paused,
            state=listener.state,
            phrase=listener.phrase or None,
            variants=listener.variants,
            followup_window_s=listener.followup_window_s,
            timestamp=_now(),
        )

    @router.get("", response_model=WakeStatusResponse)
    async def get_wake_status(request: Request):
        """Report the wake-word listener's current configuration and state."""
        return _status(_listener(request))

    @router.put("", response_model=WakeStatusResponse)
    async def update_wake_config(body: WakeUpdateRequest, request: Request):
        """Enable/disable the listener and/or update its catch phrase at runtime.

        Phrase or variant changes restart the listener (the recognizer
        grammar is built at thread start).  Changes do not persist across a
        service restart — set ``NOMON_WAKE_*`` in the environment for that.

        Raises
        ------
        HTTPException
            400 when enabling without any phrase configured,
            503 when the listener cannot start (pyaudio missing) or the
            listener subsystem is unavailable.
        """
        listener = _listener(request)
        loop = asyncio.get_running_loop()
        config_changed = body.phrase is not None or body.variants is not None
        target_enabled = body.enabled if body.enabled is not None else listener.enabled

        # stop() joins the thread, so keep it (and start) off the event loop.
        if config_changed or not target_enabled:
            await asyncio.to_thread(listener.stop)
        if not target_enabled:
            # Actually going idle (not just restarting for a config change) —
            # release the shared Vosk model so its memory comes back until
            # voice is re-enabled.
            await asyncio.to_thread(listener.unload_model)
        if config_changed:
            listener.set_phrase_config(phrase=body.phrase, variants=body.variants)
        if target_enabled:
            if not listener.phrase:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No wake phrase configured; supply 'phrase' when enabling",
                )
            started = await asyncio.to_thread(listener.start_background, loop)
            if not started:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "Wake-word listener could not start; the audio stack "
                        "(nomothetic[audio]) may be missing on this device"
                    ),
                )
        return _status(listener)

    return router
