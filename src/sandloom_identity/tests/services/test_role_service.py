"""
Comprehensive tests for RoleService and PermissionService with 100% coverage.
Tests both positive and negative scenarios following best practices.
Fixed async mock issues for reliable test execution based on working UserService pattern.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_identity.events import EventActor
from sandloom_identity.models import Permission, Role, RolePermission, User, UserRole
from sandloom_identity.schemas.rbac import PermissionCreate, RoleCreate, RoleUpdate
from sandloom_identity.services.role_service import PermissionService, RoleService


class TestRoleServiceComprehensive:
    """Comprehensive test cases for RoleService with 100% coverage."""

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        """Mock database session with proper async setup."""
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.add = Mock()
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()
        mock_session.delete = AsyncMock()
        return mock_session

    @pytest.fixture
    def sample_role_data(self) -> RoleCreate:
        """Sample role data for testing."""
        return RoleCreate(
            name="Test Role", code="test_role", description="Test role description"
        )

    @pytest.fixture
    def sample_role(self) -> Mock:
        """Sample role instance."""
        role = Mock(spec=Role)
        role.id = uuid4()
        role.name = "Test Role"
        role.code = "test_role"
        role.description = "Test role description"
        role.is_system = False
        role.created_at = datetime.now(UTC)
        role.updated_at = datetime.now(UTC)
        return role

    @pytest.fixture
    def system_role(self) -> Mock:
        """Sample system role instance."""
        role = Mock(spec=Role)
        role.id = uuid4()
        role.name = "System Role"
        role.code = "system_role"
        role.description = "System role description"
        role.is_system = True
        role.created_at = datetime.now(UTC)
        role.updated_at = datetime.now(UTC)
        return role

    @pytest.fixture
    def sample_actor(self) -> EventActor:
        """Sample event actor."""
        return EventActor(user_id=uuid4(), ip="127.0.0.1")

    # ===== CREATE ROLE TESTS =====
    @pytest.mark.asyncio
    async def test_create_role_success(
        self, mock_db: AsyncMock, sample_role_data: RoleCreate, sample_actor: EventActor
    ) -> None:
        """Test successful role creation with all valid data."""
        # Mock check for existing role (none found)
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act
        result = await RoleService.create_role(sample_role_data, mock_db, sample_actor)

        # Assert
        assert result.name == sample_role_data.name
        assert result.code == sample_role_data.code
        assert result.description == sample_role_data.description
        assert result.is_system is False
        mock_db.add.assert_called_once()
        mock_db.flush.assert_called_once()
        mock_db.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_role_without_actor(
        self, mock_db: AsyncMock, sample_role_data: RoleCreate
    ) -> None:
        """Test role creation without actor."""
        # Mock check for existing role (none found)
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act
        result = await RoleService.create_role(sample_role_data, mock_db)

        # Assert
        assert result.name == sample_role_data.name
        assert result.code == sample_role_data.code
        mock_db.add.assert_called_once()
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_role_duplicate_name(
        self, mock_db: AsyncMock, sample_role_data: RoleCreate, sample_role: Mock
    ) -> None:
        """Test role creation fails when role with same name exists."""
        # Mock existing role found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_role
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await RoleService.create_role(sample_role_data, mock_db)

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "already exists" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_create_role_duplicate_code(
        self, mock_db: AsyncMock, sample_role_data: RoleCreate, sample_role: Mock
    ) -> None:
        """Test role creation fails when role with same code exists."""
        # Mock existing role found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_role
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await RoleService.create_role(sample_role_data, mock_db)

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "already exists" in exc_info.value.detail

    # ===== GET ROLE TESTS =====
    @pytest.mark.asyncio
    async def test_get_role_by_id_success(
        self, mock_db: AsyncMock, sample_role: Mock
    ) -> None:
        """Test successful role retrieval by ID."""
        # Mock role found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_role
        mock_db.execute.return_value = mock_result

        # Act
        result = await RoleService.get_role_by_id(sample_role.id, mock_db)

        # Assert
        assert result == sample_role
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_role_by_id_not_found(self, mock_db: AsyncMock) -> None:
        """Test role retrieval by ID when role doesn't exist."""
        role_id = uuid4()

        # Mock no role found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await RoleService.get_role_by_id(role_id, mock_db)

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert "not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_get_role_by_code_success(
        self, mock_db: AsyncMock, sample_role: Mock
    ) -> None:
        """Test successful role retrieval by code."""
        # Mock role found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_role
        mock_db.execute.return_value = mock_result

        # Act
        result = await RoleService.get_role_by_code(sample_role.code, mock_db)

        # Assert
        assert result == sample_role
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_role_by_code_not_found(self, mock_db: AsyncMock) -> None:
        """Test role retrieval by code when role doesn't exist."""
        # Mock no role found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act
        result = await RoleService.get_role_by_code("nonexistent", mock_db)

        # Assert
        assert result is None
        mock_db.execute.assert_called_once()

    # ===== LIST ROLES TESTS =====
    @pytest.mark.asyncio
    async def test_list_roles_success(self, mock_db: AsyncMock) -> None:
        """Test successful role listing."""
        # Create sample roles
        role1 = Mock(spec=Role)
        role1.id = uuid4()
        role1.name = "Role 1"
        role1.code = "role1"
        role1.is_system = False

        role2 = Mock(spec=Role)
        role2.id = uuid4()
        role2.name = "Role 2"
        role2.code = "role2"
        role2.is_system = True

        roles = [role1, role2]

        # Mock roles found
        mock_result = Mock()
        mock_scalars = Mock()
        mock_scalars.all.return_value = roles
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        # Act
        result = await RoleService.list_roles(mock_db)

        # Assert
        assert len(result) == 2
        assert result == roles
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_roles_empty(self, mock_db: AsyncMock) -> None:
        """Test role listing when no roles exist."""
        # Mock empty result
        mock_result = Mock()
        mock_scalars = Mock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        # Act
        result = await RoleService.list_roles(mock_db)

        # Assert
        assert len(result) == 0
        assert result == []

    # ===== UPDATE ROLE TESTS =====
    @pytest.mark.asyncio
    async def test_update_role_success(
        self, mock_db: AsyncMock, sample_role: Mock
    ) -> None:
        """Test successful role update."""
        update_data = RoleUpdate(name="Updated Role", description="Updated description")

        # Mock role found and not system
        sample_role.is_system = False
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_role
        mock_db.execute.return_value = mock_result

        # Act
        result = await RoleService.update_role(sample_role.id, update_data, mock_db)

        # Assert
        assert result.name == "Updated Role"
        assert result.description == "Updated description"
        assert isinstance(result.updated_at, datetime)
        mock_db.flush.assert_called_once()
        mock_db.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_role_partial_update(
        self, mock_db: AsyncMock, sample_role: Mock
    ) -> None:
        """Test partial role update (only name)."""
        update_data = RoleUpdate(name="Updated Role")

        # Mock role found and not system
        sample_role.is_system = False
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_role
        mock_db.execute.return_value = mock_result

        # Act
        result = await RoleService.update_role(sample_role.id, update_data, mock_db)

        # Assert
        assert result.name == "Updated Role"
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_system_role_fails(
        self, mock_db: AsyncMock, system_role: Mock
    ) -> None:
        """Test updating system role fails."""
        update_data = RoleUpdate(name="Updated Role")

        # Mock system role found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = system_role
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await RoleService.update_role(system_role.id, update_data, mock_db)

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "system role" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_update_role_not_found(self, mock_db: AsyncMock) -> None:
        """Test updating non-existent role fails."""
        role_id = uuid4()
        update_data = RoleUpdate(name="Updated Role")

        # Mock role not found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await RoleService.update_role(role_id, update_data, mock_db)

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    # ===== DELETE ROLE TESTS =====
    @pytest.mark.asyncio
    async def test_delete_role_success(
        self, mock_db: AsyncMock, sample_role: Mock
    ) -> None:
        """Test successful role deletion."""
        # Mock role found and not system
        sample_role.is_system = False

        # Mock get_role_by_id call
        mock_get_role_result = Mock()
        mock_get_role_result.scalar_one_or_none.return_value = sample_role

        # Mock check for user assignments (none found)
        mock_user_check_result = Mock()
        mock_user_check_result.scalar_one_or_none.return_value = None

        # Setup execute to return different results for different calls
        mock_db.execute.side_effect = [mock_get_role_result, mock_user_check_result]

        # Act
        await RoleService.delete_role(sample_role.id, mock_db)

        # Assert
        mock_db.delete.assert_called_once_with(sample_role)
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_system_role_fails(
        self, mock_db: AsyncMock, system_role: Mock
    ) -> None:
        """Test deleting system role fails."""
        # Mock system role found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = system_role
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await RoleService.delete_role(system_role.id, mock_db)

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "system role" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_delete_role_with_users_fails(
        self, mock_db: AsyncMock, sample_role: Mock
    ) -> None:
        """Test deleting role assigned to users fails."""
        # Mock role found and not system
        sample_role.is_system = False

        # Mock get_role_by_id call
        mock_get_role_result = Mock()
        mock_get_role_result.scalar_one_or_none.return_value = sample_role

        # Mock check for user assignments (found)
        user_role = Mock(spec=UserRole)
        mock_user_check_result = Mock()
        mock_user_check_result.scalar_one_or_none.return_value = user_role

        # Setup execute to return different results for different calls
        mock_db.execute.side_effect = [mock_get_role_result, mock_user_check_result]

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await RoleService.delete_role(sample_role.id, mock_db)

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "assigned to users" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_delete_role_not_found(self, mock_db: AsyncMock) -> None:
        """Test deleting non-existent role fails."""
        role_id = uuid4()

        # Mock role not found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await RoleService.delete_role(role_id, mock_db)

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    # ===== ASSIGN ROLE TESTS =====
    @pytest.mark.asyncio
    async def test_assign_role_success(
        self, mock_db: AsyncMock, sample_role: Mock, sample_actor: EventActor
    ) -> None:
        """Test successful role assignment."""
        user_id = uuid4()

        # Mock user exists
        user = Mock(spec=User)
        user.id = user_id
        user.email = "test@example.com"
        mock_user_result = Mock()
        mock_user_result.scalar_one_or_none.return_value = user

        # Mock role exists
        mock_role_result = Mock()
        mock_role_result.scalar_one_or_none.return_value = sample_role

        # Mock assignment doesn't exist yet
        mock_assignment_result = Mock()
        mock_assignment_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = [
            mock_user_result,
            mock_role_result,
            mock_assignment_result,
        ]

        # Act
        await RoleService.assign_role(user_id, sample_role.id, mock_db, sample_actor)

        # Assert
        mock_db.add.assert_called_once()
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_assign_role_without_actor(
        self, mock_db: AsyncMock, sample_role: Mock
    ) -> None:
        """Test role assignment without actor."""
        user_id = uuid4()

        # Mock user exists
        user = Mock(spec=User)
        user.id = user_id
        mock_user_result = Mock()
        mock_user_result.scalar_one_or_none.return_value = user

        # Mock role exists
        mock_role_result = Mock()
        mock_role_result.scalar_one_or_none.return_value = sample_role

        # Mock assignment doesn't exist
        mock_assignment_result = Mock()
        mock_assignment_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = [
            mock_user_result,
            mock_role_result,
            mock_assignment_result,
        ]

        # Act
        await RoleService.assign_role(user_id, sample_role.id, mock_db)

        # Assert
        mock_db.add.assert_called_once()
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_assign_role_user_not_found(
        self, mock_db: AsyncMock, sample_role: Mock
    ) -> None:
        """Test role assignment when user not found."""
        user_id = uuid4()

        # Mock user not found (first call)
        mock_user_result = Mock()
        mock_user_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_user_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await RoleService.assign_role(user_id, sample_role.id, mock_db)

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert "User not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_assign_role_role_not_found(self, mock_db: AsyncMock) -> None:
        """Test role assignment when role not found."""
        user_id = uuid4()
        role_id = uuid4()

        # Mock user exists
        user = Mock(spec=User)
        user.id = user_id
        mock_user_result = Mock()
        mock_user_result.scalar_one_or_none.return_value = user

        # Mock role not found (second call)
        mock_role_result = Mock()
        mock_role_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = [mock_user_result, mock_role_result]

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await RoleService.assign_role(user_id, role_id, mock_db)

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert "Role not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_assign_role_already_assigned(
        self, mock_db: AsyncMock, sample_role: Mock
    ) -> None:
        """Test role assignment when already assigned."""
        user_id = uuid4()

        # Mock user exists
        user = Mock(spec=User)
        user.id = user_id
        mock_user_result = Mock()
        mock_user_result.scalar_one_or_none.return_value = user

        # Mock role exists
        mock_role_result = Mock()
        mock_role_result.scalar_one_or_none.return_value = sample_role

        # Mock assignment already exists
        user_role = Mock(spec=UserRole)
        user_role.user_id = user_id
        user_role.role_id = sample_role.id
        mock_assignment_result = Mock()
        mock_assignment_result.scalar_one_or_none.return_value = user_role

        mock_db.execute.side_effect = [
            mock_user_result,
            mock_role_result,
            mock_assignment_result,
        ]

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await RoleService.assign_role(user_id, sample_role.id, mock_db)

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "already assigned" in exc_info.value.detail

    # ===== REVOKE ROLE TESTS =====
    @pytest.mark.asyncio
    async def test_revoke_role_success(
        self, mock_db: AsyncMock, sample_role: Mock, sample_actor: EventActor
    ) -> None:
        """Test successful role revocation from user."""
        user_id = uuid4()

        # Mock user role assignment exists
        user_role = Mock(spec=UserRole)
        user_role.user_id = user_id
        user_role.role_id = sample_role.id
        user_role.role = sample_role  # Set the relationship

        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = user_role
        mock_db.execute.return_value = mock_result

        # Act
        await RoleService.revoke_role(user_id, sample_role.id, mock_db, sample_actor)

        # Assert
        mock_db.delete.assert_called_once_with(user_role)
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_revoke_role_without_actor(
        self, mock_db: AsyncMock, sample_role: Mock
    ) -> None:
        """Test role revocation without actor."""
        user_id = uuid4()

        # Mock user role assignment exists
        user_role = Mock(spec=UserRole)
        user_role.user_id = user_id
        user_role.role_id = sample_role.id
        user_role.role = sample_role

        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = user_role
        mock_db.execute.return_value = mock_result

        # Act
        await RoleService.revoke_role(user_id, sample_role.id, mock_db)

        # Assert
        mock_db.delete.assert_called_once_with(user_role)
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_revoke_role_not_found(self, mock_db: AsyncMock) -> None:
        """Test role revocation when assignment not found."""
        user_id = uuid4()
        role_id = uuid4()

        # Mock no assignment found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await RoleService.revoke_role(user_id, role_id, mock_db)

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert "assignment not found" in exc_info.value.detail

    # ===== UTILITY METHOD TESTS =====
    @pytest.mark.asyncio
    async def test_get_instance(self) -> None:
        """Test service instance creation."""
        instance = RoleService.get_instance()
        assert isinstance(instance, RoleService)


class TestPermissionServiceComprehensive:
    """Comprehensive test cases for PermissionService with 100% coverage."""

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        """Mock database session with proper async setup."""
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.add = Mock()
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()
        mock_session.delete = AsyncMock()
        return mock_session

    @pytest.fixture
    def sample_permission_data(self) -> PermissionCreate:
        """Sample permission data for testing."""
        return PermissionCreate(
            name="Test Permission",
            code="test_permission",
            description="Test permission description",
            resource="test_resource",
            action="test_action",
        )

    @pytest.fixture
    def sample_permission(self) -> Mock:
        """Sample permission instance."""
        permission = Mock(spec=Permission)
        permission.id = uuid4()
        permission.name = "Test Permission"
        permission.code = "test_permission"
        permission.description = "Test permission description"
        permission.resource = "test_resource"
        permission.action = "test_action"
        permission.is_system = False
        permission.created_at = datetime.now(UTC)
        return permission

    @pytest.fixture
    def sample_role(self) -> Mock:
        """Sample role instance."""
        role = Mock(spec=Role)
        role.id = uuid4()
        role.name = "Test Role"
        role.code = "test_role"
        return role

    @pytest.fixture
    def sample_actor(self) -> EventActor:
        """Sample event actor."""
        return EventActor(user_id=uuid4(), ip="127.0.0.1")

    # ===== CREATE PERMISSION TESTS =====
    @pytest.mark.asyncio
    async def test_create_permission_success(
        self, mock_db: AsyncMock, sample_permission_data: PermissionCreate
    ) -> None:
        """Test successful permission creation."""
        # Mock check for existing permission (none found)
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act
        result = await PermissionService.create_permission(
            sample_permission_data, mock_db
        )

        # Assert
        assert result.name == sample_permission_data.name
        assert result.code == sample_permission_data.code
        assert result.description == sample_permission_data.description
        assert result.resource == sample_permission_data.resource
        assert result.action == sample_permission_data.action
        assert result.is_system is False
        mock_db.add.assert_called_once()
        mock_db.flush.assert_called_once()
        mock_db.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_permission_minimal_data(self, mock_db: AsyncMock) -> None:
        """Test permission creation with minimal required data."""
        permission_data = PermissionCreate(
            name="Minimal Permission",
            code="minimal_permission",
            resource=None,
            action=None,
        )

        # Mock check for existing permission (none found)
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act
        result = await PermissionService.create_permission(permission_data, mock_db)

        # Assert
        assert result.name == permission_data.name
        assert result.code == permission_data.code
        assert result.resource is None
        assert result.action is None
        mock_db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_permission_duplicate_name(
        self,
        mock_db: AsyncMock,
        sample_permission_data: PermissionCreate,
        sample_permission: Mock,
    ) -> None:
        """Test permission creation fails when permission with same name exists."""
        # Mock existing permission found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_permission
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await PermissionService.create_permission(sample_permission_data, mock_db)

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "already exists" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_create_permission_duplicate_code(
        self,
        mock_db: AsyncMock,
        sample_permission_data: PermissionCreate,
        sample_permission: Mock,
    ) -> None:
        """Test permission creation fails when permission with same code exists."""
        # Mock existing permission found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_permission
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await PermissionService.create_permission(sample_permission_data, mock_db)

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "already exists" in exc_info.value.detail

    # ===== LIST PERMISSIONS TESTS =====
    @pytest.mark.asyncio
    async def test_list_permissions_success(self, mock_db: AsyncMock) -> None:
        """Test successful permission listing."""
        # Create sample permissions
        perm1 = Mock(spec=Permission)
        perm1.id = uuid4()
        perm1.name = "Permission 1"
        perm1.code = "perm1"

        perm2 = Mock(spec=Permission)
        perm2.id = uuid4()
        perm2.name = "Permission 2"
        perm2.code = "perm2"

        permissions = [perm1, perm2]

        # Mock permissions found
        mock_result = Mock()
        mock_scalars = Mock()
        mock_scalars.all.return_value = permissions
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        # Act
        result = await PermissionService.list_permissions(mock_db)

        # Assert
        assert len(result) == 2
        assert result == permissions
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_permissions_empty(self, mock_db: AsyncMock) -> None:
        """Test permission listing when no permissions exist."""
        # Mock empty result
        mock_result = Mock()
        mock_scalars = Mock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        # Act
        result = await PermissionService.list_permissions(mock_db)

        # Assert
        assert len(result) == 0
        assert result == []

    # ===== ASSIGN PERMISSION TO ROLE TESTS =====
    @pytest.mark.asyncio
    async def test_assign_permission_to_role_success(
        self,
        mock_db: AsyncMock,
        sample_role: Mock,
        sample_permission: Mock,
        sample_actor: EventActor,
    ) -> None:
        """Test successful permission assignment to role."""
        # Mock role exists
        mock_role_result = Mock()
        mock_role_result.scalar_one_or_none.return_value = sample_role

        # Mock permission exists
        mock_perm_result = Mock()
        mock_perm_result.scalar_one_or_none.return_value = sample_permission

        # Mock assignment doesn't exist yet
        mock_assignment_result = Mock()
        mock_assignment_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = [
            mock_role_result,
            mock_perm_result,
            mock_assignment_result,
        ]

        # Act
        await PermissionService.assign_permission_to_role(
            sample_role.id, sample_permission.id, mock_db, sample_actor
        )

        # Assert
        mock_db.add.assert_called_once()
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_assign_permission_to_role_role_not_found(
        self, mock_db: AsyncMock, sample_permission: Mock
    ) -> None:
        """Test permission assignment when role not found."""
        role_id = uuid4()

        # Mock role not found
        mock_role_result = Mock()
        mock_role_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_role_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await PermissionService.assign_permission_to_role(
                role_id, sample_permission.id, mock_db
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert "Role not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_assign_permission_to_role_permission_not_found(
        self, mock_db: AsyncMock, sample_role: Mock
    ) -> None:
        """Test permission assignment when permission not found."""
        permission_id = uuid4()

        # Mock role exists
        mock_role_result = Mock()
        mock_role_result.scalar_one_or_none.return_value = sample_role

        # Mock permission not found
        mock_perm_result = Mock()
        mock_perm_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = [mock_role_result, mock_perm_result]

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await PermissionService.assign_permission_to_role(
                sample_role.id, permission_id, mock_db
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert "Permission not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_assign_permission_to_role_already_assigned(
        self, mock_db: AsyncMock, sample_role: Mock, sample_permission: Mock
    ) -> None:
        """Test permission assignment when already assigned."""
        # Mock role exists
        mock_role_result = Mock()
        mock_role_result.scalar_one_or_none.return_value = sample_role

        # Mock permission exists
        mock_perm_result = Mock()
        mock_perm_result.scalar_one_or_none.return_value = sample_permission

        # Mock assignment already exists
        role_permission = Mock(spec=RolePermission)
        role_permission.role_id = sample_role.id
        role_permission.permission_id = sample_permission.id
        mock_assignment_result = Mock()
        mock_assignment_result.scalar_one_or_none.return_value = role_permission

        mock_db.execute.side_effect = [
            mock_role_result,
            mock_perm_result,
            mock_assignment_result,
        ]

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await PermissionService.assign_permission_to_role(
                sample_role.id, sample_permission.id, mock_db
            )

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "already assigned" in exc_info.value.detail

    # ===== REVOKE PERMISSION FROM ROLE TESTS =====
    @pytest.mark.asyncio
    async def test_revoke_permission_from_role_success(
        self,
        mock_db: AsyncMock,
        sample_role: Mock,
        sample_permission: Mock,
        sample_actor: EventActor,
    ) -> None:
        """Test successful permission revocation from role."""
        # Mock role permission assignment exists
        role_permission = Mock(spec=RolePermission)
        role_permission.role_id = sample_role.id
        role_permission.permission_id = sample_permission.id
        role_permission.role = sample_role
        role_permission.permission = sample_permission

        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = role_permission
        mock_result.scalars.return_value.all.return_value = []  # no holders
        mock_db.execute.return_value = mock_result

        # Act
        await PermissionService.revoke_permission_from_role(
            sample_role.id, sample_permission.id, mock_db, sample_actor
        )

        # Assert
        mock_db.delete.assert_called_once_with(role_permission)
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_revoke_permission_from_role_not_found(
        self, mock_db: AsyncMock
    ) -> None:
        """Test permission revocation when assignment not found."""
        role_id = uuid4()
        permission_id = uuid4()

        # Mock no assignment found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            await PermissionService.revoke_permission_from_role(
                role_id, permission_id, mock_db
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert "assignment not found" in exc_info.value.detail

    # ===== USER HAS ROLE TESTS =====
    @pytest.mark.asyncio
    async def test_user_has_role_true(self, mock_db: AsyncMock) -> None:
        """Test user_has_role when user has the role."""
        user_id = uuid4()
        role_code = "test_role"

        # Mock user role found
        user_role = Mock(spec=UserRole)
        user_role.user_id = user_id
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = user_role
        mock_db.execute.return_value = mock_result

        # Act
        result = await RoleService.user_has_role(user_id, role_code, mock_db)

        # Assert
        assert result is True
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_user_has_role_false(self, mock_db: AsyncMock) -> None:
        """Test user_has_role when user doesn't have the role."""
        user_id = uuid4()
        role_code = "test_role"

        # Mock user role not found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Act
        result = await RoleService.user_has_role(user_id, role_code, mock_db)

        # Assert
        assert result is False
        mock_db.execute.assert_called_once()
