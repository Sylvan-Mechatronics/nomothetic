"""ArcadeDB database client using the HTTP API.

Supports Gremlin and SQL queries via POST /api/v1/command/{database}.
Uses httpx.AsyncClient for connection pooling and async I/O.
See ADR-012 for design rationale.
"""

import logging
import os
from dataclasses import dataclass
from typing import Any, cast

try:
    import httpx

    _HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]
    _HTTPX_AVAILABLE = False

logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    """Error communicating with the ArcadeDB HTTP API.

    Attributes
    ----------
    status_code : int
        HTTP status code from the server (0 if unreachable).
    message : str
        Human-readable error description.
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"ArcadeDB error {status_code}: {message}")


@dataclass
class DatabaseConfig:
    """Connection parameters for an ArcadeDB instance.

    Attributes
    ----------
    host : str
        Hostname or IP of the ArcadeDB server.
    port : int
        HTTP API port (default 2480).
    database : str
        Target database name.
    user : str
        Authentication username.
    password : str
        Authentication password.
    """

    host: str
    port: int
    database: str
    user: str
    password: str
    use_tls: bool = False

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        """Build a config from environment variables.

        Reads
        -----
        ARCADEDB_HOST : str (required)
        ARCADEDB_HTTP_PORT : int (default 2480)
        ARCADEDB_DATABASE : str (default ``nomon_central``)
        ARCADEDB_ROOT_PASSWORD : str (required)

        Raises
        ------
        ValueError
            If ``ARCADEDB_HOST`` or ``ARCADEDB_ROOT_PASSWORD`` is not set.
        """
        host = os.environ.get("ARCADEDB_HOST")
        if not host:
            raise ValueError("ARCADEDB_HOST environment variable is required")

        password = os.environ.get("ARCADEDB_ROOT_PASSWORD")
        if not password:
            raise ValueError("ARCADEDB_ROOT_PASSWORD environment variable is required")

        return cls(
            host=host,
            port=int(os.environ.get("ARCADEDB_HTTP_PORT", "2480")),
            database=os.environ.get("ARCADEDB_DATABASE", "nomon_central"),
            user="root",
            password=password,
            use_tls=os.environ.get("ARCADEDB_USE_TLS", "").lower() in ("1", "true"),
        )


class DatabaseClient:
    """Async client for the ArcadeDB HTTP API.

    Parameters
    ----------
    config : DatabaseConfig
        Connection parameters.

    Raises
    ------
    RuntimeError
        If ``httpx`` is not installed.
    """

    def __init__(self, config: DatabaseConfig) -> None:
        if not _HTTPX_AVAILABLE:
            raise RuntimeError("httpx is required: pip install httpx")

        self._config = config
        scheme = "https" if config.use_tls else "http"
        self._client = httpx.AsyncClient(
            base_url=f"{scheme}://{config.host}:{config.port}",
            auth=httpx.BasicAuth(config.user, config.password),
            timeout=10.0,
        )

    async def execute_gremlin(self, query: str, params: dict | None = None) -> list[dict[str, Any]]:
        """Execute a Gremlin query via the HTTP command endpoint.

        Parameters
        ----------
        query : str
            Gremlin traversal string.
        params : dict, optional
            Query parameters (passed as-is to ArcadeDB).

        Returns
        -------
        list[dict]
            The ``result`` array from the server response.

        Raises
        ------
        DatabaseError
            On non-2xx HTTP responses.
        """
        return await self._execute("gremlin", query, params)

    async def execute_sql(self, query: str, params: dict | None = None) -> list[dict[str, Any]]:
        """Execute a SQL query via the HTTP command endpoint.

        Parameters
        ----------
        query : str
            SQL statement.
        params : dict, optional
            Query parameters.

        Returns
        -------
        list[dict]
            The ``result`` array from the server response.

        Raises
        ------
        DatabaseError
            On non-2xx HTTP responses.
        """
        return await self._execute("sql", query, params)

    async def health(self) -> bool:
        """Check whether the ArcadeDB server is ready.

        Returns
        -------
        bool
            True if the server responds with HTTP 200, False otherwise.
        """
        try:
            resp = await self._client.get("/api/v1/ready")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    # -- internal -----------------------------------------------------------

    async def _execute(
        self, language: str, command: str, params: dict | None
    ) -> list[dict[str, Any]]:
        """Post a command to the ArcadeDB HTTP API.

        Parameters
        ----------
        language : str
            Query language (``gremlin`` or ``sql``).
        command : str
            The query string.
        params : dict, optional
            Query parameters.

        Returns
        -------
        list[dict]
            Parsed ``result`` array.

        Raises
        ------
        DatabaseError
            On non-2xx HTTP responses.
        """
        url = f"/api/v1/command/{self._config.database}"
        body = {
            "language": language,
            "command": command,
            "params": params or {},
        }
        try:
            resp = await self._client.post(url, json=body)
        except Exception as exc:
            logger.error("Database connection failed: %s", exc)
            raise DatabaseError(0, "Database connection failed") from exc

        if resp.status_code >= 400:
            logger.error("Database error %d: %s", resp.status_code, resp.text)
            raise DatabaseError(resp.status_code, "Database query failed")

        payload = resp.json()
        result = payload.get("result", [])
        if not isinstance(result, list):
            return []
        return cast(list[dict[str, Any]], result)
