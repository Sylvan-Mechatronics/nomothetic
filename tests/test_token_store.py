"""Tests for token store implementations."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from nomothetic.token_store import InMemoryTokenStore, SqlTokenStore

# ============================================================================
# InMemoryTokenStore
# ============================================================================


class TestInMemoryTokenStore:
    """Tests for the in-memory token store."""

    @pytest.fixture
    def store(self):
        return InMemoryTokenStore()

    @pytest.mark.asyncio
    async def test_store_and_retrieve(self, store):
        """store_token + get_email returns correct email."""
        expires = datetime.now(UTC) + timedelta(hours=1)
        await store.store_token("hash1", "alice@example.com", expires)
        assert await store.get_email("hash1") == "alice@example.com"

    @pytest.mark.asyncio
    async def test_get_email_unknown(self, store):
        """get_email returns None for unknown token hash."""
        assert await store.get_email("nonexistent") is None

    @pytest.mark.asyncio
    async def test_get_email_expired(self, store):
        """get_email returns None for expired token."""
        expired = datetime.now(UTC) - timedelta(hours=1)
        await store.store_token("expired_hash", "bob@example.com", expired)
        assert await store.get_email("expired_hash") is None

    @pytest.mark.asyncio
    async def test_delete_token(self, store):
        """delete_token returns True and removes the token."""
        expires = datetime.now(UTC) + timedelta(hours=1)
        await store.store_token("del_hash", "alice@example.com", expires)
        assert await store.delete_token("del_hash") is True
        assert await store.get_email("del_hash") is None

    @pytest.mark.asyncio
    async def test_delete_token_unknown(self, store):
        """delete_token returns False for unknown hash."""
        assert await store.delete_token("noexist") is False

    @pytest.mark.asyncio
    async def test_delete_tokens_for_user(self, store):
        """delete_tokens_for_user deletes all tokens for email, returns count."""
        expires = datetime.now(UTC) + timedelta(hours=1)
        await store.store_token("h1", "alice@example.com", expires)
        await store.store_token("h2", "alice@example.com", expires)
        await store.store_token("h3", "bob@example.com", expires)
        count = await store.delete_tokens_for_user("alice@example.com")
        assert count == 2
        assert await store.get_email("h1") is None
        assert await store.get_email("h2") is None
        assert await store.get_email("h3") == "bob@example.com"

    @pytest.mark.asyncio
    async def test_cleanup_expired(self, store):
        """cleanup_expired removes only expired tokens, returns count."""
        future = datetime.now(UTC) + timedelta(hours=1)
        past = datetime.now(UTC) - timedelta(hours=1)
        await store.store_token("valid", "alice@example.com", future)
        await store.store_token("stale1", "bob@example.com", past)
        await store.store_token("stale2", "carol@example.com", past)
        count = await store.cleanup_expired()
        assert count == 2
        assert await store.get_email("valid") == "alice@example.com"


# ============================================================================
# SqlTokenStore
# ============================================================================


class TestSqlTokenStore:
    """Tests for the ArcadeDB-backed token store."""

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.execute_sql = AsyncMock()
        return db

    @pytest.fixture
    def store(self, mock_db):
        return SqlTokenStore(mock_db)

    @pytest.mark.asyncio
    async def test_sql_store_and_retrieve(self, store, mock_db):
        """store_token sends INSERT; get_email returns email from SELECT."""
        mock_db.execute_sql.side_effect = [
            [],  # store_token result
            [
                {
                    "email": "alice@example.com",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                }
            ],  # get_email result
        ]
        expires = datetime.now(UTC) + timedelta(hours=1)
        await store.store_token("hash1", "alice@example.com", expires)
        email = await store.get_email("hash1")
        assert email == "alice@example.com"
        first_call = mock_db.execute_sql.call_args_list[0][0][0]
        assert "INSERT INTO RefreshToken" in first_call

    @pytest.mark.asyncio
    async def test_sql_get_email_not_found(self, store, mock_db):
        """get_email returns None when no row matches."""
        mock_db.execute_sql.return_value = []
        assert await store.get_email("unknown_hash") is None

    @pytest.mark.asyncio
    async def test_sql_delete_token(self, store, mock_db):
        """delete_token returns True and sends DELETE query."""
        mock_db.execute_sql.side_effect = [
            [{"count": 1}],  # count check
            [],  # DELETE result
        ]
        assert await store.delete_token("hash1") is True
        assert mock_db.execute_sql.call_count == 2
        delete_query = mock_db.execute_sql.call_args_list[1][0][0]
        assert "DELETE FROM RefreshToken" in delete_query

    @pytest.mark.asyncio
    async def test_sql_delete_token_not_found(self, store, mock_db):
        """delete_token returns False when count is 0."""
        mock_db.execute_sql.return_value = [{"count": 0}]
        assert await store.delete_token("nonexistent") is False

    @pytest.mark.asyncio
    async def test_sql_delete_tokens_for_user(self, store, mock_db):
        """delete_tokens_for_user sends count + DELETE queries."""
        mock_db.execute_sql.side_effect = [
            [{"count": 3}],  # count
            [],  # DELETE
        ]
        count = await store.delete_tokens_for_user("alice@example.com")
        assert count == 3
        assert mock_db.execute_sql.call_count == 2

    @pytest.mark.asyncio
    async def test_sql_cleanup_expired(self, store, mock_db):
        """cleanup_expired sends count + DELETE for expired tokens."""
        mock_db.execute_sql.side_effect = [
            [{"count": 5}],  # count
            [],  # DELETE
        ]
        count = await store.cleanup_expired()
        assert count == 5
        assert mock_db.execute_sql.call_count == 2
        count_query = mock_db.execute_sql.call_args_list[0][0][0]
        assert "<=" in count_query
