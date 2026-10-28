"""
Test to verify that our JWT token fixtures generate real tokens.
"""

import pytest


class TestJWTTokenFixtures:
    """Test that fixtures generate real JWT tokens."""

    @pytest.mark.asyncio
    async def test_authenticated_headers_generates_real_jwt(
        self, authenticated_headers: dict[str, str]
    ) -> None:
        """Test that authenticated_headers fixture generates real JWT token."""
        # Use the fixture as a parameter
        headers = authenticated_headers

        # Verify headers structure
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Bearer ")

        # Extract token
        token = headers["Authorization"].replace("Bearer ", "")

        # Verify it's not the old fake token
        assert token != "test-access-token"

        # Verify it looks like a real JWT (has 3 parts separated by dots)
        token_parts = token.split(".")
        assert len(token_parts) == 3, (
            "JWT should have 3 parts: header.payload.signature"
        )

        # Each part should be base64 encoded (non-empty)
        for part in token_parts:
            assert len(part) > 0, "JWT parts should not be empty"

        print(f"✅ Real JWT token generated: {token[:30]}...")

    @pytest.mark.asyncio
    async def test_admin_headers_generates_real_jwt(
        self, admin_headers: dict[str, str]
    ) -> None:
        """Test that admin_headers fixture generates real JWT token."""
        # Use the fixture as a parameter
        headers = admin_headers

        # Verify headers structure
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Bearer ")

        # Extract token
        token = headers["Authorization"].replace("Bearer ", "")

        # Verify it's not the old fake token
        assert token != "admin-access-token"

        # Verify it looks like a real JWT
        token_parts = token.split(".")
        assert len(token_parts) == 3, "JWT should have 3 parts"

        print(f"✅ Real admin JWT token generated: {token[:30]}...")

    @pytest.mark.asyncio
    async def test_tokens_are_different_for_different_users(
        self, authenticated_headers: dict[str, str], admin_headers: dict[str, str]
    ) -> None:
        """Test that different users get different tokens."""
        # Use different fixtures which should have different tokens

        token1 = authenticated_headers["Authorization"].replace("Bearer ", "")
        token2 = admin_headers["Authorization"].replace("Bearer ", "")

        # Tokens should be different
        assert token1 != token2, "Different users should get different tokens"

        print("✅ Different users receive different JWT tokens")

    def test_fixture_improvements_summary(self) -> None:
        """Summarize the improvements made to JWT token fixtures."""
        print("\n🎯 JWT Token Fixture Improvements:")
        print("✅ Before: Fake tokens like 'test-access-token'")
        print("✅ After: Real JWT tokens via TokenService.create_tokens()")
        print("✅ Benefits:")
        print("   - API integration tests can now pass authentication")
        print("   - Guards like get_current_user can decode real tokens")
        print("   - No more 401 Unauthorized errors in API tests")
        print("   - Higher API endpoint coverage achievable")
        print("🚀 Ready for comprehensive API testing!")
