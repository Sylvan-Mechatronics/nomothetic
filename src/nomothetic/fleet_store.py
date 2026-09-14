"""Fleet device persistence layer with in-memory and ArcadeDB backends.

Protocol-based store abstraction for vehicle registration and fleet queries.
"""

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional, runtime_checkable

from pydantic import BaseModel
from typing_extensions import Protocol

from nomothetic.db_utils import coerce_count as _coerce_count

if TYPE_CHECKING:
    from nomothetic.db import DatabaseClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class DeviceItem(BaseModel):
    """Summary device entry returned in list responses."""

    vin: str
    model: str
    firmware_version: Optional[str] = None
    last_seen_at: Optional[str] = None
    registered_at: str
    role: str = "owner"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class FleetStore(Protocol):
    """Abstract interface for fleet device persistence."""

    async def get_devices(self, owner_email: str) -> list[DeviceItem]:
        """List all devices owned by a user.

        Parameters
        ----------
        owner_email : str
            Normalised email of the device owner.

        Returns
        -------
        list[DeviceItem]
        """
        ...  # pragma: no cover

    async def get_device(self, owner_email: str, vin: str) -> Optional[DeviceItem]:
        """Look up a single device by VIN scoped to its owner.

        Parameters
        ----------
        owner_email : str
            Normalised owner email.
        vin : str
            Vehicle identification number.

        Returns
        -------
        DeviceItem or None
        """
        ...  # pragma: no cover

    async def register_device(self, owner_email: str, vin: str, model: str) -> DeviceItem:
        """Register a new device under an owner.

        Parameters
        ----------
        owner_email : str
            Normalised owner email.
        vin : str
            Vehicle identification number.
        model : str
            Vehicle model name.

        Returns
        -------
        DeviceItem
            The newly registered device.

        Raises
        ------
        ValueError
            If the device is already registered for this owner.
        """
        ...  # pragma: no cover

    async def get_device_public_key(self, vin: str) -> Optional[str]:
        """Return the PEM public key pinned to *vin*, or ``None`` (review S-3)."""
        ...  # pragma: no cover

    async def set_device_public_key(self, vin: str, public_key_pem: str) -> None:
        """Pin *public_key_pem* to *vin* (first registration only)."""
        ...  # pragma: no cover

    async def remove_device(self, owner_email: str, vin: str) -> bool:
        """Remove a device from an owner's fleet.

        Parameters
        ----------
        owner_email : str
            Normalised owner email.
        vin : str
            Vehicle identification number.

        Returns
        -------
        bool
            True if the device was found and removed.
        """
        ...  # pragma: no cover

    async def device_exists(self, vin: str) -> bool:
        """Check whether a device with the given VIN exists globally.

        Parameters
        ----------
        vin : str
            Vehicle identification number.

        Returns
        -------
        bool
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryFleetStore:
    """Dict-backed fleet store for testing and development.

    Keyed by ``(email, vin)`` internally to scope devices per user.
    """

    def __init__(self) -> None:
        # {email: {vin: DeviceItem}}
        self._devices: dict[str, dict[str, DeviceItem]] = {}
        self._device_keys: dict[str, str] = {}

    async def get_devices(self, owner_email: str) -> list[DeviceItem]:
        """Return all devices owned by the user."""
        return list(self._devices.get(owner_email, {}).values())

    async def get_device(self, owner_email: str, vin: str) -> Optional[DeviceItem]:
        """Look up a single device by VIN scoped to user."""
        return self._devices.get(owner_email, {}).get(vin)

    async def register_device(self, owner_email: str, vin: str, model: str) -> DeviceItem:
        """Register a device under a user.

        Raises
        ------
        ValueError
            If the device is already registered for this user.
        """
        bucket = self._devices.setdefault(owner_email, {})
        if vin in bucket:
            raise ValueError(f"Device {vin} already registered")
        now = datetime.now(timezone.utc).isoformat()
        item = DeviceItem(
            vin=vin,
            model=model,
            registered_at=now,
            role="owner",
        )
        bucket[vin] = item
        return item

    async def remove_device(self, owner_email: str, vin: str) -> bool:
        """Remove a device from the user's fleet.

        Returns True if the device was found and removed.
        """
        bucket = self._devices.get(owner_email, {})
        return bucket.pop(vin, None) is not None

    async def get_device_public_key(self, vin: str) -> Optional[str]:
        return self._device_keys.get(vin)

    async def set_device_public_key(self, vin: str, public_key_pem: str) -> None:
        self._device_keys[vin] = public_key_pem

    async def device_exists(self, vin: str) -> bool:
        """Check whether any user owns a device with this VIN."""
        for bucket in self._devices.values():
            if vin in bucket:
                return True
        return False


# ---------------------------------------------------------------------------
# SQL implementation
# ---------------------------------------------------------------------------


class SqlFleetStore:
    """ArcadeDB-backed fleet store using parameterized SQL queries.

    Parameters
    ----------
    db : DatabaseClient
        An initialised database client.
    """

    def __init__(self, db: "DatabaseClient") -> None:
        self._db = db

    async def get_devices(self, owner_email: str) -> list[DeviceItem]:
        """List devices owned by the user via OwnsDevice edges."""
        # ArcadeDB does not support WHERE out.property / in.property filtering
        # on edge-class queries.  Traverse from User → outE(OwnsDevice) and
        # access the target Vehicle properties via @in.property.
        query = (
            "SELECT @in.vin as vin, @in.model as model,"
            " @in.firmware_version as firmware_version,"
            " @in.last_seen_at as last_seen_at, role, registered_at"
            " FROM (SELECT expand(outE('OwnsDevice')) FROM User WHERE email = :email)"
        )
        rows = await self._db.execute_sql(query, {"email": owner_email})
        items: list[DeviceItem] = []
        for row in rows:
            items.append(
                DeviceItem(
                    vin=row.get("vin", ""),
                    model=row.get("model", ""),
                    registered_at=row.get("registered_at", ""),
                    role=row.get("role", "owner"),
                    firmware_version=row.get("firmware_version"),
                    last_seen_at=row.get("last_seen_at"),
                )
            )
        return items

    async def get_device(self, owner_email: str, vin: str) -> Optional[DeviceItem]:
        """Fetch a single device by VIN scoped to the owner."""
        query = (
            "SELECT @in.vin as vin, @in.model as model,"
            " @in.firmware_version as firmware_version,"
            " @in.last_seen_at as last_seen_at, role, registered_at"
            " FROM (SELECT expand(outE('OwnsDevice')) FROM User WHERE email = :email)"
            " WHERE @in.vin = :vin LIMIT 1"
        )
        rows = await self._db.execute_sql(query, {"email": owner_email, "vin": vin})
        if not rows:
            return None
        row = rows[0]
        return DeviceItem(
            vin=row.get("vin", ""),
            model=row.get("model", ""),
            registered_at=row.get("registered_at", ""),
            role=row.get("role", "owner"),
            firmware_version=row.get("firmware_version"),
            last_seen_at=row.get("last_seen_at"),
        )

    async def register_device(self, owner_email: str, vin: str, model: str) -> DeviceItem:
        """Create a Vehicle record and OwnsDevice edge.

        Raises
        ------
        ValueError
            If the device is already registered for this owner, or if the
            User vertex cannot be found in the database.
        RuntimeError
            If the OwnsDevice edge was not created (e.g. due to a missing
            Vehicle or User vertex after all checks passed).
        """
        existing = await self.get_device(owner_email, vin)
        if existing is not None:
            raise ValueError(f"Device {vin} already registered")

        # Guard: the User vertex must exist before we attempt edge creation.
        # If absent (e.g. stale JWT from an in-memory session after a store
        # migration), CREATE EDGE silently creates 0 edges and we'd return a
        # spurious 201 while nothing is persisted.
        user_check_q = "SELECT count(*) as count FROM User WHERE email = :email"
        user_rows = await self._db.execute_sql(user_check_q, {"email": owner_email})
        if _coerce_count(user_rows) == 0:
            logger.error(
                "register_device: User vertex not found for email=%s; "
                "cannot create OwnsDevice edge",
                owner_email,
            )
            raise ValueError(
                "User account not found in the database. "
                "Please log out and log in again, then retry registration."
            )

        now = datetime.now(timezone.utc).isoformat()

        # Create Vehicle if it doesn't exist
        count_q = "SELECT count(*) as count FROM Vehicle WHERE vin = :vin"
        rows = await self._db.execute_sql(count_q, {"vin": vin})
        if _coerce_count(rows) == 0:
            insert_v = (
                "INSERT INTO Vehicle SET vin = :vin, model = :model,"
                " firmware_version = '', registered_at = sysdate(), last_seen_at = sysdate()"
            )
            await self._db.execute_sql(insert_v, {"vin": vin, "model": model})
            logger.debug("register_device: created Vehicle record for vin=%s", vin)

        # Create OwnsDevice edge
        edge_q = (
            "CREATE EDGE OwnsDevice FROM (SELECT FROM User WHERE email = :email)"
            " TO (SELECT FROM Vehicle WHERE vin = :vin)"
            " SET role = 'owner', registered_at = sysdate()"
        )
        edge_result = await self._db.execute_sql(edge_q, {"email": owner_email, "vin": vin})
        if not edge_result:
            logger.error(
                "register_device: CREATE EDGE produced 0 results for email=%s vin=%s",
                owner_email,
                vin,
            )
            raise RuntimeError(
                f"Failed to link device {vin} to account. "
                "The Vehicle or User vertex may be missing. Check server logs."
            )
        logger.debug(
            "register_device: OwnsDevice edge created for email=%s vin=%s",
            owner_email,
            vin,
        )

        return DeviceItem(
            vin=vin,
            model=model,
            registered_at=now,
            role="owner",
        )

    async def remove_device(self, owner_email: str, vin: str) -> bool:
        """Remove the OwnsDevice edge (does not delete the Vehicle record)."""
        existing = await self.get_device(owner_email, vin)
        if existing is None:
            return False
        # ArcadeDB does not support DELETE EDGE ... WHERE out.property = ...;
        # delete by selecting the matching edge RIDs first.
        query = (
            "DELETE FROM"
            " (SELECT @rid FROM (SELECT expand(outE('OwnsDevice')) FROM User WHERE email = :email)"
            " WHERE @in.vin = :vin)"
        )
        await self._db.execute_sql(query, {"email": owner_email, "vin": vin})
        return True

    async def get_device_public_key(self, vin: str) -> Optional[str]:
        """Return ``Vehicle.device_public_key`` for *vin* (nomographic V5), or ``None``."""
        query = "SELECT device_public_key FROM Vehicle WHERE vin = :vin"
        rows = await self._db.execute_sql(query, {"vin": vin})
        if not rows:
            return None
        value = rows[0].get("device_public_key")
        return value if isinstance(value, str) and value else None

    async def set_device_public_key(self, vin: str, public_key_pem: str) -> None:
        """Pin the device public key on the Vehicle vertex."""
        query = "UPDATE Vehicle SET device_public_key = :pem WHERE vin = :vin"
        await self._db.execute_sql(query, {"pem": public_key_pem, "vin": vin})

    async def device_exists(self, vin: str) -> bool:
        """Check whether a Vehicle with this VIN exists."""
        query = "SELECT count(*) as count FROM Vehicle WHERE vin = :vin"
        rows = await self._db.execute_sql(query, {"vin": vin})
        return _coerce_count(rows) > 0
