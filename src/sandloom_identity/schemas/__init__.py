"""Identity schemas."""

from .auth import (
    EmailVerificationIn,
    LoginIn,
    PasswordResetIn,
    PasswordResetRequestIn,
    RefreshTokenIn,
    RegisterIn,
    TokenPair,
)
from .rbac import (
    PermissionBase,
    PermissionCreate,
    PermissionOut,
    PermissionUpdate,
    RoleBase,
    RoleCreate,
    RoleOut,
    RolePermissionAssign,
    RoleUpdate,
    RoleWithPermissions,
    UserRoleAssign,
)
from .sso import (
    ExternalIdentityOut,
    SSOCallbackIn,
    SSOInitOut,
    SSOProviderOut,
)
from .twofa import (
    TwoFABackupCodeIn,
    TwoFAEnableIn,
    TwoFAInitResponse,
    TwoFAStatusOut,
    TwoFAVerifyIn,
)
from .user import (
    UserBase,
    UserCreate,
    UserListOut,
    UserOut,
    UserUpdate,
    UserWithRoles,
)

__all__ = [
    # Auth
    "RegisterIn",
    "LoginIn",
    "TokenPair",
    "RefreshTokenIn",
    "PasswordResetRequestIn",
    "PasswordResetIn",
    "EmailVerificationIn",
    # User
    "UserBase",
    "UserCreate",
    "UserUpdate",
    "UserOut",
    "UserWithRoles",
    "UserListOut",
    # RBAC
    "RoleBase",
    "RoleCreate",
    "RoleUpdate",
    "RoleOut",
    "PermissionBase",
    "PermissionCreate",
    "PermissionUpdate",
    "PermissionOut",
    "RoleWithPermissions",
    "UserRoleAssign",
    "RolePermissionAssign",
    # 2FA
    "TwoFAInitResponse",
    "TwoFAEnableIn",
    "TwoFAVerifyIn",
    "TwoFABackupCodeIn",
    "TwoFAStatusOut",
    # SSO
    "SSOProviderOut",
    "SSOInitOut",
    "SSOCallbackIn",
    "ExternalIdentityOut",
]
