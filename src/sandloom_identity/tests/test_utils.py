"""Tests for Identity utils module."""

from datetime import UTC, datetime
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from sandloom_identity.utils import (
    CryptoUtils,
    EmailTokenUtils,
    RefreshTokenUtils,
    TokenUtils,
    TwoFAUtils,
    crypto_utils,
    email_token_utils,
    refresh_token_utils,
    twofa_utils,
)


class TestCryptoUtils:
    """Test CryptoUtils functionality."""

    def test_crypto_utils_initialization(self):
        """Test CryptoUtils initialization."""
        # Test with default key
        crypto = CryptoUtils()
        assert crypto._fernet is not None

        # Test with custom key
        custom_crypto = CryptoUtils(key="custom_secret_key")
        assert custom_crypto._fernet is not None

    def test_encrypt_decrypt(self):
        """Test encryption and decryption."""
        crypto = CryptoUtils(key="test_secret_key")

        test_data = "Hello, World!"
        encrypted = crypto.encrypt(test_data)

        # Encrypted data should be different from original
        assert encrypted != test_data
        assert isinstance(encrypted, str)

        # Decrypt should return original data
        decrypted = crypto.decrypt(encrypted)
        assert decrypted == test_data

    def test_encrypt_decrypt_empty_string(self):
        """Test encryption and decryption of empty string."""
        crypto = CryptoUtils(key="test_secret_key")

        test_data = ""
        encrypted = crypto.encrypt(test_data)
        decrypted = crypto.decrypt(encrypted)
        assert decrypted == test_data

    def test_decrypt_invalid_data(self):
        """Test decryption with invalid data."""
        crypto = CryptoUtils(key="test_secret_key")

        with pytest.raises(ValueError, match="Failed to decrypt data"):
            crypto.decrypt("invalid_encrypted_data")

    def test_decrypt_wrong_key(self):
        """Test decryption with wrong key."""
        crypto1 = CryptoUtils(key="key1")
        crypto2 = CryptoUtils(key="key2")

        test_data = "Secret message"
        encrypted = crypto1.encrypt(test_data)

        with pytest.raises(ValueError, match="Failed to decrypt data"):
            crypto2.decrypt(encrypted)


class TestTokenUtils:
    """Test TokenUtils functionality."""

    def test_token_utils_initialization(self):
        """Test TokenUtils initialization."""
        # Test with default parameters
        token_utils = TokenUtils()
        assert token_utils._serializer is not None

        # Test with custom parameters
        custom_token_utils = TokenUtils(secret_key="custom_key", salt="custom_salt")
        assert custom_token_utils._serializer is not None

    def test_generate_validate_token(self):
        """Test token generation and validation."""
        token_utils = TokenUtils(secret_key="test_secret")

        test_data = {"user_id": "123", "role": "admin"}
        token = token_utils.generate_token(test_data)

        assert isinstance(token, str)
        assert len(token) > 0

        # Validate token
        validated_data = token_utils.validate_token(token)
        assert validated_data == test_data

    def test_generate_token_with_custom_expiry(self):
        """Test token generation with custom expiry."""
        token_utils = TokenUtils(secret_key="test_secret")

        test_data = {"user_id": "123"}
        token = token_utils.generate_token(test_data, expires_in=7200)  # 2 hours

        validated_data = token_utils.validate_token(token, max_age=7200)
        assert validated_data == test_data

    def test_validate_expired_token(self):
        """Test validation of expired token."""
        token_utils = TokenUtils(secret_key="test_secret")

        test_data = {"user_id": "123"}

        # Mock datetime to simulate past expiration
        with patch("sandloom_identity.utils.datetime") as mock_datetime:
            # Set current time for token generation
            mock_datetime.now.return_value = datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC)
            mock_datetime.fromisoformat = datetime.fromisoformat
            token = token_utils.generate_token(test_data, expires_in=3600)

            # Move time forward beyond expiration
            mock_datetime.now.return_value = datetime(2023, 1, 1, 14, 0, 0, tzinfo=UTC)

            with pytest.raises(ValueError, match="Invalid token"):
                token_utils.validate_token(token)

    def test_validate_invalid_token(self):
        """Test validation of invalid token."""
        token_utils = TokenUtils(secret_key="test_secret")

        with pytest.raises(ValueError, match="Invalid token"):
            token_utils.validate_token("invalid_token")

    def test_validate_token_wrong_secret(self):
        """Test validation with wrong secret."""
        token_utils1 = TokenUtils(secret_key="secret1")
        token_utils2 = TokenUtils(secret_key="secret2")

        test_data = {"user_id": "123"}
        token = token_utils1.generate_token(test_data)

        with pytest.raises(ValueError, match="Invalid token"):
            token_utils2.validate_token(token)


class TestTwoFAUtils:
    """Test TwoFAUtils functionality."""

    def test_generate_secret(self):
        """Test TOTP secret generation."""
        secret = TwoFAUtils.generate_secret()
        assert isinstance(secret, str)
        assert len(secret) > 0

        # Generate another secret to ensure they're different
        secret2 = TwoFAUtils.generate_secret()
        assert secret != secret2

    @patch("qrcode.QRCode")
    def test_generate_qr_code(self, mock_qr_class):
        """Test QR code generation."""
        # Mock QR code generation
        mock_qr = Mock()
        mock_img = Mock()
        mock_qr.make_image.return_value = mock_img
        mock_qr_class.return_value = mock_qr

        # Mock image save
        mock_img.save = Mock()

        with (
            patch("sandloom_identity.utils.base64.b64encode") as mock_b64encode,
            patch("sandloom_identity.utils.BytesIO") as mock_bytesio,
        ):
            mock_buffer = Mock()
            mock_bytesio.return_value = mock_buffer
            mock_buffer.getvalue.return_value = b"fake_image_data"
            mock_b64encode.return_value = b"fake_base64_data"

            result = TwoFAUtils.generate_qr_code("TESTSECRET", "user@example.com")

            assert result == "data:image/png;base64,fake_base64_data"
            mock_qr.add_data.assert_called_once()
            mock_qr.make.assert_called_once()

    @patch("sandloom_identity.utils.pyotp.TOTP")
    def test_verify_totp(self, mock_totp_class):
        """Test TOTP token verification."""
        mock_totp = Mock()
        mock_totp.verify.return_value = True
        mock_totp_class.return_value = mock_totp

        result = TwoFAUtils.verify_totp("TESTSECRET", "123456")
        assert result is True

        mock_totp.verify.assert_called_once_with("123456", valid_window=1)

        # Test with custom window
        TwoFAUtils.verify_totp("TESTSECRET", "123456", window=2)
        mock_totp.verify.assert_called_with("123456", valid_window=2)

    def test_generate_backup_codes(self):
        """Test backup codes generation."""
        codes = TwoFAUtils.generate_backup_codes()

        assert isinstance(codes, list)
        assert len(codes) == 10  # Default count

        # All codes should be strings
        for code in codes:
            assert isinstance(code, str)
            assert len(code) > 0

        # All codes should be unique
        assert len(set(codes)) == len(codes)

        # Test custom count
        custom_codes = TwoFAUtils.generate_backup_codes(count=5)
        assert len(custom_codes) == 5


class TestRefreshTokenUtils:
    """Test RefreshTokenUtils functionality."""

    def test_generate_token(self):
        """Test refresh token generation."""
        token = RefreshTokenUtils.generate_token()

        assert isinstance(token, str)
        assert len(token) > 0

        # Generate another token to ensure they're different
        token2 = RefreshTokenUtils.generate_token()
        assert token != token2

    def test_hash_token(self):
        """Test token hashing."""
        token = "test_token"
        token_hash = RefreshTokenUtils.hash_token(token)

        assert isinstance(token_hash, str)
        assert len(token_hash) == 64  # SHA256 hex digest length
        assert token_hash != token

        # Same token should produce same hash
        token_hash2 = RefreshTokenUtils.hash_token(token)
        assert token_hash == token_hash2

    def test_verify_token(self):
        """Test token verification."""
        token = "test_token"
        correct_hash = RefreshTokenUtils.hash_token(token)
        wrong_hash = RefreshTokenUtils.hash_token("wrong_token")

        # Correct verification
        assert RefreshTokenUtils.verify_token(token, correct_hash) is True

        # Wrong verification
        assert RefreshTokenUtils.verify_token(token, wrong_hash) is False
        assert RefreshTokenUtils.verify_token("wrong_token", correct_hash) is False


class TestEmailTokenUtils:
    """Test EmailTokenUtils functionality."""

    def test_email_token_utils_initialization(self):
        """Test EmailTokenUtils initialization."""
        email_utils = EmailTokenUtils()
        assert email_utils._token_utils is not None
        assert email_utils._password_utils is not None

    def test_generate_validate_email_verification_token(self):
        """Test email verification token generation and validation."""
        email_utils = EmailTokenUtils()
        user_id = uuid4()

        token = email_utils.generate_email_verification_token(user_id)
        assert isinstance(token, str)
        assert len(token) > 0

        # Validate token
        validated_user_id = email_utils.validate_email_verification_token(token)
        assert validated_user_id == user_id

    def test_validate_invalid_email_verification_token(self):
        """Test validation of invalid email verification token."""
        email_utils = EmailTokenUtils()

        with pytest.raises(ValueError, match="Invalid email verification token"):
            email_utils.validate_email_verification_token("invalid_token")

    def test_generate_validate_password_reset_token(self):
        """Test password reset token generation and validation."""
        email_utils = EmailTokenUtils()
        user_id = uuid4()

        token = email_utils.generate_password_reset_token(user_id)
        assert isinstance(token, str)
        assert len(token) > 0

        # Validate token
        validated_user_id = email_utils.validate_password_reset_token(token)
        assert validated_user_id == user_id

    def test_validate_invalid_password_reset_token(self):
        """Test validation of invalid password reset token."""
        email_utils = EmailTokenUtils()

        with pytest.raises(ValueError, match="Invalid password reset token"):
            email_utils.validate_password_reset_token("invalid_token")

    def test_validate_wrong_token_type(self):
        """Test validation with wrong token type."""
        email_utils = EmailTokenUtils()
        user_id = uuid4()

        # Generate email verification token
        email_token = email_utils.generate_email_verification_token(user_id)

        # Try to validate as password reset token (should fail)
        with pytest.raises(ValueError, match="Invalid password reset token"):
            email_utils.validate_password_reset_token(email_token)


class TestGlobalInstances:
    """Test global utility instances."""

    def test_global_instances_exist(self):
        """Test that global instances are created."""
        assert crypto_utils is not None
        assert isinstance(crypto_utils, CryptoUtils)

        assert twofa_utils is not None
        assert isinstance(twofa_utils, TwoFAUtils)

        assert refresh_token_utils is not None
        assert isinstance(refresh_token_utils, RefreshTokenUtils)

        assert email_token_utils is not None
        assert isinstance(email_token_utils, EmailTokenUtils)

    def test_global_instances_functionality(self):
        """Test basic functionality of global instances."""
        # Test crypto utils
        test_data = "test message"
        encrypted = crypto_utils.encrypt(test_data)
        decrypted = crypto_utils.decrypt(encrypted)
        assert decrypted == test_data

        # Test refresh token utils
        token = refresh_token_utils.generate_token()
        token_hash = refresh_token_utils.hash_token(token)
        assert refresh_token_utils.verify_token(token, token_hash) is True

        # Test TwoFA utils
        secret = twofa_utils.generate_secret()
        assert isinstance(secret, str)
        assert len(secret) > 0

        backup_codes = twofa_utils.generate_backup_codes()
        assert len(backup_codes) == 10

        # Test email token utils
        user_id = uuid4()
        email_token = email_token_utils.generate_email_verification_token(user_id)
        validated_id = email_token_utils.validate_email_verification_token(email_token)
        assert validated_id == user_id
