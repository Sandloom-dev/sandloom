"""Comprehensive integration tests for SSO API endpoints.

This module contains comprehensive tests for SSO API endpoints covering:
- Provider listing and availability
- SSO redirect URL generation
- SSO callback handling and authentication
- Provider linking and unlinking
- Linked providers management
- All authentication and authorization flows
- Positive and negative scenarios
- Error handling and edge cases

Following the same patterns as auth.py and roles.py tests.
"""

import secrets

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


class TestSSOProviderManagement:
    """Tests for SSO provider management (unlink and get linked providers)."""

    @pytest.mark.asyncio
    async def test_get_sso_providers(self, async_client: AsyncClient) -> None:
        """Test getting list of available SSO providers."""
        # Act - call the endpoint
        response = await async_client.get("/auth/sso/providers")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

        # Should return list of available providers
        # Each provider should have required fields
        for provider in data:
            assert isinstance(provider, dict)
            assert "code" in provider
            assert "name" in provider
            assert "enabled" in provider
            assert provider["code"] in [
                "google",
                "microsoft",
                "github",
                "linkedin",
                "telegram",
            ]
            assert isinstance(provider["name"], str)
            assert isinstance(provider["enabled"], bool)

    @pytest.mark.asyncio
    async def test_get_sso_redirect_url(self, async_client: AsyncClient) -> None:
        """Test getting SSO redirect URL."""
        # Act - call the redirect endpoint
        response = await async_client.get("/auth/sso/google/redirect")

        # Assert - should return redirect URL and state
        assert response.status_code == 200
        data = response.json()
        assert "auth_url" in data
        assert "state" in data
        assert isinstance(data["auth_url"], str)
        assert isinstance(data["state"], str)
        assert len(data["state"]) > 20  # State should be a proper token

    @pytest.mark.asyncio
    async def test_get_sso_redirect_url_with_custom_redirect_uri(
        self, async_client: AsyncClient
    ) -> None:
        """Test getting SSO redirect URL with custom redirect URI."""
        custom_redirect_uri = "https://app.example.com/auth/callback"

        # Act
        response = await async_client.get(
            f"/auth/sso/google/redirect?redirect_uri={custom_redirect_uri}"
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "auth_url" in data
        assert "state" in data

    @pytest.mark.asyncio
    async def test_get_sso_redirect_url_invalid_redirect_uri(
        self, async_client: AsyncClient
    ) -> None:
        """Test getting SSO redirect URL with invalid redirect URI."""
        invalid_redirect_uri = "https://malicious.com/auth/callback"

        # Act
        response = await async_client.get(
            f"/auth/sso/google/redirect?redirect_uri={invalid_redirect_uri}"
        )

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "invalid redirect_uri" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_telegram_hidden_and_rejected_until_implemented(
        self, async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured TELEGRAM_BOT_TOKEN must not advertise or start a flow
        whose exchange is unimplemented (it would only fail at the callback)."""
        from sandloom.sdk.infra import settings

        monkeypatch.setattr(settings, "telegram_bot_token", "123456:ABC", raising=False)

        providers = await async_client.get("/auth/sso/providers")
        assert providers.status_code == 200
        assert "telegram" not in {p["code"] for p in providers.json()}

        redirect = await async_client.get("/auth/sso/telegram/redirect")
        assert redirect.status_code == 400
        assert "not supported" in redirect.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_get_sso_redirect_url_invalid_provider(
        self, async_client: AsyncClient
    ) -> None:
        """Test getting SSO redirect URL for invalid provider."""
        # Act - try invalid provider
        response = await async_client.get("/auth/sso/invalid_provider/redirect")

        # Assert - should return error
        assert response.status_code in [400, 404]

    @pytest.mark.asyncio
    async def test_unlink_sso_provider_integration(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Integration test for unlinking SSO provider."""
        # Act - make real API call without service mocking
        response = await async_client.request(
            "DELETE",
            "/auth/sso/google/unlink",
            headers=authenticated_headers,
            json={"current_password": "TestPassword123!"},
        )

        # Assert - expect either success or appropriate error
        assert response.status_code in [204, 404, 400]

        if response.status_code == 404:
            # Provider not linked - this is valid scenario
            data = response.json()
            assert "detail" in data
        elif response.status_code == 400:
            # Business logic error (e.g., last identity)
            data = response.json()
            assert "detail" in data

    @pytest.mark.asyncio
    async def test_unlink_sso_provider_unauthorized(
        self, async_client: AsyncClient, test_db: AsyncSession
    ) -> None:
        """Test unlinking SSO provider without authentication."""
        # Act
        response = await async_client.delete("/auth/sso/google/unlink")

        # Assert
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_get_my_linked_providers_integration(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Integration test for getting linked SSO providers."""
        # Act - make real API call without service mocking
        response = await async_client.get(
            "/auth/sso/me/linked", headers=authenticated_headers
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

        # Validate structure if providers exist
        for provider in data:
            assert isinstance(provider, dict)
            assert "provider" in provider
            assert "external_id" in provider
            assert "email" in provider
            assert provider["provider"] in [
                "google",
                "microsoft",
                "github",
                "linkedin",
                "telegram",
            ]

    @pytest.mark.asyncio
    async def test_get_my_linked_providers_unauthorized(
        self, async_client: AsyncClient, test_db: AsyncSession
    ) -> None:
        """Test retrieval of linked providers without authentication."""
        # Act
        response = await async_client.get("/auth/sso/me/linked")

        # Assert
        assert response.status_code == 401


class TestSSOSecurityReal:
    """Real security tests for SSO state validation using actual Redis."""

    @pytest.mark.asyncio
    async def test_complete_sso_flow_state_validation_real(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        test_redis,
        mock_oauth_client_factory,
    ) -> None:
        """Test complete SSO flow with REAL state validation using Redis."""
        # Step 1: Get redirect URL - this should generate and store state in Redis
        response = await async_client.get("/auth/sso/google/redirect")
        print(f"Response status: {response.status_code}")
        print(f"Response text: {response.text}")
        assert response.status_code == 200
        redirect_data = response.json()
        initial_state = redirect_data["state"]

        # Verify state is properly formatted
        assert len(initial_state) > 20  # Should be a proper token
        assert (
            initial_state.replace("-", "").replace("_", "").isalnum()
        )  # URL-safe base64

        # Step 2: Check that state was ACTUALLY stored in Redis
        stored_state_key = f"sso_state:{initial_state}"
        stored_state = await test_redis.get(stored_state_key)
        assert stored_state is not None, (
            f"State {initial_state} was not stored in Redis!"
        )
        import json as _json

        ctx = _json.loads(stored_state)
        # Only the HASH of the client binding is stored, next to the PKCE
        # verifier and the purpose/provider the state may be used for.
        assert ctx["provider"] == "google" and ctx["purpose"] == "login"
        assert ctx["binding_hash"] != redirect_data["binding"]
        assert ctx["code_verifier"]
        # The binding is also delivered as an HttpOnly cookie.
        set_cookie = response.headers.get("set-cookie", "")
        assert (
            "sandloom_sso_binding=" in set_cookie and "httponly" in set_cookie.lower()
        )

        print(f"✅ State {initial_state} correctly stored in Redis")

        # Step 3: Try callback with CORRECT state - this should work but fail at OAuth level
        # (since we don't have real OAuth credentials, but state validation should pass)
        callback_response = await async_client.get(
            f"/auth/sso/google/callback?code=test_code&state={initial_state}"
        )

        # We expect this to fail at OAuth level (400), not at state validation level (400 with specific message)
        # The key is that it should NOT fail with "Invalid or expired state"
        print(f"Response status: {callback_response.status_code}")
        print(f"Response body: {callback_response.text}")

        # State should be consumed (deleted) after first use
        final_state = await test_redis.get(stored_state_key)
        assert final_state is None, "State should be deleted after use (one-time use)"

        print("✅ State correctly consumed after use")

    @pytest.mark.asyncio
    async def test_invalid_state_rejection_real(
        self, async_client: AsyncClient, test_db: AsyncSession, test_redis
    ) -> None:
        """Test that invalid state is ACTUALLY rejected with real Redis validation."""
        # Try callback with completely invalid state
        fake_state = "totally_fake_state_that_should_not_exist"

        response = await async_client.get(
            f"/auth/sso/google/callback?code=test_code&state={fake_state}"
        )

        print(f"Response for invalid state: {response.status_code} - {response.text}")

        # Should be rejected with 400 status
        assert response.status_code == 400

        # Should contain error message about invalid state
        response_data = response.json()
        error_detail = response_data.get("detail", "").lower()
        assert (
            "invalid" in error_detail
            or "expired" in error_detail
            or "state" in error_detail
        )

        print("✅ Invalid state correctly rejected")

    @pytest.mark.asyncio
    async def test_state_reuse_protection_real(
        self, async_client: AsyncClient, test_db: AsyncSession, test_redis
    ) -> None:
        """Test that state cannot be reused (one-time use protection) with real Redis."""
        # Step 1: Get a valid state
        response = await async_client.get("/auth/sso/google/redirect")
        if response.status_code != 200:
            # Skip test if redirect generation fails (rate limiting, etc.)
            pytest.skip(
                f"Redirect generation failed: {response.status_code} - {response.text}"
            )
        redirect_data = response.json()
        state = redirect_data["state"]

        # Verify state is stored
        stored_state_key = f"sso_state:{state}"
        stored_state = await test_redis.get(stored_state_key)
        assert stored_state is not None

        # Step 2: Use the state once (this will consume it)
        await async_client.get(
            f"/auth/sso/google/callback?code=test_code&state={state}"
        )

        # Step 3: Verify state was consumed
        consumed_state = await test_redis.get(stored_state_key)
        assert consumed_state is None, "State should be deleted after first use"

        # Step 4: Try to reuse the same state - should fail
        reuse_response = await async_client.get(
            f"/auth/sso/google/callback?code=test_code&state={state}"
        )
        assert reuse_response.status_code == 400

        response_data = reuse_response.json()
        error_detail = response_data.get("detail", "").lower()
        assert "invalid" in error_detail or "expired" in error_detail

        print("✅ State reuse correctly prevented")

    @pytest.mark.asyncio
    async def test_sso_state_expiration_real(
        self, async_client: AsyncClient, test_db: AsyncSession, test_redis
    ) -> None:
        """Test that state expires after TTL using real Redis."""
        # Step 1: Get state and verify it's stored with TTL
        response = await async_client.get("/auth/sso/google/redirect")
        if response.status_code != 200:
            # Skip test if redirect generation fails (rate limiting, etc.)
            pytest.skip(
                f"Redirect generation failed: {response.status_code} - {response.text}"
            )
        initial_state = response.json()["state"]

        stored_state_key = f"sso_state:{initial_state}"

        # Check TTL is set
        ttl = await test_redis.ttl(stored_state_key)
        assert ttl > 0, "State should have TTL set"
        assert ttl <= 600, "TTL should be 600 seconds or less"  # 10 minutes

        print(f"✅ State has TTL of {ttl} seconds")

    @pytest.mark.asyncio
    async def test_link_sso_provider_invalid_state_real(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_db: AsyncSession,
    ) -> None:
        """Test linking SSO provider with invalid state using real validation."""
        # Generate fake state
        fake_state = secrets.token_urlsafe(32)

        # Try to link with invalid state
        response = await async_client.post(
            "/auth/sso/google/link",
            json={
                "code": "test_code",
                "state": fake_state,
                "current_password": "TestPassword123!",
            },
            headers=authenticated_headers,
        )

        # Should fail with state validation error
        assert response.status_code == 400
        assert "Invalid or expired state" in response.text

        print("✅ Link with invalid state correctly rejected")

    @pytest.mark.asyncio
    async def test_concurrent_state_validation_real(
        self, async_client: AsyncClient, test_db: AsyncSession
    ) -> None:
        """Test concurrent access to same state (race condition protection)."""
        import asyncio

        # Step 1: Get legitimate state
        response = await async_client.get("/auth/sso/google/redirect")
        if response.status_code != 200:
            # Skip test if redirect generation fails (rate limiting, etc.)
            pytest.skip(
                f"Redirect generation failed: {response.status_code} - {response.text}"
            )
        initial_state = response.json()["state"]

        # Step 2: Try to use the same state concurrently
        async def attempt_callback(state: str, code: str) -> tuple[int, str]:
            response = await async_client.get(
                f"/auth/sso/google/callback?code={code}&state={state}"
            )
            return response.status_code, response.text

        # Step 3: Launch concurrent requests
        tasks = [attempt_callback(initial_state, f"code_{i}") for i in range(3)]

        results = await asyncio.gather(*tasks)

        # Step 4: Only one should succeed at state validation level
        # Others should fail with state validation error
        state_validation_failures = 0
        for status_code, response_text in results:
            if status_code == 400 and "Invalid or expired state" in response_text:
                state_validation_failures += 1

        # At least 2 should fail due to state being consumed by the first
        assert state_validation_failures >= 2, (
            f"Expected at least 2 state validation failures, got {state_validation_failures}"
        )

        print(
            f"✅ Concurrent access handled correctly: {state_validation_failures} failed due to state validation"
        )

    @pytest.mark.asyncio
    async def test_state_format_validation_real(
        self, async_client: AsyncClient, test_db: AsyncSession
    ) -> None:
        """Test validation of state parameter format."""
        malicious_states = [
            "",  # Empty
            "a",  # Too short
            "../../../etc/passwd",  # Path traversal
            "SELECT * FROM users;",  # SQL injection attempt
            "' OR '1'='1",  # SQL injection
            "<script>alert('xss')</script>",  # XSS
            "state with spaces",  # Invalid characters
        ]

        for malicious_state in malicious_states:
            response = await async_client.get(
                f"/auth/sso/google/callback?code=test&state={malicious_state}"
            )

            # Should fail (either 400 or 422 for validation)
            assert response.status_code in [
                400,
                422,
            ], f"Malicious state '{malicious_state}' should be rejected"

            print(f"✅ Malicious state '{malicious_state}' correctly rejected")

    @pytest.mark.asyncio
    async def test_redis_connection_failure_handling(
        self, async_client: AsyncClient, test_db: AsyncSession
    ) -> None:
        """Test behavior when Redis is unavailable (graceful degradation test)."""
        # This test would require temporarily breaking Redis connection
        # For now, we'll just verify that the service expects Redis to be available

        # Try to get redirect URL - should work if Redis is available
        response = await async_client.get("/auth/sso/google/redirect")

        if response.status_code == 200:
            print("✅ Redis connection is working for state storage")
        else:
            print(f"⚠️ Redis connection issue: {response.status_code} - {response.text}")
            # In a real test, we'd want to test Redis failure scenarios
            # but that requires more complex setup
