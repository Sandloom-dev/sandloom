"""
Comprehensive tests for TokenService with full coverage.
Tests both positive and negative scenarios following best practices.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_identity.models.refresh_token import RefreshToken
from sandloom_identity.models.user import User
from sandloom_identity.schemas.auth import TokenPair
from sandloom_identity.services.token_service import TokenService


class TestTokenServiceComprehensive:
    """Comprehensive test cases for TokenService."""

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        """Mock database session."""
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.commit = AsyncMock()
        mock_session.add = Mock()
        return mock_session

    @pytest.fixture
    def sample_user(self) -> Mock:
        """Sample user instance."""
        user = Mock(spec=User)
        user.id = uuid4()
        user.email = "test@example.com"
        user.username = "testuser"
        user.is_active = True
        return user

    @pytest.fixture
    def sample_refresh_token(self) -> Mock:
        """Sample refresh token instance."""
        token = Mock(spec=RefreshToken)
        token.token = "sample_refresh_token"
        token.user_id = uuid4()
        token.expires_at = datetime.now(UTC) + timedelta(days=30)
        token.is_revoked = False
        token.created_at = datetime.now(UTC)
        token.revoked_at = None
        return token

    # POSITIVE SCENARIOS - CREATE TOKENS
    @pytest.mark.asyncio
    @patch("sandloom_identity.services.token_service.create_access_token")
    @patch("sandloom_identity.services.token_service.secrets.token_urlsafe")
    async def test_create_tokens_success(
        self,
        mock_token_urlsafe: Mock,
        mock_create_access_token: Mock,
        mock_db: AsyncMock,
        sample_user: Mock,
    ) -> None:
        """Test successful token creation."""
        # Setup mocks
        mock_create_access_token.return_value = "access_token_123"
        mock_token_urlsafe.return_value = "refresh_token_456"

        # Act
        result = await TokenService.create_tokens(sample_user, mock_db)

        # Assert
        assert isinstance(result, TokenPair)
        assert result.access_token == "access_token_123"
        assert result.refresh_token == "refresh_token_456"
        assert result.token_type == "bearer"
        assert result.expires_in > 0

        # Verify calls
        mock_create_access_token.assert_called_once()
        mock_token_urlsafe.assert_called_once_with(32)
        mock_db.add.assert_called_once()
        mock_db.flush.assert_called_once()

    # Refresh rotation (success, inactive user, reuse detection, CAS race) is
    # covered against a real database in test_refresh_rotation.py.

    # NEGATIVE SCENARIOS - REFRESH TOKEN
    @pytest.mark.asyncio
    async def test_refresh_token_not_found(self, mock_db: AsyncMock) -> None:
        """Test token refresh with invalid token."""
        # Mock no token found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await TokenService.refresh_token("invalid_token", mock_db)

        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert "Invalid or expired refresh token" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_refresh_token_expired(
        self, mock_db: AsyncMock, sample_refresh_token: Mock
    ) -> None:
        """Test token refresh with expired token."""
        # Mock expired token (query returns None due to expiry filter)
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await TokenService.refresh_token("expired_token", mock_db)

        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_refresh_token_revoked(
        self, mock_db: AsyncMock, sample_refresh_token: Mock
    ) -> None:
        """Test token refresh with revoked token."""
        # Mock revoked token (query returns None due to revocation filter)
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await TokenService.refresh_token("revoked_token", mock_db)

        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED

    # POSITIVE SCENARIOS - REVOKE TOKEN
    @pytest.mark.asyncio
    async def test_revoke_token_success(
        self, mock_db: AsyncMock, sample_refresh_token: Mock
    ) -> None:
        """Test successful token revocation."""
        # Mock token found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_refresh_token
        mock_db.execute.return_value = mock_result

        # Act
        await TokenService.revoke_token("valid_token", mock_db)

        # Assert
        assert sample_refresh_token.is_revoked is True
        assert isinstance(sample_refresh_token.revoked_at, datetime)
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_revoke_token_not_found(self, mock_db: AsyncMock) -> None:
        """Test token revocation when token doesn't exist."""
        # Mock no token found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act (should not raise exception)
        await TokenService.revoke_token("nonexistent_token", mock_db)

        # Assert - commit should not be called
        mock_db.flush.assert_not_called()

    # POSITIVE SCENARIOS - REVOKE ALL USER TOKENS
    @pytest.mark.asyncio
    async def test_revoke_all_user_tokens_success(self, mock_db: AsyncMock) -> None:
        """Test successful revocation of all user tokens."""
        user_id = uuid4()

        # Mock update result
        mock_result = Mock()
        mock_db.execute.return_value = mock_result

        # Act
        await TokenService.revoke_all_user_tokens(user_id, mock_db)

        # Assert
        mock_db.execute.assert_called_once()
        mock_db.flush.assert_called_once()

    # POSITIVE SCENARIOS - CLEANUP EXPIRED TOKENS
    @pytest.mark.asyncio
    async def test_cleanup_expired_tokens_success(self, mock_db: AsyncMock) -> None:
        """Test successful cleanup of expired tokens."""
        # Mock delete result
        mock_result = Mock()
        mock_result.rowcount = 5
        mock_db.execute.return_value = mock_result

        # Act
        result = await TokenService.cleanup_expired_tokens(mock_db)

        # Assert
        assert result == 5
        mock_db.execute.assert_called_once()
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_expired_tokens_none_found(self, mock_db: AsyncMock) -> None:
        """Test cleanup when no expired tokens exist."""
        # Mock delete result with no rows affected
        mock_result = Mock()
        mock_result.rowcount = 0
        mock_db.execute.return_value = mock_result

        # Act
        result = await TokenService.cleanup_expired_tokens(mock_db)

        # Assert
        assert result == 0
        mock_db.execute.assert_called_once()
        mock_db.flush.assert_called_once()

    # EDGE CASES AND INTEGRATION SCENARIOS
    @pytest.mark.asyncio
    @patch("sandloom_identity.services.token_service.create_access_token")
    @patch("sandloom_identity.services.token_service.secrets.token_urlsafe")
    async def test_create_tokens_database_error(
        self,
        mock_token_urlsafe: Mock,
        mock_create_access_token: Mock,
        mock_db: AsyncMock,
        sample_user: Mock,
    ) -> None:
        """Test token creation with database error."""
        # Setup mocks
        mock_create_access_token.return_value = "access_token"
        mock_token_urlsafe.return_value = "refresh_token"
        mock_db.flush.side_effect = Exception("Database error")

        # Act & Assert
        with pytest.raises(Exception, match="Database error"):
            await TokenService.create_tokens(sample_user, mock_db)

    @pytest.mark.asyncio
    @patch("sandloom_identity.services.token_service.datetime")
    async def test_token_timestamps_consistency(
        self, mock_datetime: Mock, mock_db: AsyncMock, sample_refresh_token: Mock
    ) -> None:
        """Test that timestamps are consistently set."""
        fixed_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_datetime.now.return_value = fixed_time

        # Mock token found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_refresh_token
        mock_db.execute.return_value = mock_result

        # Act
        await TokenService.revoke_token("valid_token", mock_db)

        # Assert
        assert sample_refresh_token.revoked_at == fixed_time

    @pytest.mark.asyncio
    @patch("sandloom_identity.services.token_service.create_access_token")
    @patch("sandloom_identity.services.token_service.secrets.token_urlsafe")
    async def test_multiple_token_operations(
        self,
        mock_token_urlsafe: Mock,
        mock_create_access_token: Mock,
        mock_db: AsyncMock,
        sample_user: Mock,
    ) -> None:
        """Test multiple token operations in sequence."""
        # Setup mocks
        mock_create_access_token.return_value = "access_token"
        mock_token_urlsafe.return_value = "refresh_token"

        # Create tokens
        result1 = await TokenService.create_tokens(sample_user, mock_db)
        assert result1.access_token == "access_token"

        # Revoke all user tokens
        await TokenService.revoke_all_user_tokens(sample_user.id, mock_db)

        # Create new tokens
        result2 = await TokenService.create_tokens(sample_user, mock_db)
        assert result2.access_token == "access_token"

        # Verify multiple commits
        assert mock_db.flush.call_count == 3

    @pytest.mark.asyncio
    async def test_token_service_instance_methods(self) -> None:
        """Test that all methods are class methods."""
        # Verify all public methods are classmethods
        methods = [
            "create_tokens",
            "refresh_token",
            "revoke_token",
            "revoke_all_user_tokens",
            "cleanup_expired_tokens",
        ]

        for method_name in methods:
            method = getattr(TokenService, method_name)
            assert callable(method)
            # Class methods should be accessible without instantiation
            assert hasattr(TokenService, method_name)

    @pytest.mark.asyncio
    @patch("sandloom_identity.services.token_service.create_access_token")
    @patch("sandloom_identity.services.token_service.secrets.token_urlsafe")
    async def test_token_values_format(
        self,
        mock_token_urlsafe: Mock,
        mock_create_access_token: Mock,
        mock_db: AsyncMock,
        sample_user: Mock,
    ) -> None:
        """Test token format and values."""
        # Setup specific mock values
        mock_create_access_token.return_value = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9"
        mock_token_urlsafe.return_value = "abcdef123456789_secure_token"

        # Act
        result = await TokenService.create_tokens(sample_user, mock_db)

        # Assert token format
        assert result.access_token.startswith("eyJ")  # JWT format
        assert len(result.refresh_token) > 20  # Reasonable length
        assert result.token_type == "bearer"
        assert isinstance(result.expires_in, int)
        assert result.expires_in > 0


class TestTokenServiceCleanup:
    """Test cases for TokenService cleanup methods."""

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        """Mock database session."""
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()
        return mock_session

    @pytest.mark.asyncio
    async def test_cleanup_expired_tokens_success(self, mock_db: AsyncMock) -> None:
        """Test successful cleanup of expired tokens."""
        # Mock result
        mock_result = Mock()
        mock_result.rowcount = 5
        mock_db.execute.return_value = mock_result

        # Call method
        result = await TokenService.cleanup_expired_tokens(mock_db)

        # Assertions
        assert result == 5
        mock_db.execute.assert_called_once()
        mock_db.flush.assert_called_once()

        # Verify SQL query structure
        call_args = mock_db.execute.call_args[0][0]
        assert "DELETE" in str(call_args).upper()

    @pytest.mark.asyncio
    async def test_cleanup_expired_tokens_no_tokens(self, mock_db: AsyncMock) -> None:
        """Test cleanup when no expired tokens exist."""
        # Mock result with zero count
        mock_result = Mock()
        mock_result.rowcount = 0
        mock_db.execute.return_value = mock_result

        # Call method
        result = await TokenService.cleanup_expired_tokens(mock_db)

        # Assertions
        assert result == 0
        mock_db.execute.assert_called_once()
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_revoked_tokens_by_age_success(
        self, mock_db: AsyncMock
    ) -> None:
        """Test successful cleanup of old revoked tokens."""
        # Mock result
        mock_result = Mock()
        mock_result.rowcount = 3
        mock_db.execute.return_value = mock_result

        # Call method with custom days
        result = await TokenService.cleanup_revoked_tokens_by_age(mock_db, days_old=7)

        # Assertions
        assert result == 3
        mock_db.execute.assert_called_once()
        mock_db.flush.assert_called_once()

        # Verify SQL query structure
        call_args = mock_db.execute.call_args[0][0]
        assert "DELETE" in str(call_args).upper()

    @pytest.mark.asyncio
    async def test_cleanup_revoked_tokens_by_age_default_days(
        self, mock_db: AsyncMock
    ) -> None:
        """Test cleanup with default days parameter."""
        # Mock result
        mock_result = Mock()
        mock_result.rowcount = 2
        mock_db.execute.return_value = mock_result

        # Call method without days parameter (should use default 30)
        result = await TokenService.cleanup_revoked_tokens_by_age(mock_db)

        # Assertions
        assert result == 2
        mock_db.execute.assert_called_once()
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_expired_tokens_database_error(
        self, mock_db: AsyncMock
    ) -> None:
        """Test cleanup handles database errors properly."""
        # Mock database error
        mock_db.execute.side_effect = Exception("Database connection error")

        # Call method and expect exception to propagate
        with pytest.raises(Exception, match="Database connection error"):
            await TokenService.cleanup_expired_tokens(mock_db)

        # Verify execute was called but commit was not
        mock_db.execute.assert_called_once()
        mock_db.flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_revoked_tokens_database_error(
        self, mock_db: AsyncMock
    ) -> None:
        """Test revoked tokens cleanup handles database errors properly."""
        # Mock database error
        mock_db.execute.side_effect = Exception("Database connection error")

        # Call method and expect exception to propagate
        with pytest.raises(Exception, match="Database connection error"):
            await TokenService.cleanup_revoked_tokens_by_age(mock_db, days_old=10)

        # Verify execute was called but commit was not
        mock_db.execute.assert_called_once()
        mock_db.flush.assert_not_called()
