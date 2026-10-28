"""Comprehensive tests for authentication guards and middleware."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_identity.access import (
    get_current_user,
    get_optional_user,
    require_role,
    security,
)
from sandloom_identity.models.user import User


class TestGetCurrentUser:
    """Test the get_current_user guard function."""

    @pytest.mark.asyncio
    async def test_get_current_user_success(self):
        """Test successful user authentication."""
        # Setup
        mock_db = AsyncMock(spec=AsyncSession)
        mock_user = User(
            id=uuid4(), email="test@example.com", full_name="Test User", is_active=True
        )
        mock_credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="valid_token"
        )

        # Mock AuthService.get_current_user
        with patch(
            "sandloom_identity.access.AuthService.get_current_user"
        ) as mock_auth:
            mock_auth.return_value = mock_user

            # Execute
            result = await get_current_user(mock_credentials, mock_db)

            # Verify
            assert result == mock_user
            mock_auth.assert_called_once_with("valid_token", mock_db)

    @pytest.mark.asyncio
    async def test_get_current_user_no_credentials(self):
        """Test authentication failure when no credentials provided."""
        # Setup
        mock_db = AsyncMock(spec=AsyncSession)

        # Execute & Verify
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(None, mock_db)

        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert exc_info.value.detail == "Authentication required"
        assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}

    @pytest.mark.asyncio
    async def test_get_current_user_invalid_token(self):
        """Test authentication failure with invalid token."""
        # Setup
        mock_db = AsyncMock(spec=AsyncSession)
        mock_credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="invalid_token"
        )

        # Mock AuthService.get_current_user to return None
        with patch(
            "sandloom_identity.access.AuthService.get_current_user"
        ) as mock_auth:
            mock_auth.return_value = None

            # Execute & Verify
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(mock_credentials, mock_db)

            assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
            assert exc_info.value.detail == "Invalid token"
            assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}

    @pytest.mark.asyncio
    async def test_get_current_user_auth_service_exception(self):
        """Test authentication failure when AuthService raises exception."""
        # Setup
        mock_db = AsyncMock(spec=AsyncSession)
        mock_credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="error_token"
        )

        # Mock AuthService.get_current_user to raise exception
        with patch(
            "sandloom_identity.access.AuthService.get_current_user"
        ) as mock_auth:
            mock_auth.side_effect = ValueError("Token decode error")

            # Execute & Verify
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(mock_credentials, mock_db)

            assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
            assert exc_info.value.detail == "Invalid token"
            assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


class TestGetOptionalUser:
    """Test the get_optional_user guard function."""

    @pytest.mark.asyncio
    async def test_get_optional_user_success(self):
        """Test successful optional user authentication."""
        # Setup
        mock_db = AsyncMock(spec=AsyncSession)
        mock_user = User(
            id=uuid4(), email="test@example.com", full_name="Test User", is_active=True
        )
        mock_credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="valid_token"
        )

        # Mock AuthService.get_current_user
        with patch(
            "sandloom_identity.access.AuthService.get_current_user"
        ) as mock_auth:
            mock_auth.return_value = mock_user

            # Execute
            result = await get_optional_user(mock_credentials, mock_db)

            # Verify
            assert result == mock_user
            mock_auth.assert_called_once_with("valid_token", mock_db)

    @pytest.mark.asyncio
    async def test_get_optional_user_no_credentials(self):
        """Test optional authentication when no credentials provided."""
        # Setup
        mock_db = AsyncMock(spec=AsyncSession)

        # Execute
        result = await get_optional_user(None, mock_db)

        # Verify
        assert result is None

    @pytest.mark.asyncio
    async def test_get_optional_user_auth_service_exception(self):
        """Test optional authentication when AuthService raises exception."""
        # Setup
        mock_db = AsyncMock(spec=AsyncSession)
        mock_credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="error_token"
        )

        # Mock AuthService.get_current_user to raise exception
        with patch(
            "sandloom_identity.access.AuthService.get_current_user"
        ) as mock_auth:
            mock_auth.side_effect = ValueError("Token decode error")

            # Execute
            result = await get_optional_user(mock_credentials, mock_db)

            # Verify
            assert result is None


class TestRequireRole:
    """Test the require_role decorator function."""

    def test_require_role_returns_decorator(self):
        """Test that require_role returns a decorator function."""
        # Execute
        decorator = require_role("admin")

        # Verify
        assert callable(decorator)

    @pytest.mark.asyncio
    async def test_require_role_success(self):
        """Test successful role requirement check."""
        # Setup
        mock_db = AsyncMock(spec=AsyncSession)
        mock_user = User(
            id=uuid4(),
            email="admin@example.com",
            full_name="Admin User",
            is_active=True,
        )

        # Create the role checker function
        role_checker = require_role("admin")

        # Mock RoleService.user_has_role
        with patch(
            "sandloom_identity.services.role_service.RoleService"
        ) as mock_role_service:
            mock_role_service.user_has_role = AsyncMock(return_value=True)

            # Execute
            result = await role_checker(mock_user, mock_db)

            # Verify
            assert result == mock_user
            mock_role_service.user_has_role.assert_called_once_with(
                mock_user.id, "admin", mock_db
            )

    @pytest.mark.asyncio
    async def test_require_role_forbidden(self):
        """Test role requirement check failure."""
        # Setup
        mock_db = AsyncMock(spec=AsyncSession)
        mock_user = User(
            id=uuid4(),
            email="user@example.com",
            full_name="Regular User",
            is_active=True,
        )

        # Create the role checker function
        role_checker = require_role("admin")

        # Mock RoleService.user_has_role to return False (user doesn't have role)
        with patch(
            "sandloom_identity.services.role_service.RoleService"
        ) as mock_role_service:
            mock_role_service.user_has_role = AsyncMock(return_value=False)

            # Execute & Verify
            with pytest.raises(HTTPException) as exc_info:
                await role_checker(mock_user, mock_db)

            assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
            assert exc_info.value.detail == "Role 'admin' required"
            mock_role_service.user_has_role.assert_called_once_with(
                mock_user.id, "admin", mock_db
            )

    @pytest.mark.asyncio
    async def test_require_role_with_different_roles(self):
        """Test role requirement with different role names."""
        test_cases = [
            ("moderator", "moderator"),
            ("premium_user", "premium_user"),
            ("super_admin", "super_admin"),
        ]

        mock_db = AsyncMock(spec=AsyncSession)
        mock_user = User(
            id=uuid4(), email="user@example.com", full_name="Test User", is_active=True
        )

        for required_role, _ in test_cases:
            # Create the role checker function
            role_checker = require_role(required_role)

            # Mock RoleService.user_has_role to return True
            with patch(
                "sandloom_identity.services.role_service.RoleService"
            ) as mock_role_service:
                mock_role_service.user_has_role = AsyncMock(return_value=True)

                # Execute
                result = await role_checker(mock_user, mock_db)

                # Verify
                assert result == mock_user
                mock_role_service.user_has_role.assert_called_with(
                    mock_user.id, required_role, mock_db
                )


class TestSecurityConfiguration:
    """Test the HTTPBearer security configuration."""

    def test_security_is_http_bearer(self):
        """Test that security is properly configured as HTTPBearer."""
        from fastapi.security import HTTPBearer

        assert isinstance(security, HTTPBearer)
        # HTTPBearer should not auto_error to allow for optional authentication
        assert security.auto_error is False


class TestIntegration:
    """Integration tests for guards working together."""

    @pytest.mark.asyncio
    async def test_full_authentication_flow(self):
        """Test complete authentication and authorization flow."""
        # Setup
        mock_db = AsyncMock(spec=AsyncSession)
        admin_user = User(
            id=uuid4(),
            email="admin@example.com",
            full_name="Admin User",
            is_active=True,
        )
        mock_credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="admin_token"
        )

        # Mock both AuthService and RoleService
        with patch(
            "sandloom_identity.access.AuthService.get_current_user"
        ) as mock_auth:
            with patch(
                "sandloom_identity.services.role_service.RoleService"
            ) as mock_role_service:
                mock_auth.return_value = admin_user
                mock_role_service.user_has_role = AsyncMock(return_value=True)

                # Test 1: Get current user
                authenticated_user = await get_current_user(mock_credentials, mock_db)
                assert authenticated_user == admin_user

                # Test 2: Check role requirement
                admin_role_checker = require_role("admin")
                authorized_user = await admin_role_checker(authenticated_user, mock_db)
                assert authorized_user == admin_user

                # Verify calls
                mock_auth.assert_called_once_with("admin_token", mock_db)
                mock_role_service.user_has_role.assert_called_once_with(
                    admin_user.id, "admin", mock_db
                )

    @pytest.mark.asyncio
    async def test_authentication_failure_prevents_authorization(self):
        """Test that authentication failure prevents role/permission checks."""
        # Setup
        mock_db = AsyncMock(spec=AsyncSession)
        invalid_credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="invalid_token"
        )

        # Mock AuthService to return None (invalid token)
        with patch(
            "sandloom_identity.access.AuthService.get_current_user"
        ) as mock_auth:
            mock_auth.return_value = None

            # Execute & Verify - Authentication should fail
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(invalid_credentials, mock_db)

            assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
            assert exc_info.value.detail == "Invalid token"


class TestUserHasAdminRole:
    """Test the user_has_admin_role helper function."""

    @pytest.mark.asyncio
    async def test_user_has_admin_role_admin_user(self):
        """Test admin user has admin role."""
        from sandloom_identity.access import user_has_admin_role

        # Setup
        mock_db = AsyncMock()
        user_id = uuid4()

        # Mock _user_has_admin_role to return True
        with patch("sandloom_identity.access._user_has_admin_role") as mock_admin_check:
            mock_admin_check.return_value = True

            # Execute
            result = await user_has_admin_role(user_id, mock_db)

            # Verify
            assert result is True
            mock_admin_check.assert_called_once_with(user_id, mock_db)

    @pytest.mark.asyncio
    async def test_user_has_admin_role_regular_user(self):
        """Test regular user does not have admin role."""
        from sandloom_identity.access import user_has_admin_role

        # Setup
        mock_db = AsyncMock()
        user_id = uuid4()

        # Mock _user_has_admin_role to return False
        with patch("sandloom_identity.access._user_has_admin_role") as mock_admin_check:
            mock_admin_check.return_value = False

            # Execute
            result = await user_has_admin_role(user_id, mock_db)

            # Verify
            assert result is False
            mock_admin_check.assert_called_once_with(user_id, mock_db)


class TestRequireAdmin:
    """Test the require_admin function."""

    @pytest.mark.asyncio
    async def test_require_admin_admin_access(self):
        """Test admin user can access."""
        from sandloom_identity.access import require_admin

        # Setup
        admin_user = User(
            id=uuid4(),
            email="admin@example.com",
            full_name="Admin User",
            is_active=True,
        )

        mock_db = AsyncMock()

        # Mock _user_has_admin_role to return True
        with patch("sandloom_identity.access._user_has_admin_role") as mock_admin_check:
            mock_admin_check.return_value = True

            # Execute
            result = await require_admin(admin_user, mock_db)

            # Verify
            assert result == admin_user

    @pytest.mark.asyncio
    async def test_require_admin_superuser_access(self):
        """Test superuser can access."""
        from sandloom_identity.access import require_admin

        # Setup
        superuser = User(
            id=uuid4(),
            email="super@example.com",
            full_name="Super User",
            is_active=True,
            is_superuser=True,
        )

        mock_db = AsyncMock()

        # Execute - superuser should bypass admin check
        result = await require_admin(superuser, mock_db)

        # Verify
        assert result == superuser

    @pytest.mark.asyncio
    async def test_require_admin_forbidden(self):
        """Test regular user cannot access."""
        from sandloom_identity.access import require_admin

        # Setup
        user = User(
            id=uuid4(),
            email="user@example.com",
            full_name="Regular User",
            is_active=True,
        )

        mock_db = AsyncMock()

        # Mock _user_has_admin_role to return False
        with patch("sandloom_identity.access._user_has_admin_role") as mock_admin_check:
            mock_admin_check.return_value = False

            # Execute & Verify
            with pytest.raises(HTTPException) as exc_info:
                await require_admin(user, mock_db)

            assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN


class TestGuardsHelperFunctions:
    """Test helper functions for improved coverage."""

    @pytest.mark.asyncio
    async def test_user_has_admin_role_database_query(self):
        """Test user_has_admin_role database query logic."""
        from sandloom_identity.access import user_has_admin_role
        from sandloom_identity.models.user_role import UserRole

        # Setup
        mock_db = AsyncMock()
        user_id = uuid4()

        # Mock the query result to return an actual result
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none.return_value = UserRole()
        mock_db.execute.return_value = mock_result

        # Execute
        result = await user_has_admin_role(user_id, mock_db)

        # Verify the query was called and result is correct
        mock_db.execute.assert_called_once()
        assert result is True


class TestIsSelfOrAdmin:
    """Test is_self_or_admin decorator."""

    @pytest.mark.asyncio
    async def test_is_self_or_admin_self_access(self):
        """Test is_self_or_admin allows self access."""
        from sandloom_identity.access import is_self_or_admin

        # Arrange
        user_id = uuid4()
        current_user = User(id=user_id, email="user@example.com")

        mock_request = AsyncMock()
        mock_request.path_params = {"user_id": str(user_id)}

        mock_db = AsyncMock()

        # Act
        checker = is_self_or_admin()
        result = await checker(mock_request, current_user, mock_db)

        # Assert
        assert result == current_user

    @pytest.mark.asyncio
    @patch("sandloom_identity.access._user_has_admin_role", new_callable=AsyncMock)
    async def test_is_self_or_admin_admin_access(self, mock_admin_check):
        """Test is_self_or_admin allows admin access to other users."""
        from sandloom_identity.access import is_self_or_admin

        # Arrange
        admin_id = uuid4()
        target_user_id = uuid4()

        admin_user = User(id=admin_id, email="admin@example.com")

        mock_request = AsyncMock()
        mock_request.path_params = {"user_id": str(target_user_id)}

        mock_db = AsyncMock()
        mock_admin_check.return_value = True

        # Act
        checker = is_self_or_admin()
        result = await checker(mock_request, admin_user, mock_db)

        # Assert
        assert result == admin_user
        mock_admin_check.assert_called_once_with(admin_id, mock_db)

    @pytest.mark.asyncio
    @patch("sandloom_identity.access._user_has_admin_role", new_callable=AsyncMock)
    async def test_is_self_or_admin_forbidden(self, mock_admin_check):
        """Test is_self_or_admin denies non-admin access to other users."""
        from sandloom_identity.access import is_self_or_admin

        # Arrange
        user_id = uuid4()
        target_user_id = uuid4()

        regular_user = User(id=user_id, email="user@example.com")

        mock_request = AsyncMock()
        mock_request.path_params = {"user_id": str(target_user_id)}

        mock_db = AsyncMock()
        mock_admin_check.return_value = False

        # Act & Assert
        checker = is_self_or_admin()
        with pytest.raises(HTTPException) as exc_info:
            await checker(mock_request, regular_user, mock_db)

        assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
        assert exc_info.value.detail == "Access denied"
        mock_admin_check.assert_called_once_with(user_id, mock_db)

    @pytest.mark.asyncio
    async def test_is_self_or_admin_custom_param_name(self):
        """Test is_self_or_admin with custom parameter name."""
        from sandloom_identity.access import is_self_or_admin

        # Arrange
        user_id = uuid4()
        current_user = User(id=user_id, email="user@example.com")

        mock_request = AsyncMock()
        mock_request.path_params = {"profile_id": str(user_id)}

        mock_db = AsyncMock()

        # Act
        checker = is_self_or_admin("profile_id")
        result = await checker(mock_request, current_user, mock_db)

        # Assert
        assert result == current_user
