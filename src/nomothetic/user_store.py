"""User persistence layer with in-memory and ArcadeDB backends.

Protocol-based store abstraction. AuthService delegates user CRUD to a
UserStore implementation, enabling test/dev in-memory mode and production
ArcadeDB persistence behind the same interface.
"""

import logging
from typing import TYPE_CHECKING, Any, runtime_checkable

from typing_extensions import Protocol

from nomothetic.auth import UserRecord
from nomothetic.db_utils import coerce_count as _coerce_count

if TYPE_CHECKING:
    from nomothetic.db import DatabaseClient

logger = logging.getLogger(__name__)


_ALLOWED_USER_UPDATE_FIELDS = frozenset({"display_name", "last_login_at", "active"})


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class UserStore(Protocol):
    """Abstract interface for user persistence."""

    async def get_user(self, email: str) -> UserRecord | None:
        """Look up a user by email.

        Parameters
        ----------
        email : str
            Normalised (lower-case, stripped) email address.

        Returns
        -------
        UserRecord or None
        """
        ...  # pragma: no cover

    async def create_user(
        self,
        email: str,
        display_name: str,
        password_hash: str,
        created_at: str,
    ) -> UserRecord:
        """Persist a new user record.

        Parameters
        ----------
        email : str
            Normalised email.
        display_name : str
            User-visible name.
        password_hash : str
            bcrypt hash string.
        created_at : str
            ISO-8601 UTC timestamp.

        Returns
        -------
        UserRecord
            The newly created record.
        """
        ...  # pragma: no cover

    async def update_user(self, email: str, **fields: str) -> UserRecord | None:
        """Update mutable fields on an existing user.

        Parameters
        ----------
        email : str
            Normalised email address.
        **fields : str
            Field names and new values to set.

        Returns
        -------
        UserRecord or None
            Updated record, or None if the user does not exist.
        """
        ...  # pragma: no cover

    async def set_password_hash(self, email: str, password_hash: str) -> bool:
        """Replace the stored password hash for a user.

        Kept off :meth:`update_user`'s general field whitelist so credential
        mutation stays on its own audited path.

        Parameters
        ----------
        email : str
            Normalised email address.
        password_hash : str
            New bcrypt hash string.

        Returns
        -------
        bool
            ``True`` if the user existed and was updated.
        """
        ...  # pragma: no cover

    async def user_exists(self, email: str) -> bool:
        """Check whether a user with the given email exists.

        Parameters
        ----------
        email : str
            Normalised email address.

        Returns
        -------
        bool
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryUserStore:
    """Dict-backed user store for testing and development.

    Thread-safety note: sufficient for single-process FastAPI (uvicorn)
    deployments; not suitable for multi-process workers.
    """

    def __init__(self) -> None:
        self._users: dict[str, UserRecord] = {}

    async def get_user(self, email: str) -> UserRecord | None:
        """Return the user record for *email*, or ``None``."""
        return self._users.get(email)

    async def create_user(
        self,
        email: str,
        display_name: str,
        password_hash: str,
        created_at: str,
    ) -> UserRecord:
        """Create and store a new user record.

        Raises
        ------
        ValueError
            If the email is already registered.
        """
        if email in self._users:
            raise ValueError(f"Email already registered: {email}")
        record = UserRecord(
            email=email,
            display_name=display_name,
            password_hash=password_hash,
            created_at=created_at,
            active=True,
        )
        self._users[email] = record
        return record

    async def update_user(self, email: str, **fields: str) -> UserRecord | None:
        """Update fields on an existing user record.

        Returns ``None`` if the user does not exist.
        """
        for key in fields:
            if key not in _ALLOWED_USER_UPDATE_FIELDS:
                raise ValueError(f"Cannot update field: {key!r}")
        user = self._users.get(email)
        if user is None:
            return None
        for key, value in fields.items():
            if hasattr(user, key):
                setattr(user, key, value)
        return user

    async def set_password_hash(self, email: str, password_hash: str) -> bool:
        """Replace the stored password hash for a user."""
        user = self._users.get(email)
        if user is None:
            return False
        user.password_hash = password_hash
        return True

    async def user_exists(self, email: str) -> bool:
        """Return ``True`` if a user with *email* exists."""
        return email in self._users


# ---------------------------------------------------------------------------
# SQL implementation
# ---------------------------------------------------------------------------


class SqlUserStore:
    """ArcadeDB-backed user store using parameterized SQL queries.

    Parameters
    ----------
    db : DatabaseClient
        An initialised database client.
    """

    def __init__(self, db: "DatabaseClient") -> None:
        self._db = db

    async def get_user(self, email: str) -> UserRecord | None:
        """Fetch a user by email."""
        query = (
            "SELECT email, display_name, password_hash, created_at,"
            " last_login_at, active FROM User WHERE email = :email"
        )
        rows = await self._db.execute_sql(query, {"email": email})
        if not rows:
            return None
        row = rows[0]
        return UserRecord(
            email=row["email"],
            display_name=row["display_name"],
            password_hash=row["password_hash"],
            created_at=row["created_at"],
            last_login_at=row.get("last_login_at"),
            active=row.get("active", True),
        )

    async def create_user(
        self,
        email: str,
        display_name: str,
        password_hash: str,
        created_at: str,
    ) -> UserRecord:
        """Insert a new User record via parameterized SQL.

        Raises
        ------
        ValueError
            If the email already exists.
        """
        if await self.user_exists(email):
            raise ValueError(f"Email already registered: {email}")

        query = (
            "INSERT INTO User SET email = :email, display_name = :display_name,"
            " password_hash = :password_hash, created_at = sysdate(), active = true"
        )
        await self._db.execute_sql(
            query,
            {
                "email": email,
                "display_name": display_name,
                "password_hash": password_hash,
            },
        )
        return UserRecord(
            email=email,
            display_name=display_name,
            password_hash=password_hash,
            created_at=created_at,
            active=True,
        )

    async def update_user(self, email: str, **fields: str) -> UserRecord | None:
        """Update properties on an existing User record."""
        for key in fields:
            if key not in _ALLOWED_USER_UPDATE_FIELDS:
                raise ValueError(f"Cannot update field: {key!r}")

        if not fields:
            return await self.get_user(email)

        set_parts: list[str] = []
        params: dict[str, Any] = {"email": email}
        for key, value in fields.items():
            set_parts.append(f"{key} = :{key}")
            params[key] = value

        set_clause = ", ".join(set_parts)
        query = f"UPDATE User SET {set_clause} WHERE email = :email"
        await self._db.execute_sql(query, params)
        return await self.get_user(email)

    async def set_password_hash(self, email: str, password_hash: str) -> bool:
        """Replace the stored password hash for a User record."""
        if not await self.user_exists(email):
            return False
        query = "UPDATE User SET password_hash = :password_hash WHERE email = :email"
        await self._db.execute_sql(query, {"email": email, "password_hash": password_hash})
        return True

    async def user_exists(self, email: str) -> bool:
        """Return whether a User with the given email exists."""
        query = "SELECT count(*) as count FROM User WHERE email = :email"
        rows = await self._db.execute_sql(query, {"email": email})
        return _coerce_count(rows) > 0
