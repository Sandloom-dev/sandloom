"""
Comprehensive tests for UserService with full coverage.
Tests both positive and negative scenarios following best practices.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_identity.events import EventActor
from sandloom_identity.models import User
from sandloom_identity.schemas.user import UserListOut, UserUpdate
from sandloom_identity.services.token_service import TokenService
from sandloom_identity.services.user_service import UserService

_REAL_REVOKE_ALL = TokenService.revoke_all_user_tokens


@pytest.fixture(autouse=True)
def _stub_refresh_revocation():
    """Mock-session tests: deactivation's token revocation is covered on a real
    DB in test_deactivation_kills_sessions below; stub it here so the flush
    accounting of these unit tests stays about the user row itself."""
    with patch(
        "sandloom_identity.services.user_service.TokenService.revoke_all_user_tokens",
        new=AsyncMock(),
    ) as stub:
        yield stub


class TestUserServiceComprehensive:
    """Comprehensive test cases for UserService."""

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        """Mock database session."""
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()
        return mock_session

    @pytest.fixture
    def sample_user(self) -> Mock:
        """Sample user instance."""
        user = Mock(spec=User)
        user.id = uuid4()
        user.email = "test@example.com"
        user.username = "testuser"
        user.full_name = "Test User"
        user.is_active = True
        user.created_at = datetime.now(UTC)
        user.updated_at = datetime.now(UTC)
        user.user_roles = []
        return user

    @pytest.fixture
    def sample_actor(self) -> EventActor:
        """Sample event actor."""
        return EventActor(user_id=uuid4(), ip="127.0.0.1")

    # POSITIVE SCENARIOS - GET USER BY ID
    @pytest.mark.asyncio
    async def test_get_by_id_success(
        self, mock_db: AsyncMock, sample_user: Mock
    ) -> None:
        """Test successful user retrieval by ID."""
        # Mock user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Act
        result = await UserService.get_by_id(sample_user.id, mock_db)

        # Assert
        assert result == sample_user
        mock_db.execute.assert_called_once()

    # NEGATIVE SCENARIOS - GET USER BY ID
    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self, mock_db: AsyncMock) -> None:
        """Test user retrieval by ID when user doesn't exist."""
        user_id = uuid4()

        # Mock no user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await UserService.get_by_id(user_id, mock_db)

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert "User not found" in exc_info.value.detail

    # POSITIVE SCENARIOS - GET USER BY EMAIL
    @pytest.mark.asyncio
    async def test_get_by_email_success(
        self, mock_db: AsyncMock, sample_user: Mock
    ) -> None:
        """Test successful user retrieval by email."""
        # Mock user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Act
        result = await UserService.get_by_email(sample_user.email, mock_db)

        # Assert
        assert result == sample_user
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_by_email_not_found(self, mock_db: AsyncMock) -> None:
        """Test user retrieval by email when user doesn't exist."""
        # Mock no user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act
        result = await UserService.get_by_email("nonexistent@example.com", mock_db)

        # Assert
        assert result is None
        mock_db.execute.assert_called_once()

    # POSITIVE SCENARIOS - LIST USERS
    @pytest.mark.asyncio
    async def test_list_users_success(self, mock_db: AsyncMock) -> None:
        """Test successful user listing."""
        # Create sample users with proper attributes
        user1 = Mock(spec=User)
        user1.id = uuid4()
        user1.email = "user1@example.com"
        user1.full_name = "User One"
        user1.first_name = "User"
        user1.last_name = "One"
        user1.username = "user1"
        user1.is_active = True
        user1.is_superuser = False
        user1.is_verified = True
        user1.created_at = datetime.now(UTC)
        user1.updated_at = datetime.now(UTC)
        user1.last_login_at = None
        user1.email_verified_at = datetime.now(UTC)

        user2 = Mock(spec=User)
        user2.id = uuid4()
        user2.email = "user2@example.com"
        user2.full_name = "User Two"
        user2.first_name = "User"
        user2.last_name = "Two"
        user2.username = "user2"
        user2.is_active = True
        user2.is_superuser = False
        user2.is_verified = False
        user2.created_at = datetime.now(UTC)
        user2.updated_at = datetime.now(UTC)
        user2.last_login_at = datetime.now(UTC)
        user2.email_verified_at = None

        users = [user1, user2]

        # Mock count query
        count_result = Mock()
        count_result.scalar.return_value = 2

        # Mock users query
        users_result = Mock()
        users_scalars = Mock()
        users_scalars.all.return_value = users
        users_result.scalars.return_value = users_scalars

        mock_db.execute.side_effect = [count_result, users_result]

        # Act
        result = await UserService.list_users(mock_db, skip=0, limit=10)

        # Assert
        assert isinstance(result, UserListOut)
        assert len(result.users) == 2
        assert result.total == 2
        assert result.page == 1
        assert result.per_page == 10

    @pytest.mark.asyncio
    async def test_list_users_with_search(self, mock_db: AsyncMock) -> None:
        """Test user listing with search filter."""
        # Mock empty result for search
        count_result = Mock()
        count_result.scalar.return_value = 0

        users_result = Mock()
        users_scalars = Mock()
        users_scalars.all.return_value = []
        users_result.scalars.return_value = users_scalars

        mock_db.execute.side_effect = [count_result, users_result]

        # Act
        result = await UserService.list_users(mock_db, search="nonexistent")

        # Assert
        assert isinstance(result, UserListOut)
        assert len(result.users) == 0
        assert result.total == 0

    @pytest.mark.asyncio
    async def test_list_users_with_active_filter(self, mock_db: AsyncMock) -> None:
        """Test user listing with is_active filter."""
        # Mock result
        count_result = Mock()
        count_result.scalar.return_value = 1

        users_result = Mock()
        users_scalars = Mock()
        users_scalars.all.return_value = []
        users_result.scalars.return_value = users_scalars

        mock_db.execute.side_effect = [count_result, users_result]

        # Act
        result = await UserService.list_users(mock_db, is_active=True)

        # Assert
        assert isinstance(result, UserListOut)
        assert result.total == 1

    @pytest.mark.asyncio
    async def test_update_user_success(
        self, mock_db: AsyncMock, sample_user: Mock, sample_actor: EventActor
    ) -> None:
        """Test successful user update."""
        update_data = UserUpdate(
            full_name="Updated Name",
            username="updateduser",
            first_name=None,
            last_name=None,
        )

        # Mock get_by_id
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Act
        result = await UserService.update_user(
            sample_user.id, update_data, mock_db, sample_actor
        )

        # Assert
        assert result == sample_user
        assert sample_user.full_name == "Updated Name"
        assert sample_user.username == "updateduser"
        assert isinstance(sample_user.updated_at, datetime)
        mock_db.flush.assert_called_once()
        mock_db.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_user_without_actor(
        self, mock_db: AsyncMock, sample_user: Mock
    ) -> None:
        """Test user update without actor."""
        update_data = UserUpdate(
            full_name="Updated Name", first_name=None, last_name=None, username=None
        )

        # Mock get_by_id
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Act
        result = await UserService.update_user(sample_user.id, update_data, mock_db)

        # Assert
        assert result == sample_user
        assert sample_user.full_name == "Updated Name"
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_user_partial_update(
        self, mock_db: AsyncMock, sample_user: Mock
    ) -> None:
        """Test partial user update (only one field)."""
        update_data = UserUpdate(
            username="newusername", full_name=None, first_name=None, last_name=None
        )

        # Mock get_by_id
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Act
        result = await UserService.update_user(sample_user.id, update_data, mock_db)

        # Assert
        assert result == sample_user
        assert sample_user.username == "newusername"
        mock_db.flush.assert_called_once()

    # NEGATIVE SCENARIOS - UPDATE USER
    @pytest.mark.asyncio
    async def test_update_user_not_found(self, mock_db: AsyncMock) -> None:
        """Test updating non-existent user fails."""
        user_id = uuid4()
        update_data = UserUpdate(
            full_name="Updated Name", first_name=None, last_name=None, username=None
        )

        # Mock user not found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await UserService.update_user(user_id, update_data, mock_db)

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    # POSITIVE SCENARIOS - DELETE USER
    @pytest.mark.asyncio
    async def test_delete_user_success(
        self, mock_db: AsyncMock, sample_user: Mock, sample_actor: EventActor
    ) -> None:
        """Test successful user deletion (soft delete)."""
        # Mock get_by_id
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Act
        await UserService.delete_user(sample_user.id, mock_db, sample_actor)

        # Assert
        assert sample_user.is_active is False
        assert isinstance(sample_user.updated_at, datetime)
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_user_without_actor(
        self, mock_db: AsyncMock, sample_user: Mock
    ) -> None:
        """Test user deletion without actor."""
        # Mock get_by_id
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Act
        await UserService.delete_user(sample_user.id, mock_db)

        # Assert
        assert sample_user.is_active is False
        mock_db.flush.assert_called_once()

    # NEGATIVE SCENARIOS - DELETE USER
    @pytest.mark.asyncio
    async def test_delete_user_not_found(self, mock_db: AsyncMock) -> None:
        """Test deleting non-existent user fails."""
        user_id = uuid4()

        # Mock user not found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await UserService.delete_user(user_id, mock_db)

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    # POSITIVE SCENARIOS - ACTIVATE USER
    @pytest.mark.asyncio
    async def test_activate_user_success(
        self, mock_db: AsyncMock, sample_user: Mock, sample_actor: EventActor
    ) -> None:
        """Test successful user activation."""
        sample_user.is_active = False  # Start as inactive

        # Mock get_by_id
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Act
        result = await UserService.activate_user(sample_user.id, mock_db, sample_actor)

        # Assert
        assert result == sample_user
        assert sample_user.is_active is True
        assert isinstance(sample_user.updated_at, datetime)
        mock_db.flush.assert_called_once()
        mock_db.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_activate_user_without_actor(
        self, mock_db: AsyncMock, sample_user: Mock
    ) -> None:
        """Test user activation without actor."""
        sample_user.is_active = False

        # Mock get_by_id
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Act
        result = await UserService.activate_user(sample_user.id, mock_db)

        # Assert
        assert result == sample_user
        assert sample_user.is_active is True
        mock_db.flush.assert_called_once()

    # NEGATIVE SCENARIOS - ACTIVATE USER
    @pytest.mark.asyncio
    async def test_activate_user_not_found(self, mock_db: AsyncMock) -> None:
        """Test activating non-existent user fails."""
        user_id = uuid4()

        # Mock user not found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await UserService.activate_user(user_id, mock_db)

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    # POSITIVE SCENARIOS - DEACTIVATE USER
    @pytest.mark.asyncio
    async def test_deactivate_user_success(
        self, mock_db: AsyncMock, sample_user: Mock, sample_actor: EventActor
    ) -> None:
        """Test successful user deactivation."""
        sample_user.is_active = True  # Start as active

        # Mock get_by_id
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Act
        result = await UserService.deactivate_user(
            sample_user.id, mock_db, sample_actor
        )

        # Assert
        assert result == sample_user
        assert sample_user.is_active is False
        assert isinstance(sample_user.updated_at, datetime)
        mock_db.flush.assert_called_once()
        mock_db.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_deactivate_user_without_actor(
        self, mock_db: AsyncMock, sample_user: Mock
    ) -> None:
        """Test user deactivation without actor."""
        sample_user.is_active = True

        # Mock get_by_id
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Act
        result = await UserService.deactivate_user(sample_user.id, mock_db)

        # Assert
        assert result == sample_user
        assert sample_user.is_active is False
        mock_db.flush.assert_called_once()

    # NEGATIVE SCENARIOS - DEACTIVATE USER
    @pytest.mark.asyncio
    async def test_deactivate_user_not_found(self, mock_db: AsyncMock) -> None:
        """Test deactivating non-existent user fails."""
        user_id = uuid4()

        # Mock user not found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await UserService.deactivate_user(user_id, mock_db)

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    # UTILITY METHODS
    @pytest.mark.asyncio
    async def test_get_instance(self) -> None:
        """Test service instance creation."""
        instance = UserService.get_instance()
        assert isinstance(instance, UserService)

    # EDGE CASES AND INTEGRATION SCENARIOS
    @pytest.mark.asyncio
    async def test_list_users_pagination(self, mock_db: AsyncMock) -> None:
        """Test user listing with different pagination parameters."""
        # Mock result
        count_result = Mock()
        count_result.scalar.return_value = 25

        users_result = Mock()
        users_scalars = Mock()
        users_scalars.all.return_value = []
        users_result.scalars.return_value = users_scalars

        mock_db.execute.side_effect = [count_result, users_result]

        # Act
        result = await UserService.list_users(mock_db, skip=10, limit=5)

        # Assert
        assert result.total == 25
        assert result.page == 3  # (10 // 5) + 1 = 3
        assert result.per_page == 5

    @pytest.mark.asyncio
    async def test_update_user_empty_data(
        self, mock_db: AsyncMock, sample_user: Mock
    ) -> None:
        """Test user update with no changes."""
        update_data = UserUpdate(
            full_name=None, first_name=None, last_name=None, username=None
        )  # Empty update

        # Mock get_by_id
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Act
        result = await UserService.update_user(sample_user.id, update_data, mock_db)

        # Assert
        assert result == sample_user
        # Should still update timestamp
        assert isinstance(sample_user.updated_at, datetime)
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_user_operations_consistency(
        self, mock_db: AsyncMock, sample_user: Mock
    ) -> None:
        """Test consistency across multiple user operations."""
        # Mock get_by_id for multiple operations
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # First deactivate
        await UserService.deactivate_user(sample_user.id, mock_db)
        assert sample_user.is_active is False

        # Then activate
        await UserService.activate_user(sample_user.id, mock_db)
        assert sample_user.is_active is True

        # Then soft delete
        await UserService.delete_user(sample_user.id, mock_db)
        assert sample_user.is_active is False

        # Should have called commit 3 times
        assert mock_db.flush.call_count == 3


@pytest.mark.asyncio
async def test_deactivation_kills_sessions(test_db, make_user, _ambient_uow) -> None:
    """Deactivating a user revokes refresh tokens, marks access tokens revoked
    (after the commit) and writes an audit record (no PII)."""
    from sqlalchemy import select

    from sandloom.sdk.testing import run_on_commit
    from sandloom_identity.models.refresh_token import RefreshToken
    from sandloom_identity.services.token_service import TokenService as RealTokens
    from sandloom_identity.sessions import is_token_revoked

    user = await make_user("deact@example.com")
    pair = await RealTokens.create_tokens(user, test_db)
    await test_db.commit()

    with (
        patch(
            "sandloom_identity.services.user_service.TokenService.revoke_all_user_tokens",
            new=_REAL_REVOKE_ALL,
        ),
        patch("sandloom_identity.services.user_service.record_audit") as audit,
    ):
        await UserService.delete_user(user.id, test_db, EventActor(user_id=uuid4()))
    await test_db.commit()
    # The access-token revocation mark is a post-commit effect (on_commit).
    await run_on_commit(_ambient_uow)
    test_db.expire_all()

    rows = (await test_db.execute(select(RefreshToken))).scalars().all()
    assert rows and all(r.is_revoked for r in rows)
    assert audit.call_args.args[0] == "identity.user.deactivated"
    assert "email" not in str(audit.call_args.kwargs.get("data"))
    from sandloom.sdk.security import decode_token

    claims = decode_token(pair.access_token)
    assert claims is not None
    claims["iat"] = claims["iat"] - 5  # minted before the revocation
    assert await is_token_revoked(claims) is True
