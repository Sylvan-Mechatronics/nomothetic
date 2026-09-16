"""Auth endpoints for central-mode deployment.

Provides user registration, login, token refresh, logout, and profile retrieval.
All endpoints are tagged ``Auth`` in the OpenAPI docs.
"""

import hmac
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from nomothetic.auth import (
    AuthService,
    TokenPayload,
    get_auth_service,
    jwt_required,
)
from nomothetic.rate_limit import login_rate_limit, refresh_rate_limit, register_rate_limit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """User registration request body."""

    email: EmailStr = Field(..., description="User email address")
    password: str = Field(..., min_length=8, description="Password (min 8 chars)")
    display_name: str = Field(..., min_length=1, max_length=100, description="Display name")
    invite_code: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Required when the server sets NOMON_REGISTRATION_INVITE_CODE; " "otherwise ignored."
        ),
    )


def _invite_code_required() -> str:
    """Return the configured registration invite code, or ``""`` if open."""
    return os.environ.get("NOMON_REGISTRATION_INVITE_CODE", "").strip()


class LoginRequest(BaseModel):
    """User login request body."""

    email: EmailStr = Field(..., description="User email address")
    password: str = Field(..., min_length=1, description="User password")


class RefreshRequest(BaseModel):
    """Token refresh request body."""

    refresh_token: str = Field(..., description="Refresh token from login")


class TokenResponse(BaseModel):
    """Authentication token response."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    """Current user profile response."""

    email: str
    display_name: str
    created_at: str
    last_login_at: str | None
    timestamp: str


class RegisterResponse(BaseModel):
    """Registration success response with tokens."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class UpdateProfileRequest(BaseModel):
    """Profile update request body."""

    display_name: str = Field(..., min_length=1, max_length=100, description="New display name")


class ChangePasswordRequest(BaseModel):
    """Password change request body."""

    current_password: str = Field(..., min_length=1, description="Current password")
    new_password: str = Field(..., min_length=8, description="New password (min 8 chars)")


class ChangePasswordResponse(BaseModel):
    """Password change confirmation response."""

    success: bool
    timestamp: str


class LogoutRequest(BaseModel):
    """Token revocation request body."""

    refresh_token: str = Field(..., description="Refresh token to revoke")


class LogoutResponse(BaseModel):
    """Logout confirmation response."""

    success: bool
    timestamp: str


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def create_auth_router() -> APIRouter:
    """Build and return the auth API router.

    Returns
    -------
    APIRouter
        Router with registration, login, refresh, and profile endpoints.
    """
    router = APIRouter(prefix="/api/auth", tags=["Auth"])

    def _require_service() -> AuthService:
        svc = get_auth_service()
        if svc is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auth service not configured",
            )
        return svc

    @router.post(
        "/register",
        response_model=RegisterResponse,
        status_code=201,
        dependencies=[Depends(register_rate_limit)],
    )
    async def register(request: RegisterRequest):
        """Register a new user account and return tokens.

        .. note::
            Email verification is deferred to a future phase. Until then, set
            ``NOMON_REGISTRATION_INVITE_CODE`` on the central server so only
            people holding the code can create accounts (review finding S-5);
            with it unset any email can register and receive tokens immediately.

        Returns
        -------
        RegisterResponse
            Tokens plus the created user profile.

        Raises
        ------
        HTTPException
            403 if an invite code is required and missing/wrong.
            409 if the email is already registered.
            422 on validation failure.
            429 if rate limit is exceeded.
        """
        required = _invite_code_required()
        if required and not (
            request.invite_code and hmac.compare_digest(request.invite_code, required)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Registration requires a valid invite code",
            )
        svc = _require_service()
        try:
            user = await svc.create_user(request.email, request.password, request.display_name)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        tokens = await svc.create_tokens(user.email)
        return RegisterResponse(
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            token_type=tokens["token_type"],
            expires_in=tokens["expires_in"],
            user=UserResponse(
                email=user.email,
                display_name=user.display_name,
                created_at=user.created_at,
                last_login_at=user.last_login_at,
                timestamp=datetime.now(timezone.utc).isoformat(),
            ),
        )

    @router.post("/login", response_model=TokenResponse, dependencies=[Depends(login_rate_limit)])
    async def login(request: LoginRequest):
        """Authenticate a user and issue access + refresh tokens.

        Returns
        -------
        TokenResponse
            JWT access and refresh tokens.

        Raises
        ------
        HTTPException
            401 on invalid credentials.
            429 if rate limit is exceeded.
        """
        svc = _require_service()
        user = await svc.authenticate(request.email, request.password)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )
        tokens = await svc.create_tokens(user.email)
        return TokenResponse(**tokens)

    @router.post(
        "/refresh", response_model=TokenResponse, dependencies=[Depends(refresh_rate_limit)]
    )
    async def refresh(request: RefreshRequest):
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
            tokens = await svc.refresh_token(request.refresh_token)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
            ) from exc
        return TokenResponse(**tokens)

    @router.get("/me", response_model=UserResponse)
    async def me(claims: TokenPayload = Depends(jwt_required)):
        """Return the authenticated user's profile.

        Returns
        -------
        UserResponse
            User email, display name, and timestamps.

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
        return UserResponse(
            email=user.email,
            display_name=user.display_name,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @router.patch("/me", response_model=UserResponse)
    async def update_me(
        request: UpdateProfileRequest,
        claims: TokenPayload = Depends(jwt_required),
    ):
        """Update the authenticated user's display name.

        Returns
        -------
        UserResponse
            The updated profile.

        Raises
        ------
        HTTPException
            401 if the token is missing or invalid.
            404 if the user no longer exists.
        """
        svc = _require_service()
        user = await svc.update_display_name(claims.sub, request.display_name)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        return UserResponse(
            email=user.email,
            display_name=user.display_name,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @router.post("/change-password", response_model=ChangePasswordResponse)
    async def change_password(
        request: ChangePasswordRequest,
        claims: TokenPayload = Depends(jwt_required),
    ):
        """Change the authenticated user's password.

        Verifies the current password, stores the new hash, and revokes all
        of the user's refresh tokens so other sessions must re-authenticate.

        Returns
        -------
        ChangePasswordResponse
            Confirmation.

        Raises
        ------
        HTTPException
            401 if the current password is incorrect.
            422 if the new password fails validation.
        """
        svc = _require_service()
        try:
            await svc.change_password(claims.sub, request.current_password, request.new_password)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
            ) from exc
        return ChangePasswordResponse(
            success=True,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @router.post("/logout", response_model=LogoutResponse)
    async def logout(
        request: LogoutRequest,
        claims: TokenPayload = Depends(jwt_required),
    ):
        """Revoke a refresh token (server-side logout).

        Always returns 200 — idempotent to avoid leaking token validity.
        """
        svc = _require_service()
        await svc.revoke_refresh_token(request.refresh_token)
        return LogoutResponse(
            success=True,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    return router
