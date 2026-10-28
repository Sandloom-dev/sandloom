"""
Comprehensive integration tests for Roles API endpoints.
Tests the complete flow: HTTP -> Router -> Guards -> Services -> Database
"""

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_identity.models.permission import Permission
from sandloom_identity.models.role import Role
from sandloom_identity.models.role_permission import RolePermission


@pytest.mark.integration
class TestRolesAPIComprehensive:
    """Comprehensive integration tests for Roles API."""

    # === Role Management Tests ===

    @pytest.mark.asyncio
    async def test_create_role_success(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test successful role creation by admin."""
        role_data = {
            "name": "Test Role",
            "code": "test_role",
            "description": "Test role for API testing",
        }

        response = await async_client.post(
            "/api/v1/roles", json=role_data, headers=admin_headers
        )

        assert response.status_code == 201
        data = response.json()

        # Check response structure
        assert "id" in data
        assert data["name"] == role_data["name"]
        assert data["code"] == role_data["code"]
        assert data["description"] == role_data["description"]
        assert data["is_system"] is False
        assert "created_at" in data
        assert "updated_at" in data

        # Verify role was created in database
        result = await test_db.execute(
            select(Role).where(Role.code == role_data["code"])
        )
        db_role = result.scalar_one_or_none()
        assert db_role is not None
        assert db_role.name == role_data["name"]
        assert db_role.code == role_data["code"]
        assert db_role.description == role_data["description"]

    @pytest.mark.asyncio
    async def test_create_role_unauthorized(self, async_client: AsyncClient):
        """Test role creation without authentication."""
        role_data = {
            "name": "Test Role",
            "code": "test_role",
            "description": "Test role",
        }

        response = await async_client.post("/api/v1/roles", json=role_data)

        assert response.status_code == 401
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_create_role_forbidden_regular_user(
        self, async_client: AsyncClient, authenticated_headers: dict[str, str]
    ):
        """Test role creation as regular user (should be forbidden)."""
        role_data = {
            "name": "Test Role",
            "code": "test_role",
            "description": "Test role",
        }

        response = await async_client.post(
            "/api/v1/roles", json=role_data, headers=authenticated_headers
        )

        assert response.status_code == 403
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_create_role_duplicate_code(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test creating role with duplicate code."""
        # Create first role
        existing_role = Role(
            name="Existing Role", code="duplicate_code", description="First role"
        )
        test_db.add(existing_role)
        await test_db.commit()

        # Try to create role with same code
        role_data = {
            "name": "Second Role",
            "code": "duplicate_code",
            "description": "Second role",
        }

        response = await async_client.post(
            "/api/v1/roles", json=role_data, headers=admin_headers
        )

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "already exists" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_create_role_invalid_data(
        self, async_client: AsyncClient, admin_headers: dict[str, str]
    ):
        """Test creating role with invalid data."""
        role_data = {
            "name": "",  # Empty name
            "code": "invalid-code-with-spaces",  # Invalid code format
        }

        response = await async_client.post(
            "/api/v1/roles", json=role_data, headers=admin_headers
        )

        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_list_roles_success(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test listing all roles as admin."""
        # Create test roles
        roles = [
            Role(name="Role 1", code="role_1", description="First role"),
            Role(name="Role 2", code="role_2", description="Second role"),
        ]
        for role in roles:
            test_db.add(role)
        await test_db.commit()

        response = await async_client.get("/api/v1/roles", headers=admin_headers)

        assert response.status_code == 200
        data = response.json()

        assert isinstance(data, list)
        assert len(data) >= 2  # At least our test roles

        # Find our test roles in response
        role_codes = {role.get("code") for role in data if isinstance(role, dict)}
        assert "role_1" in role_codes
        assert "role_2" in role_codes

    @pytest.mark.asyncio
    async def test_list_roles_unauthorized(self, async_client: AsyncClient):
        """Test listing roles without authentication."""
        response = await async_client.get("/api/v1/roles")

        assert response.status_code == 401
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_get_role_success(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test getting specific role with permissions."""
        # Create role with permissions
        role = Role(name="Test Role", code="test_role", description="Test role")
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(role)

        # Create permission and assign to role
        permission = Permission(
            name="Test Permission",
            code="test_permission",
            description="Test permission",
        )
        test_db.add(permission)
        await test_db.commit()
        await test_db.refresh(permission)

        # Create role-permission relationship
        role_perm = RolePermission(role_id=role.id, permission_id=permission.id)
        test_db.add(role_perm)
        await test_db.commit()

        response = await async_client.get(
            f"/api/v1/roles/{role.id}", headers=admin_headers
        )

        assert response.status_code == 200
        data = response.json()

        assert data["id"] == str(role.id)
        assert data["name"] == role.name
        assert data["code"] == role.code
        assert "permissions" in data
        assert len(data["permissions"]) >= 1

        # Check permission details
        perm_codes = {
            perm.get("code")
            for perm in data.get("permissions", [])
            if isinstance(perm, dict)
        }
        assert "test_permission" in perm_codes

    @pytest.mark.asyncio
    async def test_get_role_not_found(
        self, async_client: AsyncClient, admin_headers: dict[str, str]
    ):
        """Test getting non-existent role."""
        non_existent_id = uuid4()

        response = await async_client.get(
            f"/api/v1/roles/{non_existent_id}", headers=admin_headers
        )

        assert response.status_code == 404
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_update_role_success(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test successful role update."""
        # Create role
        role = Role(
            name="Original Role",
            code="original_role",
            description="Original description",
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(role)

        update_data = {"name": "Updated Role", "description": "Updated description"}

        response = await async_client.put(
            f"/api/v1/roles/{role.id}", json=update_data, headers=admin_headers
        )

        assert response.status_code == 200
        data = response.json()

        assert data["name"] == update_data["name"]
        assert data["description"] == update_data["description"]
        assert data["code"] == role.code  # Code should not change

        # Verify in database
        await test_db.refresh(role)
        assert role.name == update_data["name"]
        assert role.description == update_data["description"]

    @pytest.mark.asyncio
    async def test_update_role_not_found(
        self, async_client: AsyncClient, admin_headers: dict[str, str]
    ):
        """Test updating non-existent role."""
        non_existent_id = uuid4()
        update_data = {"name": "Updated Role"}

        response = await async_client.put(
            f"/api/v1/roles/{non_existent_id}", json=update_data, headers=admin_headers
        )

        assert response.status_code == 404
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_update_system_role_forbidden(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test updating system role (should be forbidden)."""
        # Create system role
        role = Role(
            name="System Role",
            code="system_role",
            description="System role",
            is_system=True,
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(role)

        update_data = {"name": "Updated System Role"}

        response = await async_client.put(
            f"/api/v1/roles/{role.id}", json=update_data, headers=admin_headers
        )

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "system role" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_delete_role_success(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test successful role deletion."""
        # Create role
        role = Role(name="To Delete", code="to_delete", description="Role to delete")
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(role)

        response = await async_client.delete(
            f"/api/v1/roles/{role.id}", headers=admin_headers
        )

        assert response.status_code == 204
        assert response.content == b""

        # Verify role was deleted
        result = await test_db.execute(select(Role).where(Role.id == role.id))
        deleted_role = result.scalar_one_or_none()
        assert deleted_role is None

    @pytest.mark.asyncio
    async def test_delete_role_not_found(
        self, async_client: AsyncClient, admin_headers: dict[str, str]
    ):
        """Test deleting non-existent role."""
        non_existent_id = uuid4()

        response = await async_client.delete(
            f"/api/v1/roles/{non_existent_id}", headers=admin_headers
        )

        assert response.status_code == 404
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_delete_system_role_forbidden(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test deleting system role (should be forbidden)."""
        # Create system role
        role = Role(
            name="System Role",
            code="system_role",
            description="System role",
            is_system=True,
        )
        test_db.add(role)
        await test_db.commit()
        await test_db.refresh(role)

        response = await async_client.delete(
            f"/api/v1/roles/{role.id}", headers=admin_headers
        )

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "system role" in data["detail"].lower()

    # === Permission Management Tests ===

    @pytest.mark.asyncio
    async def test_create_permission_success(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test successful permission creation."""
        permission_data = {
            "name": "Test Permission",
            "code": "test_permission",
            "description": "Test permission for API testing",
        }

        response = await async_client.post(
            "/api/v1/permissions", json=permission_data, headers=admin_headers
        )

        assert response.status_code == 201
        data = response.json()

        assert "id" in data
        assert data["name"] == permission_data["name"]
        assert data["code"] == permission_data["code"]
        assert data["description"] == permission_data["description"]

        # Verify in database
        result = await test_db.execute(
            select(Permission).where(Permission.code == permission_data["code"])
        )
        db_permission = result.scalar_one_or_none()
        assert db_permission is not None

    @pytest.mark.asyncio
    async def test_create_permission_duplicate_code(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test creating permission with duplicate code."""
        # Create existing permission
        existing_permission = Permission(
            name="Existing Permission",
            code="duplicate_permission",
            description="First permission",
        )
        test_db.add(existing_permission)
        await test_db.commit()

        permission_data = {
            "name": "Second Permission",
            "code": "duplicate_permission",
            "description": "Second permission",
        }

        response = await async_client.post(
            "/api/v1/permissions", json=permission_data, headers=admin_headers
        )

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "already exists" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_list_permissions_success(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test listing all permissions."""
        # Create test permissions
        permissions = [
            Permission(
                name="Permission 1", code="perm_1", description="First permission"
            ),
            Permission(
                name="Permission 2", code="perm_2", description="Second permission"
            ),
        ]
        for permission in permissions:
            test_db.add(permission)
        await test_db.commit()

        response = await async_client.get("/api/v1/permissions", headers=admin_headers)

        assert response.status_code == 200
        data = response.json()

        assert isinstance(data, list)
        assert len(data) >= 2  # At least our test permissions

        # Find our test permissions in response
        perm_codes = {perm.get("code") for perm in data if isinstance(perm, dict)}
        assert "perm_1" in perm_codes
        assert "perm_2" in perm_codes

    # === Role-Permission Assignment Tests ===

    @pytest.mark.asyncio
    async def test_assign_permission_to_role_success(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test successful permission assignment to role."""
        # Create role and permission
        role = Role(name="Test Role", code="test_role", description="Test role")
        permission = Permission(
            name="Test Permission", code="test_perm", description="Test permission"
        )

        test_db.add(role)
        test_db.add(permission)
        await test_db.commit()
        await test_db.refresh(role)
        await test_db.refresh(permission)

        response = await async_client.post(
            f"/api/v1/roles/{role.id}/permissions/{permission.id}",
            headers=admin_headers,
        )

        assert response.status_code == 204
        assert response.content == b""

        # Verify assignment in database
        result = await test_db.execute(
            select(RolePermission).where(
                RolePermission.role_id == role.id,
                RolePermission.permission_id == permission.id,
            )
        )
        assignment = result.scalar_one_or_none()
        assert assignment is not None

    @pytest.mark.asyncio
    async def test_assign_permission_to_role_not_found(
        self, async_client: AsyncClient, admin_headers: dict[str, str]
    ):
        """Test assigning permission to non-existent role."""
        role_id = uuid4()
        permission_id = uuid4()

        response = await async_client.post(
            f"/api/v1/roles/{role_id}/permissions/{permission_id}",
            headers=admin_headers,
        )

        assert response.status_code == 404
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_assign_permission_already_assigned(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test assigning permission that's already assigned."""
        # Create role, permission, and existing assignment
        role = Role(name="Test Role", code="test_role", description="Test role")
        permission = Permission(
            name="Test Permission", code="test_perm", description="Test permission"
        )

        test_db.add(role)
        test_db.add(permission)
        await test_db.commit()
        await test_db.refresh(role)
        await test_db.refresh(permission)

        # Create existing assignment
        assignment = RolePermission(role_id=role.id, permission_id=permission.id)
        test_db.add(assignment)
        await test_db.commit()

        response = await async_client.post(
            f"/api/v1/roles/{role.id}/permissions/{permission.id}",
            headers=admin_headers,
        )

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "already assigned" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_revoke_permission_from_role_success(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test successful permission revocation from role."""
        # Create role, permission, and assignment
        role = Role(name="Test Role", code="test_role", description="Test role")
        permission = Permission(
            name="Test Permission", code="test_perm", description="Test permission"
        )

        test_db.add(role)
        test_db.add(permission)
        await test_db.commit()
        await test_db.refresh(role)
        await test_db.refresh(permission)

        assignment = RolePermission(role_id=role.id, permission_id=permission.id)
        test_db.add(assignment)
        await test_db.commit()

        response = await async_client.delete(
            f"/api/v1/roles/{role.id}/permissions/{permission.id}",
            headers=admin_headers,
        )

        assert response.status_code == 204
        assert response.content == b""

        # Verify assignment was removed
        result = await test_db.execute(
            select(RolePermission).where(
                RolePermission.role_id == role.id,
                RolePermission.permission_id == permission.id,
            )
        )
        assignment = result.scalar_one_or_none()
        assert assignment is None

    @pytest.mark.asyncio
    async def test_revoke_permission_not_assigned(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test revoking permission that's not assigned."""
        # Create role and permission without assignment
        role = Role(name="Test Role", code="test_role", description="Test role")
        permission = Permission(
            name="Test Permission", code="test_perm", description="Test permission"
        )

        test_db.add(role)
        test_db.add(permission)
        await test_db.commit()
        await test_db.refresh(role)
        await test_db.refresh(permission)

        response = await async_client.delete(
            f"/api/v1/roles/{role.id}/permissions/{permission.id}",
            headers=admin_headers,
        )

        assert response.status_code == 404
        data = response.json()
        assert "detail" in data

    # === Integration Tests ===

    @pytest.mark.asyncio
    async def test_complete_role_lifecycle(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        test_db: AsyncSession,
    ):
        """Test complete role lifecycle: create -> assign permissions -> update -> delete."""
        # 1. Create role
        role_data = {
            "name": "Lifecycle Role",
            "code": "lifecycle_role",
            "description": "Role for lifecycle testing",
        }

        create_response = await async_client.post(
            "/api/v1/roles", json=role_data, headers=admin_headers
        )
        assert create_response.status_code == 201
        role_id = create_response.json()["id"]

        # 2. Create and assign permission
        permission_data = {
            "name": "Lifecycle Permission",
            "code": "lifecycle_permission",
            "description": "Permission for lifecycle testing",
        }

        perm_response = await async_client.post(
            "/api/v1/permissions", json=permission_data, headers=admin_headers
        )
        assert perm_response.status_code == 201
        permission_id = perm_response.json()["id"]

        assign_response = await async_client.post(
            f"/api/v1/roles/{role_id}/permissions/{permission_id}",
            headers=admin_headers,
        )
        assert assign_response.status_code == 204

        # 3. Get role with permissions
        get_response = await async_client.get(
            f"/api/v1/roles/{role_id}", headers=admin_headers
        )
        assert get_response.status_code == 200
        role_data_with_perms = get_response.json()
        assert len(role_data_with_perms["permissions"]) >= 1

        # 4. Update role
        update_data = {"name": "Updated Lifecycle Role"}
        update_response = await async_client.put(
            f"/api/v1/roles/{role_id}", json=update_data, headers=admin_headers
        )
        assert update_response.status_code == 200
        assert update_response.json()["name"] == update_data["name"]

        # 5. Revoke permission
        revoke_response = await async_client.delete(
            f"/api/v1/roles/{role_id}/permissions/{permission_id}",
            headers=admin_headers,
        )
        assert revoke_response.status_code == 204

        # 6. Delete role
        delete_response = await async_client.delete(
            f"/api/v1/roles/{role_id}", headers=admin_headers
        )
        assert delete_response.status_code == 204

    @pytest.mark.asyncio
    async def test_authorization_across_endpoints(
        self, async_client: AsyncClient, authenticated_headers: dict[str, str]
    ):
        """Test that all endpoints require admin role."""
        endpoints: list[tuple[str, str, dict[str, str] | None]] = [
            ("POST", "/api/v1/roles", {"name": "Test", "code": "test"}),
            ("GET", "/api/v1/roles", None),
            ("GET", f"/api/v1/roles/{uuid4()}", None),
            ("PUT", f"/api/v1/roles/{uuid4()}", {"name": "Updated"}),
            ("DELETE", f"/api/v1/roles/{uuid4()}", None),
            ("POST", "/api/v1/permissions", {"name": "Test", "code": "test"}),
            ("GET", "/api/v1/permissions", None),
            ("POST", f"/api/v1/roles/{uuid4()}/permissions/{uuid4()}", None),
            ("DELETE", f"/api/v1/roles/{uuid4()}/permissions/{uuid4()}", None),
        ]

        for method, url, json_data in endpoints:
            if method == "GET":
                response = await async_client.get(url, headers=authenticated_headers)
            elif method == "POST":
                response = await async_client.post(
                    url, json=json_data, headers=authenticated_headers
                )
            elif method == "PUT":
                response = await async_client.put(
                    url, json=json_data, headers=authenticated_headers
                )
            else:  # DELETE
                response = await async_client.delete(url, headers=authenticated_headers)

            # All should return 403 (Forbidden) for regular users
            assert response.status_code == 403, (
                f"Endpoint {method} {url} should require admin role"
            )

    #     }
    #     response = await async_client.post("/api/v1/roles", json=role_data, headers=admin_headers)
    #     assert response.status_code == 201
    #     data = response.json()
    #     assert data["name"] == role_data["name"]
    #     assert data["code"] == role_data["code"]

    # @pytest.mark.api
    # async def test_create_role_as_regular_user_forbidden(self, async_client, authenticated_headers):
    #     """Test creating role as regular user should be forbidden."""
    #     role_data = {
    #         "name": "Test Role",
    #         "code": "test_role",
    #         "description": "Test role description"
    #     }
    #     response = await async_client.post("/api/v1/roles", json=role_data, headers=authenticated_headers)
    #     assert response.status_code == 403

    # @pytest.mark.api
    # async def test_list_roles_as_admin(self, async_client, admin_headers, test_role):
    #     """Test listing roles as admin."""
    #     response = await async_client.get("/api/v1/roles", headers=admin_headers)
    #     assert response.status_code == 200
    #     data = response.json()
    #     assert isinstance(data, list)
    #     assert len(data) >= 1

    # @pytest.mark.api
    # async def test_get_role_as_admin(self, async_client, admin_headers, test_role):
    #     """Test getting specific role as admin."""
    #     response = await async_client.get(f"/api/v1/roles/{test_role.id}", headers=admin_headers)
    #     assert response.status_code == 200
    #     data = response.json()
    #     assert data["id"] == str(test_role.id)
    #     assert data["name"] == test_role.name

    # @pytest.mark.api
    # async def test_update_role_as_admin(self, async_client, admin_headers, test_role):
    #     """Test updating role as admin."""
    #     update_data = {
    #         "name": "Updated Role Name",
    #         "description": "Updated description"
    #     }
    #     response = await async_client.put(
    #         f"/api/v1/roles/{test_role.id}",
    #         json=update_data,
    #         headers=admin_headers
    #     )
    #     assert response.status_code == 200
    #     data = response.json()
    #     assert data["name"] == update_data["name"]

    # @pytest.mark.api
    # async def test_delete_role_as_admin(self, async_client, admin_headers, test_role):
    #     """Test deleting role as admin."""
    #     response = await async_client.delete(f"/api/v1/roles/{test_role.id}", headers=admin_headers)
    #     assert response.status_code == 204
