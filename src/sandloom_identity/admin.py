"""Admin UI admission: domain API allowlists, not database CRUD."""

from typing import Any

RESOURCES: list[dict[str, Any]] = [
    {
        "name": "users",
        "audit": {"resource_type": "user", "id_field": "id"},
        "label": "Users",
        "access": {"roles": ["admin"]},
        "group": "Customers",
        "icon": "users",
        "list": {"path": "/api/v1/users", "method": "GET"},
        "pagination": {"mode": "offset"},
        "columns": [
            "full_name",
            "email",
            "is_active",
            "is_verified",
            "created_at",
            "id",
        ],
        "filters": ["q", "is_active"],
        "detail": {"path": "/api/v1/users/{user_id}", "method": "GET"},
        "show": [
            "full_name",
            "email",
            "username",
            "id",
            "is_active",
            "is_verified",
            "created_at",
            "last_login_at",
        ],
        "actions": [
            {
                "name": "revoke-sessions",
                "label": "Revoke sessions",
                "path": "/api/v1/users/{user_id}/revoke-sessions",
                "method": "POST",
                "fields": ["reason"],
                "confirm": (
                    "Every refresh token is revoked and existing access tokens are "
                    "rejected. The user must sign in again."
                ),
                "placement": "widget",
            },
            {
                "name": "block",
                "label": "Block user",
                "path": "/api/v1/users/{user_id}/block",
                "method": "POST",
                "fields": ["reason"],
                "confirm": (
                    "Emergency stop: every request with an existing token is "
                    "rejected, sign-in is refused and all sessions end. Recorded "
                    "in the audit log with your reason."
                ),
                "tone": "danger",
                "typed_confirmation": "email",
                "placement": "widget",
            },
            {
                "name": "unblock",
                "label": "Unblock user",
                "path": "/api/v1/users/{user_id}/unblock",
                "method": "POST",
                "fields": ["reason"],
                "confirm": (
                    "Requests and sign-in are accepted again. Sessions ended by "
                    "the block stay ended."
                ),
                "placement": "widget",
            },
        ],
        "edit": {
            "path": "/api/v1/users/{user_id}",
            "method": "PUT",
            "fields": ["full_name", "first_name", "last_name", "username", "is_active"],
        },
        "delete": {"path": "/api/v1/users/{user_id}", "method": "DELETE"},
        "show_widgets": [
            "identity.account-safety",
            "identity.user-roles",
            "identity.directory",
        ],
        "labels": {
            "q": "Search",
            "reason": "Reason",
            "full_name": "Full name",
            "email": "Email",
            "is_active": "Active",
            "is_verified": "Email verified",
            "created_at": "Created",
            "last_login_at": "Last sign-in",
            "first_name": "First name",
            "last_name": "Last name",
            "username": "Username",
            "id": "ID",
        },
    },
    {
        "name": "roles",
        "audit": {"resource_type": "role", "id_field": "id"},
        "label": "Platform roles",
        "access": {"roles": ["admin"]},
        "group": "Access control",
        "icon": "shield",
        "list": {"path": "/api/v1/roles", "method": "GET"},
        "pagination": {"mode": "all"},
        "columns": ["name", "code", "description", "is_system"],
        "detail": {"path": "/api/v1/roles/{role_id}", "method": "GET"},
        "show": ["id", "name", "code", "description", "is_system"],
        "create": {
            "path": "/api/v1/roles",
            "method": "POST",
            "fields": ["name", "code", "description"],
        },
        "edit": {
            "path": "/api/v1/roles/{role_id}",
            "method": "PUT",
            "fields": ["name", "description"],
        },
        "delete": {"path": "/api/v1/roles/{role_id}", "method": "DELETE"},
        "show_widgets": ["identity.role-permissions"],
        "edit_widgets": ["identity.role-permissions"],
        "labels": {
            "name": "Name",
            "code": "Code",
            "description": "Description",
            "is_system": "System role",
            "id": "ID",
        },
    },
    {
        "name": "permissions",
        "label": "Permissions",
        "access": {"roles": ["admin"]},
        "group": "Access control",
        "icon": "key",
        "list": {"path": "/api/v1/permissions", "method": "GET"},
        "pagination": {"mode": "all"},
        "columns": ["name", "code", "resource", "action"],
        "labels": {
            "name": "Name",
            "code": "Code",
            "resource": "Resource",
            "action": "Action",
        },
    },
]
