"""Simple in-memory rate limiter for auth endpoints.

Uses a sliding-window counter keyed by client IP address.
Designed for single-process deployments; for multi-process setups
a shared store (Redis, etc.) would be needed.

Limiter instances are created per-app in ``create_app()`` and stored on
``app.state`` so that each test client gets fresh limiters automatically.
"""

import os
import time
from collections import defaultdict
from threading import Lock

from fastapi import HTTPException, Request, status


class RateLimiter:
    """Sliding-window rate limiter that tracks requests per IP.

    Parameters
    ----------
    max_requests : int
        Maximum number of requests allowed within the window.
    window_seconds : int
        Length of the sliding window in seconds.
    """

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def _clean_old(self, key: str, now: float) -> None:
        """Remove timestamps outside the current window."""
        cutoff = now - self.window_seconds
        self._requests[key] = [ts for ts in self._requests[key] if ts > cutoff]

    def check(self, key: str) -> None:
        """Record a request and raise if the limit is exceeded.

        Parameters
        ----------
        key : str
            Identifier for the requester (typically client IP).

        Raises
        ------
        HTTPException
            429 Too Many Requests when the rate limit is exceeded.
        """
        now = time.monotonic()
        with self._lock:
            self._clean_old(key, now)
            if len(self._requests[key]) >= self.max_requests:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many requests. Please try again later.",
                )
            self._requests[key].append(now)

    def reset(self) -> None:
        """Clear all tracked requests (useful for testing)."""
        with self._lock:
            self._requests.clear()


def _client_ip(request: Request) -> str:
    """Extract client IP from the request.

    When ``NOMON_TRUST_PROXY`` is set to ``"1"`` or ``"true"`` the first
    address in the ``X-Forwarded-For`` header is used.  This is safe **only**
    when a trusted reverse proxy (e.g. nginx, Caddy) is the sole entity
    setting that header.  When the variable is unset or false the direct
    ``request.client.host`` is always used, preventing header-spoofing
    bypasses.
    """
    trust_proxy = os.environ.get("NOMON_TRUST_PROXY", "").lower() in ("1", "true")
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def login_rate_limit(request: Request) -> None:
    """FastAPI dependency that enforces login rate limiting.

    Reads the ``login_limiter`` from ``app.state`` (created in
    ``create_app``).  Allows 5 requests per minute per IP address.
    """
    limiter: RateLimiter = request.app.state.login_limiter
    limiter.check(_client_ip(request))


async def register_rate_limit(request: Request) -> None:
    """FastAPI dependency that enforces registration rate limiting.

    Reads the ``register_limiter`` from ``app.state`` (created in
    ``create_app``).  Allows 10 requests per minute per IP address.
    """
    limiter: RateLimiter = request.app.state.register_limiter
    limiter.check(_client_ip(request))


async def pairing_rate_limit(request: Request) -> None:
    """FastAPI dependency that enforces pairing rate limiting.

    Reads the ``pairing_limiter`` from ``app.state`` (created in
    ``create_app``).  Allows 3 requests per minute per IP address.
    """
    limiter: RateLimiter = request.app.state.pairing_limiter
    limiter.check(_client_ip(request))


async def network_rate_limit(request: Request) -> None:
    """FastAPI dependency that enforces Wi-Fi provisioning rate limiting.

    Reads the ``network_limiter`` from ``app.state`` (created in
    ``create_app``).  Allows 5 requests per minute per IP address.
    """
    limiter: RateLimiter = request.app.state.network_limiter
    limiter.check(_client_ip(request))


async def ai_rate_limit(request: Request) -> None:
    """FastAPI dependency that enforces AI command rate limiting.

    Reads the ``ai_limiter`` from ``app.state`` (created in ``create_app``).
    Allows 10 requests per minute per IP address — each command can fan out
    into several Anthropic API round trips, so this stays deliberately low.
    """
    limiter: RateLimiter = request.app.state.ai_limiter
    limiter.check(_client_ip(request))


async def stt_rate_limit(request: Request) -> None:
    """FastAPI dependency that enforces voice transcription rate limiting.

    Reads the ``stt_limiter`` from ``app.state`` (created in ``create_app``).
    Allows 20 requests per minute per IP address — separate from the AI
    command limiter so a voice command (transcribe + command) does not
    double-count against the chat budget, while still capping the CPU-heavy
    on-device recognition.
    """
    limiter: RateLimiter = request.app.state.stt_limiter
    limiter.check(_client_ip(request))
