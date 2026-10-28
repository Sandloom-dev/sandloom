"""
Comprehensive tests for AuthService with full coverage.
Tests both positive and negative scenarios following best practices.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_identity.events import EventActor
from sandloom_identity.models.user import User
from sandloom_identity.schemas.auth import LoginIn, RegisterIn, TokenPair
from sandloom_identity.services.auth_service import AuthService
from sandloom_identity.tests.utils.emails import deliver_emails
from sandloom_identity.utils import email_token_utils


def _enabled_secret(user: Mock) -> Mock:
    """The ENABLED TOTP secret row the login lookup joins to the user."""
    secret = Mock()
    secret.user_id = user.id
    secret.is_enabled = True
    return secret


class TestAuthServiceComprehensive:
    """Comprehensive test cases for AuthService."""

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        """Mock database session."""
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()
        mock_session.add = Mock()
        return mock_session

    @pytest.fixture
    def sample_user(self) -> Mock:
        """Sample user instance."""
        user = Mock(spec=User)
        user.id = uuid4()
        user.email = "test@example.com"
        user.full_name = "Test User"
        user.hashed_password = AuthService.get_password_hash("password123")
        user.is_active = True
        user.is_verified = True
        user.password_reset_required = False
        user.password_reset_nonce = None
        user.user_roles = []
        user.last_login_at = None
        user.email_verified_at = None
        user.updated_at = None
        return user

    @pytest.fixture
    def sample_verified_user(self) -> Mock:
        """Sample verified user instance."""
        user = Mock(spec=User)
        user.id = uuid4()
        user.email = "verified@example.com"
        user.full_name = "Verified User"
        user.hashed_password = AuthService.get_password_hash("password123")
        user.is_active = True
        user.is_verified = True
        user.password_reset_required = False
        user.user_roles = []
        user.last_login_at = None
        user.email_verified_at = datetime.now(UTC)
        user.updated_at = None
        return user

    @pytest.fixture
    def sample_inactive_user(self) -> Mock:
        """Sample inactive user instance."""
        user = Mock(spec=User)
        user.id = uuid4()
        user.email = "inactive@example.com"
        user.full_name = "Inactive User"
        user.hashed_password = AuthService.get_password_hash("password123")
        user.is_active = False
        user.is_verified = True
        user.password_reset_required = False
        user.user_roles = []
        user.last_login_at = None
        user.email_verified_at = datetime.now(UTC)
        return user

    @pytest.fixture
    def sample_actor(self) -> EventActor:
        """Sample event actor."""
        return EventActor(user_id=uuid4(), ip="127.0.0.1")

    @pytest.fixture
    def register_data(self) -> RegisterIn:
        """Sample registration data."""
        return RegisterIn(
            email="newuser@example.com",
            full_name="New User",
            password="securepassword123",
        )

    @pytest.fixture
    def login_data(self) -> LoginIn:
        """Sample login data."""
        return LoginIn(email="test@example.com", password="password123", otp=None)

    @pytest.fixture
    def login_data_with_otp(self) -> LoginIn:
        """Sample login data with OTP."""
        return LoginIn(email="test@example.com", password="password123", otp="123456")

    @pytest.fixture
    def sample_tokens(self) -> TokenPair:
        """Sample token pair."""
        return TokenPair(
            access_token="access_token_123",
            refresh_token="refresh_token_456",
            token_type="bearer",
            expires_in=3600,
        )

    # UTILITY METHODS TESTS
    def test_verify_password_correct(self) -> None:
        """Test password verification with correct password."""
        password = "testpassword123"
        hashed = AuthService.get_password_hash(password)

        is_valid = AuthService.verify_password(password, hashed)
        assert is_valid is True

    def test_verify_password_incorrect(self) -> None:
        """Test password verification with incorrect password."""
        password = "testpassword123"
        wrong_password = "wrongpassword"
        hashed = AuthService.get_password_hash(password)

        is_valid = AuthService.verify_password(wrong_password, hashed)
        assert is_valid is False

    def test_get_password_hash(self) -> None:
        """Test password hashing."""
        password = "testpassword123"
        hashed = AuthService.get_password_hash(password)

        assert hashed != password
        assert len(hashed) > 50  # Argon2 hash is typically longer
        assert hashed.startswith("$argon2")

    def test_get_instance(self) -> None:
        """Test service instance creation."""
        instance = AuthService.get_instance()
        assert isinstance(instance, AuthService)

    # POSITIVE SCENARIOS - REGISTER
    @pytest.mark.asyncio
    @patch(
        "sandloom_identity.services.auth_service.AuthService._send_verification_email"
    )
    async def test_register_success(
        self,
        mock_send_email: AsyncMock,
        mock_db: AsyncMock,
        register_data: RegisterIn,
        sample_actor: EventActor,
    ) -> None:
        """Test successful user registration."""
        # Mock no existing user
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act
        result = await AuthService.register(register_data, mock_db, sample_actor)

        # Assert
        assert isinstance(result, User)
        assert result.email == register_data.email
        assert result.full_name == register_data.full_name
        assert result.is_active is True
        assert result.is_verified is False

        mock_db.add.assert_called_once()
        mock_db.flush.assert_called_once()
        mock_db.refresh.assert_called_once()
        mock_send_email.assert_called_once_with(
            register_data.email, result.id, register_data.full_name
        )

    # NEGATIVE SCENARIOS - REGISTER
    @pytest.mark.asyncio
    async def test_register_existing_user(
        self, mock_db: AsyncMock, register_data: RegisterIn, sample_user: Mock
    ) -> None:
        """Test registration with existing email."""
        # Mock existing user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Generic outcome (no enumeration): no error, no new user, and the
        # existing owner is notified instead.
        with patch(
            "sandloom_identity.services.auth_service.email_sender."
            "send_registration_attempt_email",
            new=AsyncMock(),
        ) as notify:
            result = await AuthService.register(register_data, mock_db)
            await deliver_emails()

        assert result is None
        mock_db.add.assert_not_called()
        notify.assert_awaited_once()
        assert notify.await_args.kwargs["to_email"] == sample_user.email

    @pytest.mark.asyncio
    async def test_login_unverified_user_rejected(
        self, mock_db: AsyncMock, login_data: LoginIn, sample_user: Mock
    ) -> None:
        """An unverified password registration cannot sign in (pre-hijack)."""
        sample_user.is_verified = False
        mock_result = Mock()
        mock_result.first.return_value = (sample_user, None)
        mock_db.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc:
            await AuthService.login(login_data, mock_db)
        assert exc.value.status_code == status.HTTP_403_FORBIDDEN

    # POSITIVE SCENARIOS - LOGIN
    @pytest.mark.asyncio
    @patch("sandloom_identity.services.token_service.TokenService.create_tokens")
    async def test_login_success(
        self,
        mock_create_tokens: AsyncMock,
        mock_db: AsyncMock,
        login_data: LoginIn,
        sample_user: Mock,
        sample_tokens: TokenPair,
        sample_actor: EventActor,
    ) -> None:
        """Test successful user login."""
        # Mock user found
        mock_result = Mock()
        mock_result.first.return_value = (sample_user, None)
        mock_db.execute.return_value = mock_result
        mock_create_tokens.return_value = sample_tokens

        # Act (no 2FA configured for this user)
        user, tokens = await AuthService.login(login_data, mock_db, sample_actor)

        # Assert
        assert user == sample_user
        assert tokens == sample_tokens
        assert sample_user.last_login_at is not None
        # Written by the UoW commit, after minting (no flush holding the row
        # lock across claims collection — test_login_lock_order).
        mock_db.flush.assert_not_called()
        mock_create_tokens.assert_called_once_with(
            sample_user, mock_db, None, block_checked=True
        )

    @pytest.mark.asyncio
    @patch("sandloom_identity.services.token_service.TokenService.create_tokens")
    async def test_login_with_otp(
        self,
        mock_create_tokens: AsyncMock,
        mock_db: AsyncMock,
        login_data_with_otp: LoginIn,
        sample_user: Mock,
        sample_tokens: TokenPair,
    ) -> None:
        """Test login with OTP provided."""
        # Mock user found
        mock_result = Mock()
        mock_result.first.return_value = (sample_user, _enabled_secret(sample_user))
        mock_db.execute.return_value = mock_result
        mock_create_tokens.return_value = sample_tokens

        # Act: 2FA enabled, valid OTP accepted
        with (
            patch(
                "sandloom_identity.services.twofa_service.TwoFAService.verify_2fa_code",
                new=AsyncMock(return_value=True),
            ),
        ):
            user, tokens = await AuthService.login(login_data_with_otp, mock_db)

        # Assert
        assert user == sample_user
        assert tokens == sample_tokens
        mock_create_tokens.assert_called_once()

    @pytest.mark.asyncio
    async def test_login_2fa_required_without_otp(
        self,
        mock_db: AsyncMock,
        login_data: LoginIn,
        sample_user: Mock,
    ) -> None:
        """2FA-enabled user without OTP is rejected with 401."""
        mock_result = Mock()
        mock_result.first.return_value = (sample_user, _enabled_secret(sample_user))
        mock_db.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc:
            await AuthService.login(login_data, mock_db)
        assert exc.value.status_code == 401
        assert "2FA" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_login_2fa_invalid_otp_rejected(
        self,
        mock_db: AsyncMock,
        login_data_with_otp: LoginIn,
        sample_user: Mock,
    ) -> None:
        """2FA-enabled user with a wrong OTP is rejected with 401."""
        mock_result = Mock()
        mock_result.first.return_value = (sample_user, _enabled_secret(sample_user))
        mock_db.execute.return_value = mock_result

        with (
            patch(
                "sandloom_identity.services.twofa_service.TwoFAService.verify_2fa_code",
                new=AsyncMock(return_value=False),
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await AuthService.login(login_data_with_otp, mock_db)
        assert exc.value.status_code == 401

    # NEGATIVE SCENARIOS - LOGIN
    @pytest.mark.asyncio
    async def test_login_user_not_found(
        self, mock_db: AsyncMock, login_data: LoginIn
    ) -> None:
        """Test login with non-existent user."""
        # Mock no user found
        mock_result = Mock()
        mock_result.first.return_value = None
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await AuthService.login(login_data, mock_db)

        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert "Incorrect email or password" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_login_wrong_password(
        self, mock_db: AsyncMock, sample_user: Mock
    ) -> None:
        """Test login with wrong password."""
        # Mock user found
        mock_result = Mock()
        mock_result.first.return_value = (sample_user, None)
        mock_db.execute.return_value = mock_result

        # Create login data with wrong password
        login_data = LoginIn(
            email="test@example.com", password="wrongpassword", otp=None
        )

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await AuthService.login(login_data, mock_db)

        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert "Incorrect email or password" in exc_info.value.detail

    @pytest.mark.asyncio
    @patch("sandloom_identity.services.token_service.TokenService.create_tokens")
    async def test_login_inactive_user(
        self,
        mock_create_tokens: AsyncMock,
        mock_db: AsyncMock,
        login_data: LoginIn,
        sample_inactive_user: Mock,
    ) -> None:
        """Test login with inactive user."""
        # Mock inactive user found
        mock_result = Mock()
        mock_result.first.return_value = (sample_inactive_user, None)
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await AuthService.login(login_data, mock_db)

        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert "Account is not active" in exc_info.value.detail

    # TOKEN MANAGEMENT TESTS
    @pytest.mark.asyncio
    @patch("sandloom_identity.services.token_service.TokenService.refresh_token")
    async def test_refresh_token_success(
        self,
        mock_refresh_token: AsyncMock,
        mock_db: AsyncMock,
        sample_tokens: TokenPair,
    ) -> None:
        """Test successful token refresh."""
        mock_refresh_token.return_value = sample_tokens

        result = await AuthService.refresh_token("refresh_token_123", mock_db)

        assert result == sample_tokens
        mock_refresh_token.assert_called_once_with("refresh_token_123", mock_db, None)

    @pytest.mark.asyncio
    @patch("sandloom_identity.services.token_service.TokenService.revoke_token")
    async def test_logout_success(
        self, mock_revoke_token: AsyncMock, mock_db: AsyncMock, sample_actor: EventActor
    ) -> None:
        """Test successful logout."""
        await AuthService.logout("refresh_token_123", mock_db, sample_actor)

        mock_revoke_token.assert_called_once_with("refresh_token_123", mock_db)

    # EMAIL CONFIRMATION TESTS
    @pytest.mark.asyncio
    async def test_confirm_email_success(
        self, mock_db: AsyncMock, sample_user: Mock, sample_actor: EventActor
    ) -> None:
        """Test successful email confirmation."""
        sample_user.is_verified = False
        # Mock user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Generate proper email verification token
        token = email_token_utils.generate_email_verification_token(sample_user.id)

        result = await AuthService.confirm_email(token, mock_db, sample_actor)

        assert result == sample_user
        assert sample_user.is_verified is True
        # Mock objects don't auto-update, so we need to check if it was set
        assert hasattr(sample_user, "updated_at")
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_confirm_email_user_not_found(self, mock_db: AsyncMock) -> None:
        """Test email confirmation with non-existent user."""
        # Mock no user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Use invalid token
        token = "invalid_token_format"

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await AuthService.confirm_email(token, mock_db)

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "Invalid or expired verification token" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_confirm_email_already_verified(
        self, mock_db: AsyncMock, sample_verified_user: Mock
    ) -> None:
        """Test email confirmation for already verified user."""
        # Mock verified user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_verified_user
        mock_db.execute.return_value = mock_result

        # Generate proper email verification token
        token = email_token_utils.generate_email_verification_token(
            sample_verified_user.id
        )

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await AuthService.confirm_email(token, mock_db)

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "Email already verified" in exc_info.value.detail

    # PASSWORD RESET TESTS
    @pytest.mark.asyncio
    @patch(
        "sandloom_identity.services.auth_service.email_sender.send_password_reset_email",
        new_callable=AsyncMock,
    )
    async def test_forgot_password_user_exists(
        self, mock_send_email: AsyncMock, mock_db: AsyncMock, sample_user: Mock
    ) -> None:
        """Test forgot password with existing user."""
        # Mock user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        await AuthService.forgot_password(sample_user.email, mock_db)
        await deliver_emails()

        mock_send_email.assert_called_once()
        assert mock_send_email.call_args.kwargs["to_email"] == sample_user.email
        # A fresh single-use nonce is bound to the user and the emailed token.
        assert sample_user.password_reset_nonce
        url = mock_send_email.call_args.kwargs["reset_url"]
        token = url.split("token=", 1)[1]
        claims = email_token_utils.read_password_reset_token(token)
        assert claims.nonce == sample_user.password_reset_nonce

    @pytest.mark.asyncio
    @patch(
        "sandloom_identity.services.auth_service.email_sender.send_password_reset_email",
        new_callable=AsyncMock,
    )
    async def test_forgot_password_user_not_exists(
        self, mock_send_email: AsyncMock, mock_db: AsyncMock
    ) -> None:
        """Test forgot password with non-existent user."""
        # Mock no user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        await AuthService.forgot_password("nonexistent@example.com", mock_db)

        # Should not send email for security
        mock_send_email.assert_not_called()

    @pytest.mark.asyncio
    @patch(
        "sandloom_identity.services.token_service.TokenService.revoke_all_user_tokens"
    )
    async def test_reset_password_success(
        self,
        mock_revoke_tokens: AsyncMock,
        mock_db: AsyncMock,
        sample_user: Mock,
        sample_actor: EventActor,
    ) -> None:
        """Test successful password reset."""
        # Mock user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Generate a token bound to the user's current nonce + password hash
        sample_user.password_reset_nonce = "nonce-1"
        token = email_token_utils.generate_password_reset_token(
            sample_user.id,
            nonce="nonce-1",
            hashed_password=sample_user.hashed_password,
        )
        new_password = "newpassword123"
        old_hash = sample_user.hashed_password

        result = await AuthService.reset_password(
            token, new_password, mock_db, sample_actor
        )

        assert result == sample_user
        assert sample_user.hashed_password != old_hash
        # Mock objects don't auto-update, so we check if it has the attribute
        assert hasattr(sample_user, "updated_at")
        mock_db.flush.assert_called_once()
        mock_revoke_tokens.assert_called_once_with(sample_user.id, mock_db)
        # Single use: the nonce is consumed.
        assert sample_user.password_reset_nonce is None

    @pytest.mark.asyncio
    async def test_reset_password_invalid_token(self, mock_db: AsyncMock) -> None:
        """Test password reset with invalid token."""
        # Mock no user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Use invalid token (malformed)
        token = "invalid_token_format"
        new_password = "newpassword123"

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await AuthService.reset_password(token, new_password, mock_db)

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "Invalid or expired reset token" in exc_info.value.detail

    # CURRENT USER TESTS
    @pytest.mark.asyncio
    @patch("sandloom.core.auth.tokens.decode_token")
    async def test_get_current_user_success(
        self, mock_decode_token: Mock, mock_db: AsyncMock, sample_user: Mock
    ) -> None:
        """Test successful current user retrieval."""
        # Mock token decode
        mock_decode_token.return_value = {
            "sub": str(sample_user.id),
            "token_type": "access",
        }

        # Mock user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        result = await AuthService.get_current_user("valid_token", mock_db)

        assert result == sample_user
        mock_decode_token.assert_called_once_with("valid_token")

    @pytest.mark.asyncio
    @patch("sandloom.core.auth.tokens.decode_token")
    async def test_get_current_user_invalid_token(
        self, mock_decode_token: Mock, mock_db: AsyncMock
    ) -> None:
        """Test current user with invalid token."""
        mock_decode_token.return_value = None

        result = await AuthService.get_current_user("invalid_token", mock_db)

        assert result is None

    @pytest.mark.asyncio
    @patch("sandloom.core.auth.tokens.decode_token")
    async def test_get_current_user_no_subject(
        self, mock_decode_token: Mock, mock_db: AsyncMock
    ) -> None:
        """Test current user with token missing subject."""
        mock_decode_token.return_value = {"other": "data"}

        result = await AuthService.get_current_user("token_without_sub", mock_db)

        assert result is None

    @pytest.mark.asyncio
    @patch("sandloom.core.auth.tokens.decode_token")
    async def test_get_current_user_not_found(
        self, mock_decode_token: Mock, mock_db: AsyncMock
    ) -> None:
        """Test current user when user not found in database."""
        mock_decode_token.return_value = {"sub": str(uuid4())}

        # Mock no user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await AuthService.get_current_user("valid_token", mock_db)

        assert result is None

    @pytest.mark.asyncio
    @patch("sandloom.core.auth.tokens.decode_token")
    async def test_get_current_user_inactive(
        self, mock_decode_token: Mock, mock_db: AsyncMock, sample_inactive_user: Mock
    ) -> None:
        """Test current user when user is inactive."""
        mock_decode_token.return_value = {"sub": str(sample_inactive_user.id)}

        # Mock inactive user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_inactive_user
        mock_db.execute.return_value = mock_result

        result = await AuthService.get_current_user("valid_token", mock_db)

        assert result is None

    @pytest.mark.asyncio
    @patch("sandloom.core.auth.tokens.decode_token")
    async def test_get_current_user_exception(
        self, mock_decode_token: Mock, mock_db: AsyncMock
    ) -> None:
        """Test current user when exception occurs."""
        mock_decode_token.side_effect = Exception("Token error")

        result = await AuthService.get_current_user("error_token", mock_db)

        assert result is None

    # TESTS FOR FULL COVERAGE OF EMAIL METHODS
    @pytest.mark.asyncio
    @patch("sandloom_identity.integrations.email_sender.send_verification_email")
    async def test_register_calls_verification_email(
        self, mock_send_email: AsyncMock, mock_db: AsyncMock, register_data: RegisterIn
    ) -> None:
        """Test registration calls verification email without mocking the private method."""
        # Mock no existing user
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act - don't mock _send_verification_email so it executes
        await AuthService.register(register_data, mock_db)
        await deliver_emails()

        # Assert verification email was called
        mock_send_email.assert_called_once()
        call_kwargs = mock_send_email.call_args[1]
        assert call_kwargs["to_email"] == register_data.email
        assert call_kwargs["full_name"] == register_data.full_name
        assert "verify-email" in call_kwargs["verification_url"]

    @pytest.mark.asyncio
    @patch(
        "sandloom_identity.integrations.email_sender.send_password_reset_email",
        new_callable=AsyncMock,
    )
    async def test_forgot_password_calls_reset_email(
        self, mock_send_email: AsyncMock, mock_db: AsyncMock, sample_user: Mock
    ) -> None:
        """Test forgot password calls reset email without mocking the private method."""
        # Mock user found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_user
        mock_db.execute.return_value = mock_result

        # Act - don't mock _send_password_reset_email so it executes
        await AuthService.forgot_password(sample_user.email, mock_db)
        await deliver_emails()

        # Assert reset email was called
        mock_send_email.assert_called_once()
        call_kwargs = mock_send_email.call_args[1]
        assert call_kwargs["to_email"] == sample_user.email
        assert call_kwargs["full_name"] == sample_user.full_name
        assert "reset-password" in call_kwargs["reset_url"]
