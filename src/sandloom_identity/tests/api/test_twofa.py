"""Comprehensive integration tests for 2FA API endpoints.

This module contains comprehensive tests for 2FA API endpoints covering:
- 2FA status checking
- 2FA initialization and setup
- 2FA enabling and disabling
- 2FA code verification
- Backup codes generation
- All authentication and authorization flows
- Positive and negative scenarios
- Error handling and edge cases

Following the same patterns as other API integration tests.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

# test_user's password: changing the second factor requires step-up re-auth.
PW = "TestPassword123!"
REAUTH = {"current_password": PW}


class TestTwoFAAPI:
    """Integration tests for 2FA API endpoints."""

    @pytest.mark.asyncio
    async def test_get_2fa_status_unauthenticated(
        self, async_client: AsyncClient
    ) -> None:
        """Test getting 2FA status without authentication should fail."""
        # Act
        response = await async_client.get("/auth/2fa/status")

        # Assert
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_get_2fa_status_authenticated(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test getting 2FA status for authenticated user."""
        # Act
        response = await async_client.get(
            "/auth/2fa/status", headers=authenticated_headers
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "enabled" in data
        assert "has_backup_codes" in data
        assert isinstance(data["enabled"], bool)
        assert isinstance(data["has_backup_codes"], bool)

    @pytest.mark.asyncio
    async def test_init_2fa_unauthenticated(self, async_client: AsyncClient) -> None:
        """Test 2FA initialization without authentication should fail."""
        # Act
        response = await async_client.post("/auth/2fa/init")

        # Assert
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_init_2fa_authenticated(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test 2FA initialization for authenticated user."""
        # Act
        response = await async_client.post(
            "/auth/2fa/init", json=REAUTH, headers=authenticated_headers
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "secret" in data
        assert "qr_code" in data
        assert "backup_codes" in data
        assert isinstance(data["secret"], str)
        assert isinstance(data["qr_code"], str)
        assert isinstance(data["backup_codes"], list)
        assert len(data["secret"]) > 0
        assert len(data["qr_code"]) > 0
        assert len(data["backup_codes"]) > 0

    @pytest.mark.asyncio
    async def test_enable_2fa_unauthenticated(self, async_client: AsyncClient) -> None:
        """Test 2FA enabling without authentication should fail."""
        # Act
        response = await async_client.post(
            "/auth/2fa/enable", json={"otp": "123456", "current_password": PW}
        )

        # Assert
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_enable_2fa_invalid_otp_format(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test 2FA enabling with invalid OTP format."""
        # Test validation errors first (these should return 422)
        validation_errors = [
            {"otp": "12345"},  # Too short
            {"otp": "1234567"},  # Too long
            {"otp": ""},  # Empty
        ]

        for invalid_data in validation_errors:
            response = await async_client.post(
                "/auth/2fa/enable", json=invalid_data, headers=authenticated_headers
            )
            assert response.status_code == 422  # Validation error

        # Test valid format but without 2FA setup (should return 404)
        valid_format_invalid_setup = [
            {"otp": "abcdef", "current_password": PW},  # Valid length, no setup
            {"otp": "123456", "current_password": PW},  # Valid format but no 2FA setup
        ]

        for valid_format_data in valid_format_invalid_setup:
            response = await async_client.post(
                "/auth/2fa/enable",
                json=valid_format_data,
                headers=authenticated_headers,
            )
            assert response.status_code == 404  # 2FA setup not found

    @pytest.mark.asyncio
    async def test_enable_2fa_invalid_otp_code(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test 2FA enabling with invalid but well-formatted OTP."""
        # First initialize 2FA to get valid secret
        init_response = await async_client.post(
            "/auth/2fa/init", json=REAUTH, headers=authenticated_headers
        )
        assert init_response.status_code == 200

        # Try to enable with invalid OTP
        response = await async_client.post(
            "/auth/2fa/enable",
            json={"otp": "000000", "current_password": PW},  # Invalid code
            headers=authenticated_headers,
        )

        # Should fail with business logic error
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_disable_2fa_unauthenticated(self, async_client: AsyncClient) -> None:
        """Test 2FA disabling without authentication should fail."""
        # Act
        response = await async_client.post(
            "/auth/2fa/disable", json={"otp": "123456", "current_password": PW}
        )

        # Assert
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_disable_2fa_when_not_enabled(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test disabling 2FA when it's not enabled."""
        # First check status
        status_response = await async_client.get(
            "/auth/2fa/status", headers=authenticated_headers
        )
        assert status_response.status_code == 200

        # Try to disable 2FA when it's not even enabled/setup
        response = await async_client.post(
            "/auth/2fa/disable",
            json={"otp": "123456", "current_password": PW},
            headers=authenticated_headers,
        )

        # Should return 404 since 2FA is not setup
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_disable_2fa_invalid_otp_format(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test 2FA disabling with invalid OTP format."""
        # Test validation errors (should return 422)
        validation_errors = [
            {"otp": "12345"},  # Too short
            {"otp": "1234567"},  # Too long
            {"otp": ""},  # Empty
        ]

        for invalid_data in validation_errors:
            response = await async_client.post(
                "/auth/2fa/disable", json=invalid_data, headers=authenticated_headers
            )
            assert response.status_code == 422  # Validation error

        # Test valid format but no 2FA setup (should return 404)
        valid_format_no_setup = [
            {"otp": "abcdef1234567890abcd", "current_password": PW},  # backup code
            {"otp": "123456", "current_password": PW},  # Valid format but no 2FA setup
        ]

        for valid_data in valid_format_no_setup:
            response = await async_client.post(
                "/auth/2fa/disable", json=valid_data, headers=authenticated_headers
            )
            assert response.status_code == 404  # 2FA setup not found

    @pytest.mark.asyncio
    async def test_verify_2fa_code_unauthenticated(
        self, async_client: AsyncClient
    ) -> None:
        """Test 2FA code verification without authentication should fail."""
        # Act
        response = await async_client.post(
            "/auth/2fa/verify", json={"otp": "123456", "current_password": PW}
        )

        # Assert
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_verify_2fa_code_authenticated(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test 2FA code verification for authenticated user."""
        # Act
        response = await async_client.post(
            "/auth/2fa/verify",
            json={"otp": "123456", "current_password": PW},
            headers=authenticated_headers,
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "valid" in data
        assert isinstance(data["valid"], bool)
        # Should be False since we used invalid code
        assert data["valid"] is False

    @pytest.mark.asyncio
    async def test_verify_2fa_code_invalid_format(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test 2FA code verification with invalid format."""
        # Test validation errors (should return 422)
        validation_errors = [
            {"otp": "12345"},  # Too short
            {"otp": "1234567"},  # Too long
            {"otp": ""},  # Empty
        ]

        for invalid_data in validation_errors:
            response = await async_client.post(
                "/auth/2fa/verify", json=invalid_data, headers=authenticated_headers
            )
            assert response.status_code == 422  # Validation error

        # Test valid format codes (should return 200 with valid=false)
        valid_format_codes = [
            {
                "otp": "123456",
                "current_password": PW,
            },  # Valid format but incorrect code
            {
                "otp": "abcdef",
                "current_password": PW,
            },  # Valid format but incorrect code
        ]

        for valid_format_data in valid_format_codes:
            response = await async_client.post(
                "/auth/2fa/verify",
                json=valid_format_data,
                headers=authenticated_headers,
            )
            assert response.status_code == 200
            data = response.json()
            assert "valid" in data
            assert data["valid"] is False  # Should be invalid code

    @pytest.mark.asyncio
    async def test_verify_2fa_code_success_response(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test 2FA code verification returns proper success response format."""
        # Test that the endpoint returns the expected format regardless of validity
        response = await async_client.post(
            "/auth/2fa/verify",
            json={"otp": "123456", "current_password": PW},
            headers=authenticated_headers,
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "valid" in data
        assert isinstance(data["valid"], bool)
        # The response format should always include "valid" field

    @pytest.mark.asyncio
    async def test_generate_backup_codes_unauthenticated(
        self, async_client: AsyncClient
    ) -> None:
        """Test backup codes generation without authentication should fail."""
        # Act
        response = await async_client.get("/auth/2fa/backup-codes")

        # Assert
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_generate_backup_codes_authenticated(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test backup codes generation for authenticated user."""
        # Get backup codes without 2FA setup (should return empty list)
        response = await async_client.get(
            "/auth/2fa/backup-codes", headers=authenticated_headers
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "backup_codes" in data
        assert isinstance(data["backup_codes"], list)
        # Should be empty since user doesn't have 2FA set up
        assert len(data["backup_codes"]) == 0

    @pytest.mark.asyncio
    async def test_get_backup_codes_with_response_format(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test backup codes endpoint returns correct response format."""
        # Get backup codes (should return proper response structure)
        response = await async_client.get(
            "/auth/2fa/backup-codes", headers=authenticated_headers
        )

        # Assert response format is correct
        assert response.status_code == 200
        data = response.json()
        assert "backup_codes" in data
        assert isinstance(data["backup_codes"], list)
        # This test ensures the return statement on line 78 is covered

    @pytest.mark.asyncio
    async def test_regenerate_backup_codes_unauthenticated(
        self, async_client: AsyncClient
    ) -> None:
        """Test backup codes regeneration without authentication should fail."""
        # Act
        response = await async_client.post(
            "/auth/2fa/backup-codes/regenerate",
            json={"otp": "123456", "current_password": PW},
        )

        # Assert
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_regenerate_backup_codes_2fa_disabled(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test backup codes regeneration when 2FA is disabled should fail."""
        # Act - try to regenerate backup codes without 2FA enabled
        response = await async_client.post(
            "/auth/2fa/backup-codes/regenerate",
            headers=authenticated_headers,
            json={"otp": "123456", "current_password": PW},
        )

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "2FA must be enabled" in data["detail"]

    @pytest.mark.asyncio
    async def test_regenerate_backup_codes_invalid_otp(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
        monkeypatch,
    ) -> None:
        """Test backup codes regeneration with invalid OTP should fail."""
        from sandloom_identity.services.twofa_service import TwoFAService

        # Mock 2FA as enabled
        async def mock_is_enabled(user, db):
            return True

        # Mock verify_2fa_code to return False (invalid OTP)
        async def mock_verify_2fa_code(user, otp, db):
            return False

        monkeypatch.setattr(TwoFAService, "is_2fa_enabled", mock_is_enabled)
        monkeypatch.setattr(TwoFAService, "verify_2fa_code", mock_verify_2fa_code)

        # Act - try to regenerate backup codes with invalid OTP
        response = await async_client.post(
            "/auth/2fa/backup-codes/regenerate",
            headers=authenticated_headers,
            json={"otp": "000000", "current_password": PW},
        )

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "Invalid OTP code" in data["detail"]

    @pytest.mark.asyncio
    async def test_regenerate_backup_codes_success_scenario(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
        monkeypatch,
    ) -> None:
        """Test backup codes regeneration success scenario with valid OTP."""
        from sandloom_identity.schemas.twofa import TwoFAStatusOut
        from sandloom_identity.services.twofa_service import TwoFAService

        # Mock 2FA as enabled
        async def mock_get_2fa_status(user, db):
            return TwoFAStatusOut(enabled=True, has_backup_codes=True)

        # Mock verify_2fa_code to return True (valid OTP)
        async def mock_verify_2fa_code(user, otp, db):
            return True

        # Mock regenerate backup codes
        async def mock_regenerate_backup_codes(user, db, actor=None, **kwargs):
            return ["CODE1-ABCD", "CODE2-EFGH", "CODE3-IJKL"]

        monkeypatch.setattr(TwoFAService, "get_2fa_status", mock_get_2fa_status)
        monkeypatch.setattr(TwoFAService, "verify_2fa_code", mock_verify_2fa_code)
        monkeypatch.setattr(
            TwoFAService, "regenerate_backup_codes", mock_regenerate_backup_codes
        )

        # Act - regenerate backup codes with valid OTP
        response = await async_client.post(
            "/auth/2fa/backup-codes/regenerate",
            headers=authenticated_headers,
            json={"otp": "123456", "current_password": PW},
        )

        # Assert success response
        assert response.status_code == 200
        data = response.json()
        assert "backup_codes" in data
        assert isinstance(data["backup_codes"], list)
        assert len(data["backup_codes"]) == 3

    @pytest.mark.asyncio
    async def test_regenerate_backup_codes_missing_otp(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test backup codes regeneration without OTP should fail validation."""
        # Act - try to regenerate backup codes without OTP
        response = await async_client.post(
            "/auth/2fa/backup-codes/regenerate", headers=authenticated_headers, json={}
        )

        # Assert validation error
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data
        # Should contain validation error about missing 'otp' field


class TestTwoFAAPIFlow:
    """Integration tests for complete 2FA flows."""

    @pytest.mark.asyncio
    async def test_complete_2fa_flow_invalid_codes(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test complete 2FA flow with invalid codes (realistic scenario)."""
        # Step 1: Check initial status (should be disabled)
        status_response = await async_client.get(
            "/auth/2fa/status", headers=authenticated_headers
        )
        assert status_response.status_code == 200
        # Note: enabled status depends on user's current state

        # Step 2: Initialize 2FA
        init_response = await async_client.post(
            "/auth/2fa/init", json=REAUTH, headers=authenticated_headers
        )
        assert init_response.status_code == 200
        init_data = init_response.json()
        assert "secret" in init_data
        assert "qr_code" in init_data
        assert "backup_codes" in init_data

        # Step 3: Try to enable with invalid OTP (realistic scenario)
        enable_response = await async_client.post(
            "/auth/2fa/enable",
            json={"otp": "000000", "current_password": PW},  # Invalid OTP
            headers=authenticated_headers,
        )
        # Should fail because the OTP is invalid
        assert enable_response.status_code == 400

        # Step 4: Verify 2FA code (should be invalid)
        verify_response = await async_client.post(
            "/auth/2fa/verify",
            json={"otp": "111111", "current_password": PW},
            headers=authenticated_headers,
        )
        assert verify_response.status_code == 200
        verify_data = verify_response.json()
        assert verify_data["valid"] is False

        # Step 5: Generate backup codes (should work regardless of 2FA status)
        backup_response = await async_client.get(
            "/auth/2fa/backup-codes", headers=authenticated_headers
        )
        assert backup_response.status_code == 200
        backup_data = backup_response.json()
        assert "backup_codes" in backup_data
        assert len(backup_data["backup_codes"]) > 0

    @pytest.mark.asyncio
    async def test_all_endpoints_consistency(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test that all 2FA endpoints are consistent and accessible."""
        endpoints_to_test = [
            ("GET", "/auth/2fa/status", None, [200]),  # Always works
            ("POST", "/auth/2fa/init", REAUTH, [200]),  # Always works
            (
                "POST",
                "/auth/2fa/enable",
                {"otp": "123456", "current_password": PW},
                [400, 404],
            ),  # No setup = 400 or 404
            (
                "POST",
                "/auth/2fa/disable",
                {"otp": "123456", "current_password": PW},
                [400, 404],
            ),  # No setup = 400 or 404
            (
                "POST",
                "/auth/2fa/verify",
                {"otp": "123456", "current_password": PW},
                [200],
            ),  # Always works
            ("GET", "/auth/2fa/backup-codes", None, [200]),  # Always works
        ]

        for method, endpoint, payload, expected_codes in endpoints_to_test:
            if method == "GET":
                response = await async_client.get(
                    endpoint, headers=authenticated_headers
                )
            else:  # POST
                response = await async_client.post(
                    endpoint, json=payload, headers=authenticated_headers
                )

            # All endpoints should be accessible (not 404 routing error)
            # and require authentication (not 401 with headers)
            assert response.status_code != 401, f"Endpoint {endpoint} auth failed"
            assert response.status_code in expected_codes, (
                f"Endpoint {endpoint} returned {response.status_code}, "
                f"expected one of {expected_codes}"
            )


class TestTwoFAStepUp:
    """Changing the second factor requires re-authentication."""

    @pytest.mark.asyncio
    async def test_init_without_password_rejected(
        self, async_client: AsyncClient, authenticated_headers: dict[str, str]
    ) -> None:
        response = await async_client.post(
            "/auth/2fa/init", headers=authenticated_headers
        )
        assert response.status_code == 401
        assert "password" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_init_with_wrong_password_rejected(
        self, async_client: AsyncClient, authenticated_headers: dict[str, str]
    ) -> None:
        response = await async_client.post(
            "/auth/2fa/init",
            json={"current_password": "not-my-password"},
            headers=authenticated_headers,
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_disable_without_password_rejected(
        self, async_client: AsyncClient, authenticated_headers: dict[str, str]
    ) -> None:
        response = await async_client.post(
            "/auth/2fa/disable", json={"otp": "123456"}, headers=authenticated_headers
        )
        assert response.status_code == 401
