"""
Simple test to verify PostgreSQL test database setup.
"""

import pytest
from sqlalchemy import text


class TestDatabaseSetup:
    """Test database connectivity and basic operations."""

    @pytest.mark.asyncio
    async def test_database_connection(self, test_db):
        """Test that we can connect to the test database."""
        # Try to execute a simple query
        result = await test_db.execute(text("SELECT 1 as test_value"))
        row = result.fetchone()
        assert row[0] == 1

    @pytest.mark.asyncio
    async def test_user_creation(self, test_user):
        """Test that we can create users in the test database."""
        assert test_user.id is not None
        assert test_user.email == "testuser@example.com"
        assert test_user.full_name == "Test User"
        assert test_user.is_active is True
        assert test_user.is_verified is True

    @pytest.mark.asyncio
    async def test_admin_user_creation(self, admin_user):
        """Test that we can create admin users in the test database."""
        assert admin_user.id is not None
        assert admin_user.email == "admin@example.com"
        assert admin_user.full_name == "Admin User"
        assert admin_user.is_active is True
        assert admin_user.is_verified is True
        assert admin_user.is_superuser is True

    @pytest.mark.asyncio
    async def test_auth_headers_generation(self, authenticated_headers):
        """Test that we can generate authentication headers."""
        assert "Authorization" in authenticated_headers
        assert authenticated_headers["Authorization"].startswith("Bearer ")

        # Extract token and verify it's not a fake one
        token = authenticated_headers["Authorization"].replace("Bearer ", "")
        assert token != "test-access-token"
        assert len(token) > 20  # JWT tokens are longer

        # Verify JWT structure (header.payload.signature)
        token_parts = token.split(".")
        assert len(token_parts) == 3

    def test_test_config(self):
        """Test that test configuration is accessible."""
        from sandloom_identity.tests.conftest import TestConfig

        user_data = TestConfig.get_test_user_data()
        assert user_data["email"] == "newuser@example.com"
        assert "password" in user_data

        admin_data = TestConfig.get_admin_user_data()
        assert admin_data["email"] == "newadmin@example.com"
        assert "password" in admin_data
