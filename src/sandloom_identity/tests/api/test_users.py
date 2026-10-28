"""
Integration tests for Users API endpoints.
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
class TestUsersAPIIntegration:
    """Integration tests for users API endpoints."""

    # === List Users Tests ===

    @pytest.mark.asyncio
    async def test_list_users_as_admin_success(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test listing users as admin."""
        # Create some test users
        user1 = User(
            email="user1@example.com",
            full_name="User One",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        user2 = User(
            email="user2@example.com",
            full_name="User Two",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(user1)
        test_db.add(user2)
        await test_db.commit()

        # Make the API call
        response = await async_client.get("/api/v1/users", headers=admin_headers)

        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # Should have at least our test users + admin user from fixture
        assert len(data) >= 3

        # Check that our test users are in the response
        emails = [user["email"] for user in data]
        assert "user1@example.com" in emails
        assert "user2@example.com" in emails

    @pytest.mark.asyncio
    async def test_list_users_as_regular_user_forbidden(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
    ):
        """Test listing users as regular user should be forbidden."""
        response = await async_client.get(
            "/api/v1/users", headers=authenticated_headers
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_list_users_unauthenticated(
        self,
        async_client: AsyncClient,
    ):
        """Test listing users without authentication."""
        response = await async_client.get("/api/v1/users")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_list_users_with_pagination(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test listing users with pagination parameters."""
        # Create multiple test users
        for i in range(5):
            user = User(
                email=f"testuser{i}@example.com",
                full_name=f"Test User {i}",
                hashed_password="dummy_hash",
                is_verified=True,
            )
            test_db.add(user)
        await test_db.commit()

        # Test with limit
        response = await async_client.get(
            "/api/v1/users?limit=3", headers=admin_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) <= 3

        # Test with skip
        response = await async_client.get(
            "/api/v1/users?skip=1&limit=2", headers=admin_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) <= 2

    # === Get User by ID Tests ===

    @pytest.mark.asyncio
    async def test_get_own_user_success(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        test_user: User,
        authenticated_headers: dict[str, str],
    ):
        """Test getting own user profile."""
        response = await async_client.get(
            f"/api/v1/users/{test_user.id}", headers=authenticated_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(test_user.id)
        assert data["email"] == test_user.email
        assert data["full_name"] == test_user.full_name
        assert "hashed_password" not in data

    @pytest.mark.asyncio
    async def test_get_other_user_as_admin_success(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test getting other user as admin."""
        # Create a target user
        target_user = User(
            email="target@example.com",
            full_name="Target User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(target_user)
        await test_db.commit()
        await test_db.refresh(target_user)

        response = await async_client.get(
            f"/api/v1/users/{target_user.id}", headers=admin_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(target_user.id)
        assert data["email"] == target_user.email

    @pytest.mark.asyncio
    async def test_get_other_user_as_regular_user_forbidden(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        authenticated_headers: dict[str, str],
    ):
        """Test getting other user as regular user should be forbidden."""
        # Create another user
        other_user = User(
            email="other@example.com",
            full_name="Other User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(other_user)
        await test_db.commit()
        await test_db.refresh(other_user)

        response = await async_client.get(
            f"/api/v1/users/{other_user.id}", headers=authenticated_headers
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_get_user_not_found(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
    ):
        """Test getting non-existent user."""
        fake_user_id = uuid4()
        response = await async_client.get(
            f"/api/v1/users/{fake_user_id}", headers=admin_headers
        )

        assert response.status_code == 404
        error_data = response.json()
        assert error_data["detail"] == "User not found"

    @pytest.mark.asyncio
    async def test_get_user_invalid_uuid(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
    ):
        """Test getting user with invalid UUID."""
        response = await async_client.get(
            "/api/v1/users/invalid-uuid", headers=admin_headers
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_get_user_unauthenticated(
        self,
        async_client: AsyncClient,
        test_user: User,
    ):
        """Test getting user without authentication."""
        response = await async_client.get(f"/api/v1/users/{test_user.id}")

        assert response.status_code == 401

    # === Update User Tests ===

    @pytest.mark.asyncio
    async def test_update_own_user_success(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        test_user: User,
        authenticated_headers: dict[str, str],
    ):
        """Test updating own user profile."""
        update_data = {
            "full_name": "Updated Full Name",
            "first_name": "Updated",
            "last_name": "Name",
            "username": "updated_username",
        }

        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            json=update_data,
            headers=authenticated_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["full_name"] == "Updated Full Name"
        assert data["first_name"] == "Updated"
        assert data["last_name"] == "Name"
        assert data["username"] == "updated_username"

        # Verify database was updated
        await test_db.refresh(test_user)
        assert test_user.full_name == "Updated Full Name"
        assert test_user.first_name == "Updated"
        assert test_user.last_name == "Name"
        assert test_user.username == "updated_username"

    @pytest.mark.asyncio
    async def test_update_other_user_as_admin_success(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test updating other user as admin."""
        # Create target user
        target_user = User(
            email="target@example.com",
            full_name="Original Name",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(target_user)
        await test_db.commit()
        await test_db.refresh(target_user)

        update_data = {"full_name": "Admin Updated Name"}

        response = await async_client.put(
            f"/api/v1/users/{target_user.id}",
            json=update_data,
            headers=admin_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["full_name"] == "Admin Updated Name"

    @pytest.mark.asyncio
    async def test_update_other_user_as_regular_user_forbidden(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        authenticated_headers: dict[str, str],
    ):
        """Test updating other user as regular user should be forbidden."""
        # Create other user
        other_user = User(
            email="other@example.com",
            full_name="Other User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(other_user)
        await test_db.commit()
        await test_db.refresh(other_user)

        update_data = {"full_name": "Attempted Update"}

        response = await async_client.put(
            f"/api/v1/users/{other_user.id}",
            json=update_data,
            headers=authenticated_headers,
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_update_user_is_active_as_admin(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test admin can update is_active field."""
        # Create target user
        target_user = User(
            email="target@example.com",
            full_name="Target User",
            hashed_password="dummy_hash",
            is_verified=True,
            is_active=True,
        )
        test_db.add(target_user)
        await test_db.commit()
        await test_db.refresh(target_user)

        update_data = {"is_active": False}

        response = await async_client.put(
            f"/api/v1/users/{target_user.id}",
            json=update_data,
            headers=admin_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is False

        # Verify database was updated
        await test_db.refresh(target_user)
        assert target_user.is_active is False

    @pytest.mark.asyncio
    async def test_update_user_is_active_as_regular_user_ignored(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        test_user: User,
        authenticated_headers: dict[str, str],
    ):
        """Test regular user cannot update is_active field (it should be ignored)."""
        original_is_active = test_user.is_active
        update_data = {"is_active": not original_is_active, "full_name": "Updated Name"}

        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            json=update_data,
            headers=authenticated_headers,
        )

        assert response.status_code == 200
        data = response.json()
        # is_active should remain unchanged
        assert data["is_active"] == original_is_active
        # But other fields should be updated
        assert data["full_name"] == "Updated Name"

    @pytest.mark.asyncio
    async def test_update_user_not_found(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
    ):
        """Test updating non-existent user."""
        fake_user_id = uuid4()
        update_data = {"full_name": "Updated Name"}

        response = await async_client.put(
            f"/api/v1/users/{fake_user_id}",
            json=update_data,
            headers=admin_headers,
        )

        assert response.status_code == 404
        error_data = response.json()
        assert error_data["detail"] == "User not found"

    @pytest.mark.asyncio
    async def test_update_user_invalid_data(
        self,
        async_client: AsyncClient,
        test_user: User,
        authenticated_headers: dict[str, str],
    ):
        """Test updating user with invalid data."""
        update_data = {"full_name": ""}  # Empty string should fail validation

        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            json=update_data,
            headers=authenticated_headers,
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_update_user_partial_update(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        test_user: User,
        authenticated_headers: dict[str, str],
    ):
        """Test partial update of user (only some fields)."""
        original_full_name = test_user.full_name
        update_data = {"first_name": "Only First Name"}

        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            json=update_data,
            headers=authenticated_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["first_name"] == "Only First Name"
        # Other fields should remain unchanged
        assert data["full_name"] == original_full_name

    # === Delete User Tests ===

    @pytest.mark.asyncio
    async def test_delete_user_as_admin_success(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test deleting user as admin (soft delete)."""
        # Create target user
        target_user = User(
            email="todelete@example.com",
            full_name="To Delete User",
            hashed_password="dummy_hash",
            is_verified=True,
            is_active=True,
        )
        test_db.add(target_user)
        await test_db.commit()
        await test_db.refresh(target_user)

        response = await async_client.delete(
            f"/api/v1/users/{target_user.id}", headers=admin_headers
        )

        assert response.status_code == 204
        assert response.content == b""

        # Verify soft delete - user should be inactive
        await test_db.refresh(target_user)
        assert target_user.is_active is False

    @pytest.mark.asyncio
    async def test_delete_user_as_regular_user_forbidden(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        authenticated_headers: dict[str, str],
    ):
        """Test deleting user as regular user should be forbidden."""
        # Create target user
        target_user = User(
            email="todelete@example.com",
            full_name="To Delete User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(target_user)
        await test_db.commit()
        await test_db.refresh(target_user)

        response = await async_client.delete(
            f"/api/v1/users/{target_user.id}", headers=authenticated_headers
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_user_not_found(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
    ):
        """Test deleting non-existent user."""
        fake_user_id = uuid4()
        response = await async_client.delete(
            f"/api/v1/users/{fake_user_id}", headers=admin_headers
        )

        assert response.status_code == 404
        error_data = response.json()
        assert error_data["detail"] == "User not found"

    @pytest.mark.asyncio
    async def test_delete_user_unauthenticated(
        self,
        async_client: AsyncClient,
        test_user: User,
    ):
        """Test deleting user without authentication."""
        response = await async_client.delete(f"/api/v1/users/{test_user.id}")

        assert response.status_code == 401

    # === Get User Roles Tests ===

    @pytest.mark.asyncio
    async def test_get_own_user_roles_success(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        test_user: User,
        authenticated_headers: dict[str, str],
    ):
        """Test getting own user roles."""
        # Create a role and assign it to test user
        role = Role(
            name="Test Role",
            code="test_role",
            description="Test role description",
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(role)

        user_role = UserRole(user_id=test_user.id, role_id=role.id)
        test_db.add(user_role)
        await test_db.commit()

        response = await async_client.get(
            f"/api/v1/users/{test_user.id}/roles", headers=authenticated_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1

        # Find our test role
        test_role_data = next((r for r in data if r["code"] == "test_role"), None)
        assert test_role_data is not None
        assert test_role_data["name"] == "Test Role"

    @pytest.mark.asyncio
    async def test_get_other_user_roles_as_admin_success(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test getting other user roles as admin."""
        # Create target user
        target_user = User(
            email="target@example.com",
            full_name="Target User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(target_user)

        # Create role
        role = Role(
            name="Target Role",
            code="target_role",
            description="Target role description",
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(target_user)
        await test_db.refresh(role)

        # Assign role to target user
        user_role = UserRole(user_id=target_user.id, role_id=role.id)
        test_db.add(user_role)
        await test_db.commit()

        response = await async_client.get(
            f"/api/v1/users/{target_user.id}/roles", headers=admin_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

        # Should find our target role
        role_codes = [r["code"] for r in data]
        assert "target_role" in role_codes

    @pytest.mark.asyncio
    async def test_get_other_user_roles_as_regular_user_forbidden(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        authenticated_headers: dict[str, str],
    ):
        """Test getting other user roles as regular user should be forbidden."""
        # Create other user
        other_user = User(
            email="other@example.com",
            full_name="Other User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(other_user)
        await test_db.commit()
        await test_db.refresh(other_user)

        response = await async_client.get(
            f"/api/v1/users/{other_user.id}/roles", headers=authenticated_headers
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_get_user_roles_empty_list(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test getting roles for user with no roles."""
        # Create user with no roles
        user_no_roles = User(
            email="noroles@example.com",
            full_name="No Roles User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(user_no_roles)
        await test_db.commit()
        await test_db.refresh(user_no_roles)

        response = await async_client.get(
            f"/api/v1/users/{user_no_roles.id}/roles", headers=admin_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0

    @pytest.mark.asyncio
    async def test_get_user_roles_unauthenticated(
        self,
        async_client: AsyncClient,
        test_user: User,
    ):
        """Test getting user roles without authentication."""
        response = await async_client.get(f"/api/v1/users/{test_user.id}/roles")

        assert response.status_code == 401


@pytest.mark.integration
class TestUsersAPIEdgeCases:
    """Edge cases and additional scenarios for users API."""

    @pytest.mark.asyncio
    async def test_admin_can_access_any_user_endpoint(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test that admin can access any user-related endpoint."""
        # Create multiple test users
        users = []
        for i in range(3):
            user = User(
                email=f"testuser{i}@example.com",
                full_name=f"Test User {i}",
                hashed_password="dummy_hash",
                is_verified=True,
            )
            test_db.add(user)
            users.append(user)
        await test_db.commit()

        for user in users:
            await test_db.refresh(user)

            # Admin should be able to get any user
            response = await async_client.get(
                f"/api/v1/users/{user.id}", headers=admin_headers
            )
            assert response.status_code == 200

            # Admin should be able to update any user
            response = await async_client.put(
                f"/api/v1/users/{user.id}",
                json={"full_name": f"Updated {user.full_name}"},
                headers=admin_headers,
            )
            assert response.status_code == 200

            # Admin should be able to get any user's roles
            response = await async_client.get(
                f"/api/v1/users/{user.id}/roles", headers=admin_headers
            )
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_user_data_integrity_after_update(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        test_user: User,
        authenticated_headers: dict[str, str],
    ):
        """Test that user data integrity is maintained after updates."""
        original_email = test_user.email
        original_id = test_user.id

        # Get original data from API to ensure format consistency
        orig_response = await async_client.get(
            f"/api/v1/users/{test_user.id}",
            headers=authenticated_headers,
        )
        original_api_data = orig_response.json()
        original_created_at = original_api_data["created_at"]

        update_data = {
            "full_name": "New Full Name",
            "first_name": "New First",
            "last_name": "New Last",
            "username": "new_username",
        }

        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            json=update_data,
            headers=authenticated_headers,
        )

        assert response.status_code == 200
        data = response.json()

        # These should be updated
        assert data["full_name"] == "New Full Name"
        assert data["first_name"] == "New First"
        assert data["last_name"] == "New Last"
        assert data["username"] == "new_username"

        # These should remain unchanged
        assert data["email"] == original_email
        assert data["id"] == str(original_id)
        assert data["created_at"] == original_created_at

        # updated_at should be changed
        assert data["updated_at"] != original_created_at

    @pytest.mark.asyncio
    async def test_sensitive_data_not_exposed(
        self,
        async_client: AsyncClient,
        test_user: User,
        authenticated_headers: dict[str, str],
    ):
        """Test that sensitive data is not exposed in API responses."""
        response = await async_client.get(
            f"/api/v1/users/{test_user.id}", headers=authenticated_headers
        )

        assert response.status_code == 200
        data = response.json()

        # Sensitive fields should not be present
        assert "hashed_password" not in data
        assert "password" not in data

        # Only safe fields should be present
        expected_fields = {
            "id",
            "email",
            "full_name",
            "first_name",
            "last_name",
            "username",
            "is_active",
            "is_superuser",
            "is_verified",
            "created_at",
            "updated_at",
            "last_login_at",
            "email_verified_at",
        }
        actual_fields = set(data.keys())
        assert actual_fields == expected_fields


class TestUsersAPIAdditionalCoverage:
    """Additional tests to improve coverage for edge cases."""

    @pytest.mark.asyncio
    async def test_get_nonexistent_user_as_admin(
        self, async_client: AsyncClient, admin_headers: dict[str, str]
    ):
        """Test getting a non-existent user as admin returns 404."""
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        response = await async_client.get(
            f"/api/v1/users/{fake_uuid}", headers=admin_headers
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "User not found"

    @pytest.mark.asyncio
    async def test_update_nonexistent_user_as_admin(
        self, async_client: AsyncClient, admin_headers: dict[str, str]
    ):
        """Test updating a non-existent user as admin returns 404."""
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        update_data = {"full_name": "New Name"}
        response = await async_client.put(
            f"/api/v1/users/{fake_uuid}",
            headers=admin_headers,
            json=update_data,
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "User not found"

    @pytest.mark.asyncio
    async def test_delete_nonexistent_user_as_admin(
        self, async_client: AsyncClient, admin_headers: dict[str, str]
    ):
        """Test deleting a non-existent user as admin returns 404."""
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        response = await async_client.delete(
            f"/api/v1/users/{fake_uuid}", headers=admin_headers
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "User not found"

    @pytest.mark.asyncio
    async def test_get_roles_for_nonexistent_user_as_admin(
        self, async_client: AsyncClient, admin_headers: dict[str, str]
    ):
        """Test getting roles for non-existent user returns empty list."""
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        response = await async_client.get(
            f"/api/v1/users/{fake_uuid}/roles", headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_update_user_all_fields_separately(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        authenticated_headers: dict[str, str],
        test_user: User,
    ):
        """Test updating each field separately to cover all update branches."""
        # Test full_name update only
        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            headers=authenticated_headers,
            json={"full_name": "New Full Name"},
        )
        assert response.status_code == 200
        assert response.json()["full_name"] == "New Full Name"

        # Test first_name update only
        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            headers=authenticated_headers,
            json={"first_name": "New First"},
        )
        assert response.status_code == 200
        assert response.json()["first_name"] == "New First"

        # Test last_name update only
        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            headers=authenticated_headers,
            json={"last_name": "New Last"},
        )
        assert response.status_code == 200
        assert response.json()["last_name"] == "New Last"

        # Test username update only
        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            headers=authenticated_headers,
            json={"username": "new_username"},
        )
        assert response.status_code == 200
        assert response.json()["username"] == "new_username"

    @pytest.mark.asyncio
    async def test_admin_can_activate_deactivate_user(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
        test_user: User,
    ):
        """Test admin can change is_active status."""
        # Deactivate user
        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            headers=admin_headers,
            json={"is_active": False},
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False

        # Activate user back
        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            headers=admin_headers,
            json={"is_active": True},
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is True

    @pytest.mark.asyncio
    async def test_regular_user_cannot_change_is_active(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        authenticated_headers: dict[str, str],
        test_user: User,
    ):
        """Test regular user cannot change is_active status - it's ignored."""
        original_status = test_user.is_active

        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            headers=authenticated_headers,
            json={"is_active": not original_status, "full_name": "Updated Name"},
        )
        assert response.status_code == 200
        # is_active should remain unchanged
        assert response.json()["is_active"] == original_status
        # but other fields should be updated
        assert response.json()["full_name"] == "Updated Name"

    @pytest.mark.asyncio
    async def test_admin_role_check_helper_function(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        authenticated_headers: dict[str, str],
        admin_headers: dict[str, str],
    ):
        """Test the _user_has_admin_role helper function through API calls."""
        # Create a user without admin role
        regular_user = User(
            email="regular@example.com",
            full_name="Regular User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(regular_user)
        await test_db.commit()
        await test_db.refresh(regular_user)

        # Create another user that regular user tries to access
        target_user = User(
            email="target@example.com",
            full_name="Target User",
            hashed_password="dummy_hash",
            is_verified=True,
        )
        test_db.add(target_user)
        await test_db.commit()
        await test_db.refresh(target_user)

        # Regular user should not be able to access another user
        response = await async_client.get(
            f"/api/v1/users/{target_user.id}", headers=authenticated_headers
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Access denied"

        # But admin should be able to access the user
        response = await async_client.get(
            f"/api/v1/users/{target_user.id}", headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()["email"] == "target@example.com"

    @pytest.mark.asyncio
    async def test_update_with_none_values_are_ignored(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_user: User,
    ):
        """Test that None values in update request are ignored."""
        original_name = test_user.full_name

        # Update with None values should not change anything
        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            headers=authenticated_headers,
            json={
                "full_name": None,
                "first_name": None,
                "last_name": None,
                "username": None,
                "is_active": None,
            },
        )
        assert response.status_code == 200
        # Full name should remain unchanged
        assert response.json()["full_name"] == original_name

    @pytest.mark.asyncio
    async def test_delete_user_soft_delete_behavior(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test that delete operation performs soft delete (sets is_active=False)."""
        # Create a user to delete
        user_to_delete = User(
            email="delete@example.com",
            full_name="Delete Me",
            hashed_password="dummy_hash",
            is_verified=True,
            is_active=True,
        )
        test_db.add(user_to_delete)
        await test_db.commit()
        await test_db.refresh(user_to_delete)

        # Delete the user
        response = await async_client.delete(
            f"/api/v1/users/{user_to_delete.id}", headers=admin_headers
        )
        assert response.status_code == 204

        # User should still exist in database but be inactive
        result = await test_db.execute(select(User).where(User.id == user_to_delete.id))
        deleted_user = result.scalar_one_or_none()
        assert deleted_user is not None
        assert deleted_user.is_active is False


class TestUsersAPIMaxCoverage:
    """Tests specifically designed to hit remaining uncovered lines."""

    @pytest.mark.asyncio
    async def test_update_user_individual_field_coverage(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_user: User,
    ):
        """Test individual field updates to hit all if statements in update logic."""
        # Test each field update individually to hit all the if statements

        # Update only full_name (hits line ~100)
        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            headers=authenticated_headers,
            json={"full_name": "Only Full Name Changed"},
        )
        assert response.status_code == 200
        assert response.json()["full_name"] == "Only Full Name Changed"

        # Update only first_name (hits line ~102)
        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            headers=authenticated_headers,
            json={"first_name": "Only First Changed"},
        )
        assert response.status_code == 200
        assert response.json()["first_name"] == "Only First Changed"

        # Update only last_name (hits line ~104)
        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            headers=authenticated_headers,
            json={"last_name": "Only Last Changed"},
        )
        assert response.status_code == 200
        assert response.json()["last_name"] == "Only Last Changed"

        # Update only username (hits line ~106)
        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            headers=authenticated_headers,
            json={"username": "only_username_changed"},
        )
        assert response.status_code == 200
        assert response.json()["username"] == "only_username_changed"

    @pytest.mark.asyncio
    async def test_delete_user_commit_and_no_return(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        admin_headers: dict[str, str],
    ):
        """Test delete operation to hit commit and implicit return."""
        # Create user to delete
        user_to_delete = User(
            email="delete_coverage@example.com",
            full_name="Delete Coverage User",
            hashed_password="dummy_hash",
            is_verified=True,
            is_active=True,
        )
        test_db.add(user_to_delete)
        await test_db.commit()
        await test_db.refresh(user_to_delete)

        # Perform delete - should hit soft delete logic
        response = await async_client.delete(
            f"/api/v1/users/{user_to_delete.id}", headers=admin_headers
        )
        assert response.status_code == 204

        # Verify the soft delete worked
        result = await test_db.execute(select(User).where(User.id == user_to_delete.id))
        deleted_user = result.scalar_one_or_none()
        assert deleted_user is not None
        assert deleted_user.is_active is False

    @pytest.mark.asyncio
    async def test_list_users_return_coverage(
        self, async_client: AsyncClient, admin_headers: dict[str, str]
    ):
        """Test list users to hit the return statement."""
        response = await async_client.get("/api/v1/users", headers=admin_headers)
        assert response.status_code == 200
        # This should hit the return statement at the end of list_users

    @pytest.mark.asyncio
    async def test_get_user_by_id_return_coverage(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_user: User,
    ):
        """Test get user by id to hit return statement."""
        response = await async_client.get(
            f"/api/v1/users/{test_user.id}", headers=authenticated_headers
        )
        assert response.status_code == 200
        # This should hit the return statement at the end of get_user_by_id

    @pytest.mark.asyncio
    async def test_update_user_return_coverage(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_user: User,
    ):
        """Test update user to hit return statement and commit."""
        response = await async_client.put(
            f"/api/v1/users/{test_user.id}",
            headers=authenticated_headers,
            json={"full_name": "Coverage Test"},
        )
        assert response.status_code == 200
        # This should hit commit and return statement in update_user

    @pytest.mark.asyncio
    async def test_get_user_roles_return_coverage(
        self,
        async_client: AsyncClient,
        authenticated_headers: dict[str, str],
        test_user: User,
    ):
        """Test get user roles to hit return statement."""
        response = await async_client.get(
            f"/api/v1/users/{test_user.id}/roles", headers=authenticated_headers
        )
        assert response.status_code == 200
        # This should hit the return statement at the end of get_user_roles
