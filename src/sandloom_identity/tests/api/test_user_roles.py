"""
Integration tests for User-Role assignment API endpoints.
Tests the complete flow: HTTP -> Router -> Guards -> Services -> Database
"""

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_identity.models.role import Role
from sandloom_identity.models.user import User
from sandloom_identity.models.user_role import UserRole


@pytest.mark.integration
class TestUserRolesAPIIntegration:
    """Integration tests for user roles API endpoints."""

    # === Assign Role Tests ===

    @pytest.mark.asyncio
    async def test_assign_role_success(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test successful role assignment."""
        # Create test user and role
        user = User(
            email="testuser@example.com",
            full_name="Test User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(user)

        role = Role(
            name="Editor",
            code="editor",
            description="Editor role",
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(user)
        await test_db.refresh(role)

        # Make the API call
        response = await async_client.post(
            f"/api/v1/users/{user.id}/roles/{role.id}",
            headers=admin_headers,
        )

        # Verify response
        assert response.status_code == 204
        assert response.content == b""

        # Verify database state
        result = await test_db.execute(
            select(UserRole).where(
                UserRole.user_id == user.id, UserRole.role_id == role.id
            )
        )
        user_role = result.scalar_one_or_none()
        assert user_role is not None
        assert user_role.user_id == user.id
        assert user_role.role_id == role.id

    @pytest.mark.asyncio
    async def test_assign_role_user_not_found(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test role assignment when user doesn't exist."""
        # Create only role, not user
        role = Role(
            name="Editor",
            code="editor",
            description="Editor role",
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(role)

        # Try to assign role to non-existent user
        fake_user_id = uuid4()
        response = await async_client.post(
            f"/api/v1/users/{fake_user_id}/roles/{role.id}",
            headers=admin_headers,
        )

        # Verify error response
        assert response.status_code == 404
        error_data = response.json()
        assert error_data["detail"] == "User not found"

    @pytest.mark.asyncio
    async def test_assign_role_role_not_found(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test role assignment when role doesn't exist."""
        # Create only user, not role
        user = User(
            email="testuser@example.com",
            full_name="Test User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(user)
        await test_db.commit()
        await test_db.refresh(user)

        # Try to assign non-existent role
        fake_role_id = uuid4()
        response = await async_client.post(
            f"/api/v1/users/{user.id}/roles/{fake_role_id}",
            headers=admin_headers,
        )

        # Verify error response
        assert response.status_code == 404
        error_data = response.json()
        assert error_data["detail"] == "Role not found"

    @pytest.mark.asyncio
    async def test_assign_role_already_assigned(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test role assignment when role is already assigned."""
        # Create test user and role
        user = User(
            email="testuser@example.com",
            full_name="Test User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(user)

        role = Role(
            name="Editor",
            code="editor",
            description="Editor role",
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(user)
        await test_db.refresh(role)

        # Pre-assign the role
        user_role = UserRole(user_id=user.id, role_id=role.id)
        test_db.add(user_role)
        await test_db.commit()

        # Try to assign the same role again
        response = await async_client.post(
            f"/api/v1/users/{user.id}/roles/{role.id}",
            headers=admin_headers,
        )

        # Verify error response
        assert response.status_code == 400
        error_data = response.json()
        assert error_data["detail"] == "Role already assigned to user"

    @pytest.mark.asyncio
    async def test_assign_role_unauthorized_non_admin(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        authenticated_headers: dict[str, str],
    ):
        """Test role assignment fails for non-admin user."""
        # Create test user and role
        user = User(
            email="testuser_nonadmin@example.com",  # Unique email
            full_name="Test User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(user)

        role = Role(
            name="Editor",
            code="editor",
            description="Editor role",
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(user)
        await test_db.refresh(role)

        # Try to assign role with non-admin headers
        response = await async_client.post(
            f"/api/v1/users/{user.id}/roles/{role.id}",
            headers=authenticated_headers,  # Regular user, not admin
        )

        # Verify access denied
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_assign_role_unauthenticated(
        self, async_client: AsyncClient, test_db: AsyncSession
    ):
        """Test role assignment fails for unauthenticated user."""
        # Create test user and role
        user = User(
            email="testuser@example.com",
            full_name="Test User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(user)

        role = Role(
            name="Editor",
            code="editor",
            description="Editor role",
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(user)
        await test_db.refresh(role)

        # Try to assign role without authentication
        response = await async_client.post(
            f"/api/v1/users/{user.id}/roles/{role.id}",
            # No headers provided
        )

        # Verify authentication required
        assert response.status_code == 401

    # === Revoke Role Tests ===

    @pytest.mark.asyncio
    async def test_revoke_role_success(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test successful role revocation."""
        # Create test user and role
        user = User(
            email="testuser@example.com",
            full_name="Test User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(user)

        role = Role(
            name="Editor",
            code="editor",
            description="Editor role",
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(user)
        await test_db.refresh(role)

        # Pre-assign the role
        user_role = UserRole(user_id=user.id, role_id=role.id)
        test_db.add(user_role)
        await test_db.commit()

        # Make the API call
        response = await async_client.delete(
            f"/api/v1/users/{user.id}/roles/{role.id}",
            headers=admin_headers,
        )

        # Verify response
        assert response.status_code == 204
        assert response.content == b""

        # Verify database state - assignment should be removed
        result = await test_db.execute(
            select(UserRole).where(
                UserRole.user_id == user.id, UserRole.role_id == role.id
            )
        )
        user_role = result.scalar_one_or_none()
        assert user_role is None

    @pytest.mark.asyncio
    async def test_revoke_role_assignment_not_found(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test role revocation when assignment doesn't exist."""
        # Create test user and role but no assignment
        user = User(
            email="testuser@example.com",
            full_name="Test User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(user)

        role = Role(
            name="Editor",
            code="editor",
            description="Editor role",
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(user)
        await test_db.refresh(role)

        # Try to revoke non-existent assignment
        response = await async_client.delete(
            f"/api/v1/users/{user.id}/roles/{role.id}",
            headers=admin_headers,
        )

        # Verify error response
        assert response.status_code == 404
        error_data = response.json()
        assert error_data["detail"] == "Role assignment not found"

    @pytest.mark.asyncio
    async def test_revoke_role_unauthorized_non_admin(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        authenticated_headers: dict[str, str],
    ):
        """Test role revocation fails for non-admin user."""
        # Create test user and role with assignment
        user = User(
            email="testuser_revoke_nonadmin@example.com",  # Unique email
            full_name="Test User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(user)

        role = Role(
            name="Editor",
            code="editor",
            description="Editor role",
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(user)
        await test_db.refresh(role)

        # Pre-assign the role
        user_role = UserRole(user_id=user.id, role_id=role.id)
        test_db.add(user_role)
        await test_db.commit()

        # Try to revoke role with non-admin headers
        response = await async_client.delete(
            f"/api/v1/users/{user.id}/roles/{role.id}",
            headers=authenticated_headers,  # Regular user, not admin
        )

        # Verify access denied
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_revoke_role_unauthenticated(
        self, async_client: AsyncClient, test_db: AsyncSession
    ):
        """Test role revocation fails for unauthenticated user."""
        # Create test user and role with assignment
        user = User(
            email="testuser@example.com",
            full_name="Test User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(user)

        role = Role(
            name="Editor",
            code="editor",
            description="Editor role",
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(user)
        await test_db.refresh(role)

        # Pre-assign the role
        user_role = UserRole(user_id=user.id, role_id=role.id)
        test_db.add(user_role)
        await test_db.commit()

        # Try to revoke role without authentication
        response = await async_client.delete(
            f"/api/v1/users/{user.id}/roles/{role.id}",
            # No headers provided
        )

        # Verify authentication required
        assert response.status_code == 401

    # === Invalid UUID Tests ===

    @pytest.mark.asyncio
    async def test_assign_role_invalid_user_uuid(
        self, async_client: AsyncClient, admin_headers: dict[str, str]
    ):
        """Test role assignment with invalid user UUID."""
        role_id = uuid4()
        response = await async_client.post(
            f"/api/v1/users/invalid-uuid/roles/{role_id}",
            headers=admin_headers,
        )

        # Should return 422 for validation error
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_assign_role_invalid_role_uuid(
        self, async_client: AsyncClient, admin_headers: dict[str, str]
    ):
        """Test role assignment with invalid role UUID."""
        user_id = uuid4()
        response = await async_client.post(
            f"/api/v1/users/{user_id}/roles/invalid-uuid",
            headers=admin_headers,
        )

        # Should return 422 for validation error
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_revoke_role_invalid_user_uuid(
        self, async_client: AsyncClient, admin_headers: dict[str, str]
    ):
        """Test role revocation with invalid user UUID."""
        role_id = uuid4()
        response = await async_client.delete(
            f"/api/v1/users/invalid-uuid/roles/{role_id}",
            headers=admin_headers,
        )

        # Should return 422 for validation error
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_revoke_role_invalid_role_uuid(
        self, async_client: AsyncClient, admin_headers: dict[str, str]
    ):
        """Test role revocation with invalid role UUID."""
        user_id = uuid4()
        response = await async_client.delete(
            f"/api/v1/users/{user_id}/roles/invalid-uuid",
            headers=admin_headers,
        )

        # Should return 422 for validation error
        assert response.status_code == 422


@pytest.mark.integration
class TestUserRolesAPIEdgeCases:
    """Edge cases and additional scenarios for user roles API."""

    @pytest.mark.asyncio
    async def test_assign_role_to_inactive_user(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test role assignment to inactive user."""
        # Create inactive user
        user = User(
            email="inactive@example.com",
            full_name="Inactive User",
            hashed_password="dummy_hash",
            is_verified=True,
            is_active=False,  # Inactive user
        )
        test_db.add(user)

        role = Role(
            name="Editor",
            code="editor",
            description="Editor role",
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(user)
        await test_db.refresh(role)

        # Should still work - admin can assign roles to inactive users
        response = await async_client.post(
            f"/api/v1/users/{user.id}/roles/{role.id}",
            headers=admin_headers,
        )

        assert response.status_code == 204

        # Verify database state
        result = await test_db.execute(
            select(UserRole).where(
                UserRole.user_id == user.id, UserRole.role_id == role.id
            )
        )
        user_role = result.scalar_one_or_none()
        assert user_role is not None

    @pytest.mark.asyncio
    async def test_multiple_role_assignments(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test multiple role assignments to same user."""
        # Create user and multiple roles
        user = User(
            email="multiuser@example.com",
            full_name="Multi Role User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(user)

        role1 = Role(name="Editor", code="editor", description="Editor role")
        role2 = Role(name="Viewer", code="viewer", description="Viewer role")
        test_db.add(role1)
        test_db.add(role2)
        await test_db.commit()
        await test_db.refresh(user)
        await test_db.refresh(role1)
        await test_db.refresh(role2)

        # Assign first role
        response1 = await async_client.post(
            f"/api/v1/users/{user.id}/roles/{role1.id}",
            headers=admin_headers,
        )
        assert response1.status_code == 204

        # Assign second role
        response2 = await async_client.post(
            f"/api/v1/users/{user.id}/roles/{role2.id}",
            headers=admin_headers,
        )
        assert response2.status_code == 204

        # Verify both assignments exist
        result = await test_db.execute(
            select(UserRole).where(UserRole.user_id == user.id)
        )
        user_roles = result.scalars().all()
        assert len(user_roles) == 2
        role_ids = {ur.role_id for ur in user_roles}
        assert role1.id in role_ids
        assert role2.id in role_ids

    @pytest.mark.asyncio
    async def test_assign_and_revoke_role_workflow(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test complete workflow: assign -> verify -> revoke -> verify."""
        # Create user and role
        user = User(
            email="workflow@example.com",
            full_name="Workflow User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(user)

        role = Role(
            name="Temporary",
            code="temporary",
            description="Temporary role",
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(user)
        await test_db.refresh(role)

        # Step 1: Assign role
        assign_response = await async_client.post(
            f"/api/v1/users/{user.id}/roles/{role.id}",
            headers=admin_headers,
        )
        assert assign_response.status_code == 204

        # Step 2: Verify assignment exists
        result = await test_db.execute(
            select(UserRole).where(
                UserRole.user_id == user.id, UserRole.role_id == role.id
            )
        )
        user_role = result.scalar_one_or_none()
        assert user_role is not None

        # Step 3: Revoke role
        revoke_response = await async_client.delete(
            f"/api/v1/users/{user.id}/roles/{role.id}",
            headers=admin_headers,
        )
        assert revoke_response.status_code == 204

        # Step 4: Verify assignment is removed
        result = await test_db.execute(
            select(UserRole).where(
                UserRole.user_id == user.id, UserRole.role_id == role.id
            )
        )
        user_role = result.scalar_one_or_none()
        assert user_role is None
