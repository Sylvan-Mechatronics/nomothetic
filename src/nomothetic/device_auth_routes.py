"""Device-mode authentication endpoints.

Provides pairing, token refresh, and profile retrieval for the
single-owner device authentication model.  No registration or login
endpoints — pairing IS the initial authentication step.

See ADR-014 for design rationale.
"""

import importlib.metadata as _meta
import logging
import os
import secrets
import socket
from datetime import datetime, timezone
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from nomothetic.auth import AuthService, TokenPayload, get_auth_service, owner_required
from nomothetic.pairing import PairingState
from nomothetic.rate_limit import identity_rate_limit, pairing_rate_limit, refresh_rate_limit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class PairingStatusResponse(BaseModel):
    """Current device pairing status."""

    paired: bool
    pairing_available: bool


class PairRequest(BaseModel):
    """Device pairing request body."""

    secret: str = Field(..., min_length=1, description="Pairing secret from device console")
    display_name: str = Field(..., min_length=1, max_length=100, description="Owner display name")


class ApPairRequest(BaseModel):
    """AP-mode pairing request body (no explicit secret required).

    Network presence on the 192.168.4.0/24 subnet proves the client
    authenticated with the Soft AP's WPA2 passphrase — since review finding
    S-1 a separate 20-character secret, not the 8-digit pairing code.
    """

    display_name: str = Field(..., min_length=1, max_length=100, description="Owner display name")


class PairResponse(BaseModel):
    """Successful pairing response with tokens."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    device_hostname: str
    """mDNS hostname of the device (e.g. ``nomon-abcd.local``).  Use this to
    reach the device API once it has joined the home network."""


class RefreshRequest(BaseModel):
    """Token refresh request body."""

    refresh_token: str = Field(..., description="Refresh token from pairing")


class TokenResponse(BaseModel):
    """Authentication token response."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class SessionResetResponse(BaseModel):
    """Session reset confirmation response."""

    success: bool
    timestamp: str


class DeviceUserResponse(BaseModel):
    """Paired device owner profile."""

    email: str
    display_name: str
    created_at: str
    last_login_at: str | None
    timestamp: str


class DeviceIdentityResponse(BaseModel):
    """Device hardware identity."""

    vin: str
    model: str
    hostname: str
    firmware_version: str
    registration_proof: str
    """Short-lived proof token (JWT, ``alg=EdDSA``) signed by this device's
    identity key. Submit it to the central fleet registration endpoint with
    the VIN and ``device_public_key``; central verifies the signature and pins
    the key to the VIN on first registration (review finding S-3)."""
    device_public_key: Optional[str] = None
    """PEM public key matching the proof signature (``None`` only on legacy
    builds without a device identity)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _derive_vin() -> str:
    """Return the device's vehicle identification number.

    Resolution order:
    1. ``NOMON_DEVICE_ID`` environment variable.
    2. Raspberry Pi serial from ``/proc/cpuinfo`` (``Serial`` field).
    3. System hostname (fallback for dev machines).
    """
    vin = os.environ.get("NOMON_DEVICE_ID")
    if vin:
        return vin
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("Serial"):
                    serial = line.split(":")[-1].strip()
                    if serial and serial != "0000000000000000":
                        return serial
    except OSError:
        pass
    return socket.gethostname()


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


_AP_SUBNET = IPv4Network("192.168.4.0/24")


def _is_ap_client(request: Request) -> bool:
    """Return True if the request originates from the Soft AP subnet."""
    if request.client is None:
        return False
    try:
        return IPv4Address(request.client.host) in _AP_SUBNET
    except ValueError:
        return False


def _get_device_hostname() -> str:
    """Return the mDNS hostname for this device (e.g. ``nomon-abcd.local``).

    Reads the last 4 hex digits of the wlan0 MAC address to match the Soft AP
    SSID convention used by ap-mode.sh.  Falls back to the system hostname if
    the MAC is unavailable (e.g. on a dev machine without wlan0).
    """
    mac_path = Path("/sys/class/net/wlan0/address")
    if mac_path.exists():
        try:
            mac = mac_path.read_text().strip().replace(":", "").lower()
            last4 = mac[-4:]
            return f"nomon-{last4}.local"
        except OSError:
            pass
    # Fallback for dev machines: use the system hostname
    return f"{socket.gethostname()}.local"


_DEVICE_OWNER_EMAIL = "device-owner@local"


async def _reset_device_session(
    pairing: PairingState,
    svc: AuthService,
    owner_email: str | None,
) -> None:
    """Invalidate the current device auth session and rotate signing state."""
    if owner_email is not None:
        await svc.revoke_all_tokens(owner_email)
    pairing.reset_session()
    svc.update_signing_secret(pairing.jwt_secret)


async def _issue_pairing_tokens(
    pairing: PairingState,
    svc: AuthService,
    display_name: str,
) -> PairResponse:
    """Rotate any old session, then issue fresh tokens for the device owner."""
    random_password = secrets.token_urlsafe(32)
    await _reset_device_session(pairing, svc, _DEVICE_OWNER_EMAIL)
    try:
        await svc.create_user(_DEVICE_OWNER_EMAIL, random_password, display_name)
    except ValueError:
        # User already exists (e.g. from a previous pairing cycle) — continue.
        # The device uses a fixed local owner identity; re-pairing only rotates
        # credentials, it does not create additional accounts.
        pass
    pairing.complete_pairing(_DEVICE_OWNER_EMAIL)
    tokens = await svc.create_tokens(_DEVICE_OWNER_EMAIL)
    return PairResponse(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        token_type=tokens["token_type"],
        expires_in=tokens["expires_in"],
        device_hostname=_get_device_hostname(),
    )


def create_device_auth_router() -> APIRouter:
    """Build and return the device auth API router.

    The ``PairingState`` and ``AuthService`` instances are read from
    ``request.app.state`` at call time, allowing test fixtures to inject
    fresh instances per test client.

    Returns
    -------
    APIRouter
        Router with pairing, refresh, and profile endpoints.
    """
    router = APIRouter(prefix="/api/device/auth", tags=["DeviceAuth"])

    def _get_pairing(request: Request) -> PairingState:
        pairing: PairingState | None = getattr(request.app.state, "pairing_state", None)
        if pairing is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Pairing not configured",
            )
        return pairing

    def _require_service() -> AuthService:
        svc = get_auth_service()
        if svc is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auth service not configured",
            )
        return svc

    @router.get("/status", response_model=PairingStatusResponse)
    async def pairing_status(request: Request):
        """Return the current pairing status.

        Returns
        -------
        PairingStatusResponse
            Whether the device is paired and whether a pairing secret is available.
        """
        pairing = _get_pairing(request)
        return PairingStatusResponse(
            paired=pairing.is_paired(),
            pairing_available=pairing.has_pairing_secret() and not pairing.is_paired(),
        )

    @router.post(
        "/pair",
        response_model=PairResponse,
        status_code=200,
        dependencies=[Depends(pairing_rate_limit)],
    )
    async def pair(request_body: PairRequest, request: Request):
        """Pair with the device using the console secret.

        Creates the owner user account and issues JWT tokens. Supplying the
        physical pairing secret also authorises secure re-pairing: any existing
        session is invalidated and replaced with fresh credentials.

        Parameters
        ----------
        request_body : PairRequest
            The pairing secret and owner display name.

        Returns
        -------
        PairResponse
            JWT access and refresh tokens.

        Raises
        ------
        HTTPException
            401 if the secret is wrong.
            429 if rate limit is exceeded.
        """
        pairing = _get_pairing(request)
        svc = _require_service()

        if not pairing.verify_secret(request_body.secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid pairing secret",
            )

        response = await _issue_pairing_tokens(pairing, svc, request_body.display_name)
        logger.info("Device paired successfully for %s", request_body.display_name)
        return response

    @router.post(
        "/pair/ap",
        response_model=PairResponse,
        status_code=200,
        dependencies=[Depends(pairing_rate_limit)],
    )
    async def pair_via_ap(request_body: ApPairRequest, request: Request):
        """Pair via Soft AP network presence — no explicit secret required.

        Only accepts requests from 192.168.4.0/24. Being on that subnet proves
        the client authenticated with the Soft AP's WPA2 passphrase — the
        20-character secret in ``ap_passphrase``, which since review finding
        S-1 is deliberately distinct from the 8-digit pairing code (an 8-digit
        PSK is crackable offline from a captured handshake). This network
        presence also authorises secure re-pairing when a previous session
        already exists.

        Returns
        -------
        PairResponse
            JWT access and refresh tokens with device hostname.

        Raises
        ------
        HTTPException
            403 if the request does not originate from the AP subnet.
            503 if the pairing secret is unavailable.
            429 if rate limit is exceeded.
        """
        if not _is_ap_client(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="AP pairing is only available from the device AP network (192.168.4.0/24)",
            )

        pairing = _get_pairing(request)
        svc = _require_service()

        if not pairing.has_pairing_secret():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Pairing secret not available",
            )

        response = await _issue_pairing_tokens(pairing, svc, request_body.display_name)
        logger.info("Device paired via AP network for %s", request_body.display_name)
        return response

    @router.post(
        "/refresh", response_model=TokenResponse, dependencies=[Depends(refresh_rate_limit)]
    )
    async def refresh(request_body: RefreshRequest):
        """Rotate a refresh token and issue new tokens.

        Returns
        -------
        TokenResponse
            New JWT access and refresh tokens.

        Raises
        ------
        HTTPException
            401 on invalid or expired refresh token.
        """
        svc = _require_service()
        try:
            tokens = await svc.refresh_token(request_body.refresh_token)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
            ) from exc
        return TokenResponse(**tokens)

    @router.delete("/session", response_model=SessionResetResponse)
    async def delete_session(
        request: Request,
        claims: TokenPayload = Depends(owner_required),
    ):
        """Invalidate the current device session and reopen pairing.

        Requires a valid **owner** device JWT (a plugin token is refused with
        403). The current signing secret is rotated so both access and refresh
        tokens from the old session stop working.
        """
        pairing = _get_pairing(request)
        svc = _require_service()
        await _reset_device_session(pairing, svc, claims.sub)
        return SessionResetResponse(
            success=True,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @router.get("/me", response_model=DeviceUserResponse)
    async def me(claims: TokenPayload = Depends(owner_required)):
        """Return the paired owner's profile.

        Returns
        -------
        DeviceUserResponse
            Owner email, display name, and timestamps.

        Raises
        ------
        HTTPException
            401 if the token is missing or invalid.
            404 if the user no longer exists.
        """
        svc = _require_service()
        user = await svc.get_user(claims.sub)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        return DeviceUserResponse(
            email=user.email,
            display_name=user.display_name,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @router.get(
        "/identity",
        response_model=DeviceIdentityResponse,
        dependencies=[Depends(identity_rate_limit)],
    )
    async def identity(request: Request, _claims: TokenPayload = Depends(owner_required)):
        """Return the device's hardware identity.

        Returns
        -------
        DeviceIdentityResponse
            VIN (Pi serial / env override), model name, hostname, a
            short-lived registration proof signed by the device identity key,
            and that key's public half.

        Raises
        ------
        HTTPException
            401 if the token is missing or invalid.
            429 if the rate limit is exceeded.
        """
        svc = _require_service()
        vin = _derive_vin()
        try:
            firmware_version = _meta.version("nomothetic")
        except _meta.PackageNotFoundError:
            firmware_version = "unknown"
        identity_key = getattr(request.app.state, "device_identity", None)
        if identity_key is not None:
            proof = identity_key.create_registration_proof(vin)
            public_key: Optional[str] = identity_key.public_key_pem
        else:  # legacy: unverifiable HS256 proof
            proof = svc.create_registration_proof(vin)
            public_key = None
        return DeviceIdentityResponse(
            vin=vin,
            model=os.environ.get("NOMON_MODEL", "nomon"),
            hostname=socket.gethostname(),
            firmware_version=firmware_version,
            registration_proof=proof,
            device_public_key=public_key,
        )

    return router
