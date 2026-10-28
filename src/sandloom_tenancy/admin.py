"""Platform directory; no implicit tenant membership or self-service controls."""

from typing import Any

RESOURCES: list[dict[str, Any]] = [
    {
        "name": "organizations",
        "audit": {"resource_type": "organization", "id_field": "id"},
        "label": "All organizations",
        "group": "Customers",
        "icon": "building",
        # The operator's OWN membership in the organization (no platform authority).
        "show_widgets": ["tenancy.directory", "tenancy.workspace"],
        "access": {"roles": ["admin"]},
        "list": {"path": "/api/v1/platform/tenancy/organizations", "method": "GET"},
        "pagination": {"mode": "offset"},
        "columns": ["name", "slug", "owner_id", "is_active", "created_at"],
        "detail": {
            "path": "/api/v1/platform/tenancy/organizations/{record_id}",
            "method": "GET",
        },
        "show": [
            "id",
            "name",
            "slug",
            "owner_id",
            "is_active",
            "created_at",
            "updated_at",
        ],
        "filters": ["q", "is_active", "owner_id"],
        "labels": {
            "name": "Name",
            "slug": "Slug",
            "owner_id": "Owner ID",
            "is_active": "Is active",
            "created_at": "Created at",
            "id": "Id",
            "updated_at": "Updated at",
            "q": "Search name or slug",
        },
    },
    {
        "name": "organization_members",
        "label": "Organization members",
        "group": "Customers",
        "icon": "users-group",
        "access": {"roles": ["admin"]},
        "list": {"path": "/api/v1/platform/tenancy/members", "method": "GET"},
        "pagination": {"mode": "offset"},
        "columns": ["organization_id", "user_id", "role", "created_at"],
        "filters": ["organization_id", "user_id"],
        "labels": {
            "organization_id": "Organization ID",
            "user_id": "User ID",
            "role": "Role",
            "created_at": "Created at",
            "q": "Search name or slug",
        },
    },
]
