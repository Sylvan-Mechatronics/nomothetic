"""Fleet management endpoints for central-mode deployment.

Provides device registration, listing, detail queries, and removal.
All endpoints require JWT authentication and are tagged ``Fleet``
in the OpenAPI docs.
"""

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from nomothetic.auth import TokenPayload, jwt_required
from nomothetic.autonomy_store import AutonomyEventItem, AutonomyRunItem, AutonomyStore
from nomothetic.device_identity import verify_registration_proof
from nomothetic.fleet_store import DeviceItem, FleetStore
from nomothetic.rate_limit import register_rate_limit
from nomothetic.telemetry_store import TelemetryReadingItem, TelemetryStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class DeviceRegisterRequest(BaseModel):
    """Register a new device to the authenticated user."""

    vin: str = Field(..., min_length=1, max_length=64, description="Vehicle identification number")
    model: str = Field(..., min_length=1, max_length=64, description="Vehicle model name")
    registration_proof: str = Field(
        ...,
        min_length=1,
        description=(
            "Short-lived proof token from GET /api/device/auth/identity. "
            "Binds this registration request to recent device access."
        ),
    )
    device_public_key: Optional[str] = Field(
        default=None,
        max_length=4096,
        description=(
            "PEM public key from GET /api/device/auth/identity. The proof signature "
            "is verified against it and it is pinned to the VIN on first registration."
        ),
    )


class DeviceRegisterResponse(BaseModel):
    """Successful device registration response."""

    vin: str
    model: str
    registered_at: str
    timestamp: str


class DeviceListResponse(BaseModel):
    """List of devices owned by the authenticated user."""

    devices: list[DeviceItem]
    timestamp: str


class DeviceDetailResponse(BaseModel):
    """Detailed device view with optional telemetry."""

    vin: str
    model: str
    firmware_version: Optional[str] = None
    last_seen_at: Optional[str] = None
    registered_at: str
    role: str
    latest_telemetry: Optional[dict] = None
    timestamp: str


class DeviceRemoveResponse(BaseModel):
    """Device removal confirmation."""

    vin: str
    removed: bool
    timestamp: str


class DeviceTelemetryResponse(BaseModel):
    """Telemetry history for a device, newest first."""

    vin: str
    readings: list[TelemetryReadingItem]
    timestamp: str


class DeviceAutonomyRunsResponse(BaseModel):
    """Autonomy run history for a device, newest-started first."""

    vin: str
    runs: list[AutonomyRunItem]
    timestamp: str


class DeviceAutonomyEventsResponse(BaseModel):
    """One autonomy run's lifecycle events, in chronological order."""

    vin: str
    run_id: str
    events: list[AutonomyEventItem]
    timestamp: str


# ---------------------------------------------------------------------------
# Proof validation
# ---------------------------------------------------------------------------


def _validate_registration_proof(proof: str, vin: str) -> bool:
    """Validate the structural integrity of a registration proof token.

    .. warning::
        **Security limitation (FL1)**: Cryptographic signature verification is
        intentionally omitted.  The device and central fleet services use
        separate JWT secrets, so the fleet server cannot verify the
        device-issued signature.

        **Consequence**: Any authenticated central user who can guess or
        enumerate a VIN can forge a structurally valid proof for that VIN,
        enabling them to register a device they do not own.

        **Planned mitigation**: asymmetric device certificates — the device
        holds an EC private key, and the central server verifies the proof
        signature using the device's public key stored at manufacture time.
        See security checklist item FL1.

        Until that work lands, registration relies on the assumption that all
        central-authenticated users are trusted fleet owners who cannot
        meaningfully harm each other by claiming the same VIN.

    This function checks:

    - The token is a well-formed three-part JWT structure.
    - The ``exp`` claim is in the future (prevents replay attacks).
    - The ``sub`` claim matches the submitted VIN (VIN-binding).
    - The ``aud`` claim is ``"nomon-fleet"`` (audience restriction).

    Parameters
    ----------
    proof : str
        The proof JWT returned by ``GET /api/device/auth/identity``.
    vin : str
        The VIN submitted for registration; must match ``sub`` in the proof.

    Returns
    -------
    bool
        ``True`` if the proof is structurally valid and non-expired.
    """
    try:
        parts = proof.split(".")
        if len(parts) != 3:
            return False
        # Decode payload (base64url — add padding as required)
        padding = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
        if payload.get("exp", 0) < time.time():
            return False
        if payload.get("sub") != vin:
            return False
        if payload.get("aud") != "nomon-fleet":
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def _require_device_key() -> bool:
    """Whether registrations without a verifiable device key are refused.

    ``NOMON_FLEET_REQUIRE_DEVICE_KEY=1`` turns the legacy structural check off
    entirely; until every device runs a build that publishes its identity key,
    the default keeps the legacy path (logged) for keyless requests.
    """
    return os.environ.get("NOMON_FLEET_REQUIRE_DEVICE_KEY", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


# Module-level stores (set by create_app).
_fleet_store: Optional[FleetStore] = None
_telemetry_store: Optional[TelemetryStore] = None
_autonomy_store: Optional[AutonomyStore] = None


def set_fleet_store(store: FleetStore) -> None:
    """Store the fleet store instance for use by fleet routes."""
    global _fleet_store
    _fleet_store = store


def get_fleet_store() -> Optional[FleetStore]:
    """Return the current fleet store (if configured)."""
    return _fleet_store


def set_telemetry_store(store: TelemetryStore) -> None:
    """Store the telemetry store instance for use by fleet routes."""
    global _telemetry_store
    _telemetry_store = store


def get_telemetry_store() -> Optional[TelemetryStore]:
    """Return the current telemetry store (if configured)."""
    return _telemetry_store


def set_autonomy_store(store: AutonomyStore) -> None:
    """Store the autonomy store instance for use by fleet routes."""
    global _autonomy_store
    _autonomy_store = store


def get_autonomy_store() -> Optional[AutonomyStore]:
    """Return the current autonomy store (if configured)."""
    return _autonomy_store


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def create_fleet_router() -> APIRouter:
    """Build and return the fleet management API router.

    Returns
    -------
    APIRouter
        Router with device CRUD endpoints.
    """
    router = APIRouter(prefix="/api/fleet", tags=["Fleet"])

    def _require_store() -> FleetStore:
        store = get_fleet_store()
        if store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Fleet service not configured",
            )
        return store

    @router.post(
        "/devices",
        response_model=DeviceRegisterResponse,
        status_code=201,
        dependencies=[Depends(register_rate_limit)],
    )
    async def register_device(
        request: DeviceRegisterRequest,
        claims: TokenPayload = Depends(jwt_required),
    ):
        """Register a new device and link it to the authenticated user.

        Returns
        -------
        DeviceRegisterResponse
            VIN, model, and registration timestamp.

        Raises
        ------
        HTTPException
            400 if the registration proof is missing or structurally invalid.
            409 if the device is already registered.
            429 if the rate limit is exceeded.
        """
        store = _require_store()
        # Review S-3: verify the proof against the device's Ed25519 key. A key
        # already pinned to this VIN always wins over one supplied in the
        # request, so a second party cannot re-register a known device with a
        # key of their own.
        pinned = await store.get_device_public_key(request.vin)
        key = pinned or request.device_public_key
        if key is not None:
            if not verify_registration_proof(request.registration_proof, request.vin, key):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Registration proof does not verify against the device's identity "
                        "key. Fetch a fresh proof from the device and retry."
                    ),
                )
        elif _require_device_key():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="device_public_key is required to register a device",
            )
        elif not _validate_registration_proof(request.registration_proof, request.vin):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Invalid or expired registration proof. "
                    "Fetch a fresh proof from the device and retry."
                ),
            )
        else:
            logger.warning(
                "register_device: VIN %s registered with an unverifiable legacy proof "
                "(no device_public_key); set NOMON_FLEET_REQUIRE_DEVICE_KEY=1 to refuse",
                request.vin,
            )
        try:
            item = await store.register_device(claims.sub, request.vin, request.model)
            if pinned is None and request.device_public_key:
                await store.set_device_public_key(request.vin, request.device_public_key)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.error("Device registration internal error: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
            ) from exc
        return DeviceRegisterResponse(
            vin=item.vin,
            model=item.model,
            registered_at=item.registered_at,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @router.get("/devices", response_model=DeviceListResponse)
    async def list_devices(claims: TokenPayload = Depends(jwt_required)):
        """List all devices owned by the authenticated user.

        Returns
        -------
        DeviceListResponse
            List of device summaries.
        """
        store = _require_store()
        devices = await store.get_devices(claims.sub)
        return DeviceListResponse(
            devices=devices,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @router.get("/devices/{vin}", response_model=DeviceDetailResponse)
    async def get_device(
        vin: str = Path(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
        claims: TokenPayload = Depends(jwt_required),
    ):
        """Return detail for a single device including latest telemetry.

        Returns
        -------
        DeviceDetailResponse
            Device info with optional telemetry snapshot.

        Raises
        ------
        HTTPException
            404 if the device is not found or owned by another user.
        """
        store = _require_store()
        item = await store.get_device(claims.sub, vin)
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {vin} not found",
            )
        latest_telemetry: Optional[dict] = None
        telemetry_store = get_telemetry_store()
        if telemetry_store is not None:
            latest = await telemetry_store.get_latest(vin)
            if latest is not None:
                latest_telemetry = latest.model_dump()
        return DeviceDetailResponse(
            vin=item.vin,
            model=item.model,
            firmware_version=item.firmware_version,
            last_seen_at=item.last_seen_at,
            registered_at=item.registered_at,
            role=item.role,
            latest_telemetry=latest_telemetry,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @router.get("/devices/{vin}/telemetry", response_model=DeviceTelemetryResponse)
    async def get_device_telemetry(
        vin: str = Path(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
        limit: int = Query(100, ge=1, le=1000),
        since: Optional[str] = Query(None, description="ISO-8601 lower bound on recorded_at"),
        claims: TokenPayload = Depends(jwt_required),
    ):
        """Return telemetry history for a device the caller owns, newest first.

        Returns
        -------
        DeviceTelemetryResponse
            Ordered list of readings (possibly empty).

        Raises
        ------
        HTTPException
            404 if the device is not found or owned by another user.
            503 if telemetry persistence is not configured.
        """
        store = _require_store()
        # Ownership scoping: reuse the fleet store's per-owner device lookup.
        if await store.get_device(claims.sub, vin) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {vin} not found",
            )
        telemetry_store = get_telemetry_store()
        if telemetry_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Telemetry service not configured",
            )
        readings = await telemetry_store.get_history(vin, limit=limit, since=since)
        return DeviceTelemetryResponse(
            vin=vin,
            readings=readings,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @router.get("/devices/{vin}/autonomy", response_model=DeviceAutonomyRunsResponse)
    async def get_device_autonomy(
        vin: str = Path(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
        limit: int = Query(50, ge=1, le=500),
        since: Optional[str] = Query(None, description="ISO-8601 lower bound on started_at"),
        claims: TokenPayload = Depends(jwt_required),
    ):
        """Return autonomy run history for a device the caller owns.

        Runs are ordered newest-started first; each carries the coarse status
        derived from the brain's lifecycle events (autonomon Phase 7).

        Returns
        -------
        DeviceAutonomyRunsResponse
            Run records, newest-started first.

        Raises
        ------
        HTTPException
            404 if the device is not found or owned by another user.
            503 if autonomy persistence is not configured.
        """
        store = _require_store()
        # Ownership scoping: reuse the fleet store's per-owner device lookup.
        if await store.get_device(claims.sub, vin) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {vin} not found",
            )
        autonomy_store = get_autonomy_store()
        if autonomy_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Autonomy telemetry service not configured",
            )
        runs = await autonomy_store.get_runs(vin, limit=limit, since=since)
        return DeviceAutonomyRunsResponse(
            vin=vin,
            runs=runs,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @router.get(
        "/devices/{vin}/autonomy/{run_id}/events",
        response_model=DeviceAutonomyEventsResponse,
    )
    async def get_device_autonomy_events(
        vin: str = Path(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
        run_id: str = Path(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"),
        limit: int = Query(200, ge=1, le=1000),
        claims: TokenPayload = Depends(jwt_required),
    ):
        """Return one autonomy run's events, oldest first (log order).

        An unknown ``run_id`` for an owned device yields an empty list, the
        same shape as a run that has not reported events yet.

        Returns
        -------
        DeviceAutonomyEventsResponse
            Lifecycle events in chronological order.

        Raises
        ------
        HTTPException
            404 if the device is not found or owned by another user.
            503 if autonomy persistence is not configured.
        """
        store = _require_store()
        if await store.get_device(claims.sub, vin) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {vin} not found",
            )
        autonomy_store = get_autonomy_store()
        if autonomy_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Autonomy telemetry service not configured",
            )
        events = await autonomy_store.get_events(vin, run_id, limit=limit)
        return DeviceAutonomyEventsResponse(
            vin=vin,
            run_id=run_id,
            events=events,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @router.delete("/devices/{vin}", response_model=DeviceRemoveResponse)
    async def remove_device(
        vin: str = Path(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
        claims: TokenPayload = Depends(jwt_required),
    ):
        """Remove a device from the authenticated user's fleet.

        Does not delete the underlying Vehicle record — only removes the
        ownership edge.

        Returns
        -------
        DeviceRemoveResponse
            Confirmation with VIN and removed status.

        Raises
        ------
        HTTPException
            404 if the device is not found.
        """
        store = _require_store()
        removed = await store.remove_device(claims.sub, vin)
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {vin} not found",
            )
        return DeviceRemoveResponse(
            vin=vin,
            removed=True,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    return router
