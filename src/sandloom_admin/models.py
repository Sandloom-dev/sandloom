"""Versioned, deliberately bounded admin declaration language.

Business modules export plain dictionaries in ``admin.RESOURCES``; they need
no imports from the kernel. OpenAPI supplies types, never permissions.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Access(Declaration):
    authenticated: bool = False
    roles: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def explicit(self) -> Access:
        if self.authenticated == bool(self.roles):
            raise ValueError("choose authenticated=true OR a non-empty roles allowlist")
        if any(not r.strip() for r in self.roles):
            raise ValueError("roles must not be empty")
        return self


class Operation(Declaration):
    path: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class FormOperation(Operation):
    fields: list[str]


class Pagination(Declaration):
    mode: Literal["all", "offset", "cursor"]
    limit: str = "limit"
    offset: str = "skip"
    cursor: str = "cursor"
    next_header: str = "X-Next-Cursor"
    # Offset pagination requires an API that accepts limit+1 for lookahead.
    max_page_size: int = Field(default=100, ge=1, le=100)


class Action(FormOperation):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    label: str
    confirm: str = Field(min_length=1)
    access: Access | None = None
    idempotency_field: str | None = None
    # Presentation only; the API stays the authority. ``danger`` styles the
    # command as destructive, ``typed_confirmation`` names a displayed record
    # field the operator must retype, ``placement="widget"`` leaves rendering
    # to a declared widget that knows the live state (e.g. blocked or not).
    tone: Literal["default", "danger"] = "default"
    typed_confirmation: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    placement: Literal["toolbar", "widget"] = "toolbar"


class Scope(Declaration):
    # None means a header-only TenantScope selector, without a path parameter.
    parameter: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = "Organization / tenant ID"


class AuditBinding(Declaration):
    resource_type: str = Field(
        min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_.-]*$"
    )
    id_field: str = Field(default="id", pattern=r"^[a-z][a-z0-9_]*$")


class Resource(Declaration):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    label: str = Field(min_length=1)
    access: Access
    # Navigation section and icon name (from the UI's fixed icon set).
    group: str | None = Field(default=None, min_length=1, max_length=40)
    icon: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]*$")
    listing: Operation = Field(alias="list")
    pagination: Pagination
    columns: list[str] = Field(min_length=1)
    detail: Operation | None = None
    show: list[str] = Field(default_factory=list)
    create: FormOperation | None = None
    edit: FormOperation | None = None
    delete: Operation | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    # Optional top-level array property in a public response envelope.
    items_field: str | None = None
    scope: Scope | None = None
    filters: list[str] = Field(default_factory=list)
    sort_parameter: str | None = None
    sort_fields: list[str] = Field(default_factory=list)
    show_widgets: list[str] = Field(default_factory=list)
    edit_widgets: list[str] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    audit: AuditBinding | None = None


Json = dict[str, Any]
