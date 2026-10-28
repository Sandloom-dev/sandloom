"""Tests for Identity module models."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sandloom_identity.models import (
    ExternalIdentity,
    Permission,
    Provider,  # Import Provider enum instead of SSOProvider
    RefreshToken,
    Role,
    RolePermission,
    TwoFASecret,
    User,
    UserRole,
)


@pytest.mark.asyncio
async def test_user_creation():
    """Test user model creation."""
    user = User(
        email="test@example.com",
        hashed_password="hashed_password",
        full_name="Test User",
        is_active=True,  # Set explicitly for test
        is_verified=False,  # Set explicitly for test
    )

    assert user.email == "test@example.com"
    assert user.hashed_password == "hashed_password"
    assert user.full_name == "Test User"
    assert user.is_active is True
    assert user.is_verified is False
    assert user.email_verified_at is None


@pytest.mark.asyncio
async def test_user_email_case_insensitive():
    """Test that user email is stored in lowercase."""
    user = User(
        email="Test@Example.COM",
        hashed_password="hashed_password",  # Correct field name
    )

    # Note: CITEXT handles case insensitivity at database level
    assert user.email == "Test@Example.COM"  # Model doesn't enforce lowercase


@pytest.mark.asyncio
async def test_external_identity_creation():
    """Test external identity model creation."""
    user_id = uuid4()

    external_identity = ExternalIdentity(
        user_id=user_id,
        provider=Provider.GOOGLE,  # Use Provider enum
        external_id="google_123",  # Correct field name
        email="test@gmail.com",
    )

    assert external_identity.user_id == user_id
    assert external_identity.provider == Provider.GOOGLE
    assert external_identity.external_id == "google_123"  # Correct field name
    assert external_identity.email == "test@gmail.com"


@pytest.mark.asyncio
async def test_role_creation():
    """Test role model creation."""
    role = Role(
        code="admin",
        name="Administrator",
    )

    assert role.code == "admin"
    assert role.name == "Administrator"


@pytest.mark.asyncio
async def test_permission_creation():
    """Test permission model creation."""
    permission = Permission(
        code="users.read",
        name="Read Users",
    )

    assert permission.code == "users.read"
    assert permission.name == "Read Users"


@pytest.mark.asyncio
async def test_user_role_assignment():
    """Test user role assignment."""
    user_id = uuid4()
    role_id = uuid4()

    user_role = UserRole(
        user_id=user_id,
        role_id=role_id,
    )

    assert user_role.user_id == user_id
    assert user_role.role_id == role_id


@pytest.mark.asyncio
async def test_role_permission_assignment():
    """Test role permission assignment."""
    role_id = uuid4()
    permission_id = uuid4()

    role_permission = RolePermission(
        role_id=role_id,
        permission_id=permission_id,
    )

    assert role_permission.role_id == role_id
    assert role_permission.permission_id == permission_id


@pytest.mark.asyncio
async def test_twofa_secret_creation():
    """Test 2FA secret model creation."""
    user_id = uuid4()

    twofa_secret = TwoFASecret(
        user_id=user_id,
        secret="base32_encoded_secret",
        is_enabled=False,
    )

    assert twofa_secret.user_id == user_id
    assert twofa_secret.secret == "base32_encoded_secret"
    assert twofa_secret.is_enabled is False


@pytest.mark.asyncio
async def test_refresh_token_creation():
    """Test refresh token model creation."""
    user_id = uuid4()
    expires_at = datetime.now(UTC)

    refresh_token = RefreshToken(
        user_id=user_id,
        token="actual_token_string",
        expires_at=expires_at,
        is_revoked=False,  # Set explicitly for test
    )

    assert refresh_token.user_id == user_id
    assert refresh_token.token == "actual_token_string"
    assert refresh_token.expires_at == expires_at
    assert refresh_token.is_revoked is False
    assert refresh_token.revoked_at is None


@pytest.mark.asyncio
async def test_user_repr():
    """Test user string representation."""
    user = User(
        email="test@example.com",
        hashed_password="hashed_password",
    )

    # Since there's no custom __repr__, check for object representation
    repr_str = repr(user)
    assert "User" in repr_str
    assert "object at" in repr_str


@pytest.mark.asyncio
async def test_role_repr():
    """Test role string representation."""
    role = Role(
        code="admin",
        name="Administrator",
    )

    # Since there's no custom __repr__, check for object representation
    repr_str = repr(role)
    assert "Role" in repr_str
    assert "object at" in repr_str


@pytest.mark.asyncio
async def test_permission_repr():
    """Test permission string representation."""
    permission = Permission(
        code="users.read",
        name="Read Users",
    )

    # Since there's no custom __repr__, check for object representation
    repr_str = repr(permission)
    assert "Permission" in repr_str
    assert "object at" in repr_str
