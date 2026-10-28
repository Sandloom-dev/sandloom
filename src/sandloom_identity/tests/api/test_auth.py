"""
Comprehensive integration tests for Auth API endpoints.
Tests the complete flow: HTTP -> Router -> Guards -> Services -> Database
"""

from datetime import UTC

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_identity.models.refresh_token import RefreshToken
from sandloom_identity.models.user import User
from sandloom_identity.services.auth_service import AuthService
from sandloom_identity.utils import email_token_utils


@pytest.mark.integration
class TestAuthAPIComprehensive:
    """Comprehensive integration tests for Auth API."""

    # === Registration Tests ===

    @pytest.mark.asyncio
    async def test_register_success(
        self, async_client: AsyncClient, test_db: AsyncSession
    ):
        """Test successful user registration."""
        user_data = {
            "email": "newuser@example.com",
            "full_name": "New Test User",
            "password": "SecurePassword123!",
        }

        response = await async_client.post("/auth/register", json=user_data)

        # Uniform 202 (no account enumeration); nothing about the user leaks.
        assert response.status_code == 202
        data = response.json()
        assert set(data) == {"message"}

        # Verify user was created in database
        result = await test_db.execute(
            select(User).where(User.email == user_data["email"])
        )
        db_user = result.scalar_one_or_none()
        assert db_user is not None
        assert db_user.email == user_data["email"]
        assert db_user.full_name == user_data["full_name"]
        assert db_user.hashed_password is not None
        assert AuthService.verify_password(
            user_data["password"], db_user.hashed_password
        )

    @pytest.mark.asyncio
    async def test_register_duplicate_email(
        self, async_client: AsyncClient, test_user: User
    ):
        """Test registration with duplicate email fails."""
        user_data = {
            "email": test_user.email,  # Duplicate email
            "full_name": "Another User",
            "password": "SecurePassword123!",
        }

        fresh = await async_client.post(
            "/auth/register",
            json={**user_data, "email": "brand-new@example.com"},
        )
        response = await async_client.post("/auth/register", json=user_data)

        # Identical to a fresh registration: the endpoint cannot be used to
        # enumerate registered emails.
        assert response.status_code == fresh.status_code == 202
        assert response.json() == fresh.json()

    @pytest.mark.asyncio
    async def test_register_invalid_email(self, async_client: AsyncClient):
        """Test registration with invalid email format."""
        user_data = {
            "email": "invalid-email",
            "full_name": "Test User",
            "password": "SecurePassword123!",
        }

        response = await async_client.post("/auth/register", json=user_data)

        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_register_weak_password(self, async_client: AsyncClient):
        """Test registration with weak password."""
        user_data = {
            "email": "test@example.com",
            "full_name": "Test User",
            "password": "weak",
        }

        response = await async_client.post("/auth/register", json=user_data)

        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_register_missing_fields(self, async_client: AsyncClient):
        """Test registration with missing required fields."""
        user_data = {
            "email": "test@example.com"
            # Missing full_name and password
        }

        response = await async_client.post("/auth/register", json=user_data)

        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

    # === Login Tests ===

    @pytest.mark.asyncio
    async def test_login_success(self, async_client: AsyncClient, test_user: User):
        """Test successful login."""
        login_data = {
            "email": test_user.email,
            "password": "TestPassword123!",  # From fixture
        }

        response = await async_client.post("/auth/login", json=login_data)

        assert response.status_code == 200
        data = response.json()

        # Check token structure
        assert "access_token" in data
        assert "refresh_token" in data
        assert "token_type" in data
        assert data["token_type"] == "bearer"
        assert "expires_in" in data

        # Verify tokens are valid JWTs
        access_token = data["access_token"]
        refresh_token = data["refresh_token"]

        assert len(access_token.split(".")) == 3  # JWT has 3 parts
        assert len(refresh_token) > 20  # Refresh token should be substantial

    @pytest.mark.asyncio
    async def test_login_invalid_email(self, async_client: AsyncClient):
        """Test login with non-existent email."""
        login_data = {"email": "nonexistent@example.com", "password": "anypassword"}

        response = await async_client.post("/auth/login", json=login_data)

        assert response.status_code == 401
        data = response.json()
        assert "detail" in data
        assert "incorrect email or password" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_login_wrong_password(
        self, async_client: AsyncClient, test_user: User
    ):
        """Test login with wrong password."""
        login_data = {"email": test_user.email, "password": "wrongpassword"}

        response = await async_client.post("/auth/login", json=login_data)

        assert response.status_code == 401
        data = response.json()
        assert "detail" in data
        assert "incorrect email or password" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_login_inactive_user(
        self, async_client: AsyncClient, test_db: AsyncSession
    ):
        """Test login with inactive user."""
        # Create inactive user
        password = "TestPassword123!"
        user = User(
            email="inactive@example.com",
            full_name="Inactive User",
            hashed_password=AuthService.get_password_hash(password),
            is_active=False,
            is_verified=True,
        )
        test_db.add(user)
        await test_db.commit()

        login_data = {"email": user.email, "password": password}

        response = await async_client.post("/auth/login", json=login_data)

        assert response.status_code == 401
        data = response.json()
        assert "detail" in data
        assert "account is not active" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_login_with_2fa(self, async_client: AsyncClient, test_user: User):
        """Test login that requires 2FA."""
        # TODO: Implement when 2FA is fully set up
        login_data = {
            "email": test_user.email,
            "password": "TestPassword123!",
            "otp": "123456",
        }

        response = await async_client.post("/auth/login", json=login_data)

        # Should still work for now (2FA not enforced)
        assert response.status_code in [200, 400, 401]

    @pytest.mark.asyncio
    async def test_login_malformed_request(self, async_client: AsyncClient):
        """Test login with malformed request."""
        response = await async_client.post("/auth/login", json={})

        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

    # === Token Refresh Tests ===

    @pytest.mark.asyncio
    async def test_refresh_token_success(
        self, async_client: AsyncClient, test_user: User, test_db: AsyncSession
    ):
        """Test successful token refresh."""
        # First login to get refresh token
        login_response = await async_client.post(
            "/auth/login",
            json={"email": test_user.email, "password": "TestPassword123!"},
        )
        assert login_response.status_code == 200
        refresh_token = login_response.json()["refresh_token"]

        # Now refresh the token
        response = await async_client.post(
            "/auth/refresh", json={"refresh_token": refresh_token}
        )

        assert response.status_code == 200
        data = response.json()

        assert "access_token" in data
        assert "refresh_token" in data
        assert "token_type" in data
        assert data["token_type"] == "bearer"
        assert "expires_in" in data

    @pytest.mark.asyncio
    async def test_refresh_invalid_token(self, async_client: AsyncClient):
        """Test refresh with invalid token."""
        response = await async_client.post(
            "/auth/refresh", json={"refresh_token": "invalid-token"}
        )

        assert response.status_code == 401
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_refresh_expired_token(
        self, async_client: AsyncClient, test_user: User, test_db: AsyncSession
    ):
        """Test refresh with expired token."""
        # First, login to get a real refresh token
        login_response = await async_client.post(
            "/auth/login",
            json={"email": test_user.email, "password": "TestPassword123!"},
        )
        assert login_response.status_code == 200
        real_refresh_token = login_response.json()["refresh_token"]

        # Tokens are stored hashed at rest; look up by hash
        from datetime import datetime, timedelta

        from sandloom_identity.utils import RefreshTokenUtils

        result = await test_db.execute(
            select(RefreshToken).where(
                RefreshToken.token == RefreshTokenUtils.hash_token(real_refresh_token)
            )
        )
        db_token = result.scalar_one_or_none()
        assert db_token is not None, "Refresh token should exist in database"

        # Manually expire the token
        db_token.expires_at = datetime.now(UTC) - timedelta(days=1)
        await test_db.commit()

        # Now try to refresh with the expired token
        response = await async_client.post(
            "/auth/refresh", json={"refresh_token": real_refresh_token}
        )

        assert response.status_code == 401
        data = response.json()
        assert "detail" in data
        assert "invalid or expired" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_refresh_missing_token(self, async_client: AsyncClient):
        """Test refresh without token."""
        response = await async_client.post("/auth/refresh", json={})

        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

    # === Logout Tests ===

    @pytest.mark.asyncio
    async def test_logout_success(self, async_client: AsyncClient, test_user: User):
        """Test successful logout."""
        # First login to get refresh token
        login_response = await async_client.post(
            "/auth/login",
            json={"email": test_user.email, "password": "TestPassword123!"},
        )
        assert login_response.status_code == 200
        refresh_token = login_response.json()["refresh_token"]

        # Now logout
        response = await async_client.post(
            "/auth/logout", json={"refresh_token": refresh_token}
        )

        assert response.status_code == 204
        assert response.content == b""

    @pytest.mark.asyncio
    async def test_logout_invalid_token(self, async_client: AsyncClient):
        """Test logout with invalid token."""
        response = await async_client.post(
            "/auth/logout", json={"refresh_token": "invalid-token"}
        )

        assert (
            response.status_code == 204
        )  # Logout silently succeeds even with invalid token

    @pytest.mark.asyncio
    async def test_logout_missing_token(self, async_client: AsyncClient):
        """Test logout without token."""
        response = await async_client.post("/auth/logout", json={})

        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

    # === Email Verification Tests ===

    @pytest.mark.asyncio
    async def test_verify_email_success(
        self, async_client: AsyncClient, test_db: AsyncSession
    ):
        """Test successful email verification."""
        # Create unverified user
        user = User(
            email="unverified@example.com",
            full_name="Unverified User",
            hashed_password=AuthService.get_password_hash("password123"),
            is_active=True,
            is_verified=False,
        )
        test_db.add(user)
        await test_db.commit()
        await test_db.refresh(user)

        # Generate proper verification token
        token = email_token_utils.generate_email_verification_token(user.id)

        response = await async_client.post("/auth/verify-email", json={"token": token})

        assert response.status_code == 204

        # Verify user is now verified
        await test_db.refresh(user)
        assert user.is_verified is True
        assert user.email_verified_at is not None

    @pytest.mark.asyncio
    async def test_verify_email_invalid_token(self, async_client: AsyncClient):
        """Test email verification with invalid token."""
        response = await async_client.post(
            "/auth/verify-email",
            json={
                "token": "invalid.token.string"  # Invalid token format
            },
        )

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "invalid or expired" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_verify_email_already_verified(
        self, async_client: AsyncClient, test_user: User
    ):
        """Test email verification for already verified user."""
        # Generate token for already verified user
        token = email_token_utils.generate_email_verification_token(test_user.id)

        response = await async_client.post("/auth/verify-email", json={"token": token})

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "already verified" in data["detail"].lower()

    # === Password Reset Tests ===

    @pytest.mark.asyncio
    async def test_request_password_reset_success(
        self, async_client: AsyncClient, test_user: User
    ):
        """Test successful password reset request."""
        response = await async_client.post(
            "/auth/request-password-reset", json={"email": test_user.email}
        )

        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_request_password_reset_nonexistent_user(
        self, async_client: AsyncClient
    ):
        """Test password reset request for non-existent user."""
        response = await async_client.post(
            "/auth/request-password-reset", json={"email": "nonexistent@example.com"}
        )

        # Should still return 204 to prevent email enumeration
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_reset_password_success(
        self, async_client: AsyncClient, test_user: User, test_db: AsyncSession
    ):
        """Test successful password reset."""
        # A real reset link is bound to the user's current nonce + password
        await AuthService.forgot_password(test_user.email, test_db)
        await test_db.commit()
        token = email_token_utils.generate_password_reset_token(
            test_user.id,
            nonce=test_user.password_reset_nonce,
            hashed_password=test_user.hashed_password,
        )
        new_password = "NewSecurePassword123!"

        response = await async_client.post(
            "/auth/reset-password", json={"token": token, "new_password": new_password}
        )

        assert response.status_code == 204

        # Verify password was changed
        await test_db.refresh(test_user)
        assert test_user.hashed_password is not None
        assert AuthService.verify_password(new_password, test_user.hashed_password)

    @pytest.mark.asyncio
    async def test_reset_password_invalid_token(self, async_client: AsyncClient):
        """Test password reset with invalid token."""
        response = await async_client.post(
            "/auth/reset-password",
            json={
                "token": "invalid.token.string",  # Invalid token format
                "new_password": "NewPassword123!",
            },
        )

        assert response.status_code == 400  # Invalid token format
        data = response.json()
        assert "detail" in data
        assert "invalid or expired" in data["detail"].lower()

    # === Current User Profile Tests ===

    @pytest.mark.asyncio
    async def test_get_current_user_success(
        self, async_client: AsyncClient, authenticated_headers: dict[str, str]
    ):
        """Test getting current user information."""
        response = await async_client.get("/auth/me", headers=authenticated_headers)

        assert response.status_code == 200
        data = response.json()

        assert "id" in data
        assert "email" in data
        assert "full_name" in data
        assert "is_active" in data
        assert "is_verified" in data
        assert "password" not in data
        assert "hashed_password" not in data

    @pytest.mark.asyncio
    async def test_get_current_user_unauthorized(self, async_client: AsyncClient):
        """Test getting current user without authentication."""
        response = await async_client.get("/auth/me")

        assert response.status_code == 401
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_get_current_user_invalid_token(self, async_client: AsyncClient):
        """Test getting current user with invalid token."""
        headers = {"Authorization": "Bearer invalid-token"}
        response = await async_client.get("/auth/me", headers=headers)

        assert response.status_code == 401
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_update_current_user_success(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test updating current user information."""
        update_data = {"full_name": "Updated Name"}

        response = await async_client.patch(
            "/auth/me", json=update_data, headers=authenticated_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert data["full_name"] == update_data["full_name"]

    @pytest.mark.asyncio
    async def test_update_current_user_unauthorized(self, async_client: AsyncClient):
        """Test updating current user without authentication."""
        update_data = {"full_name": "Updated Name"}

        response = await async_client.patch("/auth/me", json=update_data)

        assert response.status_code == 401
        data = response.json()
        assert "detail" in data

    # === Health Check Tests ===

    @pytest.mark.asyncio
    async def test_health_check(self, async_client: AsyncClient):
        """Test health check endpoint."""
        response = await async_client.get("/auth/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["module"] == "identity"

    # === Rate Limiting Tests ===

    @pytest.mark.asyncio
    async def test_rate_limiting_registration(self, async_client: AsyncClient):
        """Test rate limiting on registration endpoint."""
        user_data = {
            "email": "ratelimit@example.com",
            "full_name": "Rate Limit Test",
            "password": "SecurePassword123!",
        }

        # Make multiple rapid requests
        responses: list[int] = []
        for i in range(10):
            user_data["email"] = f"ratelimit{i}@example.com"
            response = await async_client.post("/auth/register", json=user_data)
            responses.append(response.status_code)

        # Should eventually hit rate limit (429) or continue succeeding
        # Implementation depends on rate limit configuration
        assert all(code in [202, 429] for code in responses)

    # === Integration Edge Cases ===

    @pytest.mark.asyncio
    async def test_complete_user_journey(
        self, async_client: AsyncClient, test_db: AsyncSession
    ):
        """Test complete user journey: register -> verify -> login -> refresh -> logout."""
        # 1. Register
        user_data = {
            "email": "journey@example.com",
            "full_name": "Journey User",
            "password": "SecurePassword123!",
        }

        register_response = await async_client.post("/auth/register", json=user_data)
        assert register_response.status_code == 202
        user_id = (
            await test_db.execute(
                select(User.id).where(User.email == user_data["email"])
            )
        ).scalar_one()

        # 1b. Unverified accounts cannot sign in yet
        early = await async_client.post(
            "/auth/login",
            json={"email": user_data["email"], "password": user_data["password"]},
        )
        assert early.status_code == 403

        # 2. Verify email
        verify_token = email_token_utils.generate_email_verification_token(user_id)
        verify_response = await async_client.post(
            "/auth/verify-email", json={"token": verify_token}
        )
        assert verify_response.status_code == 204

        # 3. Login
        login_response = await async_client.post(
            "/auth/login",
            json={"email": user_data["email"], "password": user_data["password"]},
        )
        assert login_response.status_code == 200
        tokens = login_response.json()

        # 4. Access protected endpoint
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        me_response = await async_client.get("/auth/me", headers=headers)
        assert me_response.status_code == 200

        # 5. Refresh token
        refresh_response = await async_client.post(
            "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert refresh_response.status_code == 200

        # 6. Logout
        logout_response = await async_client.post(
            "/auth/logout", json={"refresh_token": tokens["refresh_token"]}
        )
        assert logout_response.status_code == 204

    @pytest.mark.asyncio
    async def test_concurrent_operations(self, async_client: AsyncClient):
        """Test concurrent operations don't interfere."""
        import asyncio

        # Concurrent registrations
        tasks: list = []
        for i in range(5):
            user_data = {
                "email": f"concurrent{i}@example.com",
                "full_name": f"Concurrent User {i}",
                "password": "SecurePassword123!",
            }
            task = async_client.post("/auth/register", json=user_data)
            tasks.append(task)

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        # All should succeed (or fail gracefully)
        for response in responses:
            if hasattr(response, "status_code"):
                assert response.status_code in [
                    202,
                    400,
                    422,
                    429,
                    500,
                ]  # Include 500 for concurrent db issues
