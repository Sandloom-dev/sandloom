"""Paradigm-agnostic tenant context shared by HTTP and UoW runtimes."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_current_tenant: ContextVar[uuid.UUID | None] = ContextVar(
    "sandloom_current_tenant", default=None
)
_bypass: ContextVar[bool] = ContextVar("sandloom_tenant_bypass", default=False)


class MissingTenantError(RuntimeError):
    """A tenant-scoped operation ran with no tenant bound in the context."""

    sandloom_code = "SANDLOOM-R001"


class TenantMismatchError(RuntimeError):
    """A tenant-owned object does not belong to the bound tenant."""


class UnsafeTenantOperationError(RuntimeError):
    """An ORM operation could bypass or mutate tenant ownership."""


def current_tenant_id() -> uuid.UUID:
    """Return the tenant bound in the current context, or raise (fail closed)."""
    tid = _current_tenant.get()
    if tid is None:
        raise MissingTenantError(
            "no tenant bound in the current context; use the TenantScope "
            "dependency, bind_tenant(...), or unscoped() for system queries"
        )
    return tid


def current_tenant_id_optional() -> uuid.UUID | None:
    """Return the bound tenant or ``None`` (does not raise)."""
    return _current_tenant.get()


@contextmanager
def bind_tenant(tenant_id: uuid.UUID) -> Iterator[uuid.UUID]:
    """Bind ``tenant_id`` for the duration of the block."""
    token = _current_tenant.set(tenant_id)
    try:
        yield tenant_id
    finally:
        _current_tenant.reset(token)


@contextmanager
def configured_tenant_scope(settings: Any) -> Iterator[uuid.UUID | None]:
    """Bind the canonical tenant for a configured single-tenant deployment."""
    current = _current_tenant.get()
    if getattr(settings, "tenancy_mode", "multi") != "single":
        yield current
        return

    configured = getattr(settings, "single_tenant_id", None)
    if configured is None:
        raise RuntimeError("TENANCY_MODE=single requires SINGLE_TENANT_ID")
    canonical = uuid.UUID(str(configured))
    if current is not None:
        if current != canonical:
            raise TenantMismatchError(
                "the bound tenant does not match SINGLE_TENANT_ID"
            )
        yield current
        return

    with bind_tenant(canonical):
        yield canonical


@contextmanager
def unscoped() -> Iterator[None]:
    """Disable tenant enforcement for deliberate admin/system work."""
    token = _bypass.set(True)
    try:
        yield
    finally:
        _bypass.reset(token)


__all__ = [
    "MissingTenantError",
    "TenantMismatchError",
    "UnsafeTenantOperationError",
    "bind_tenant",
    "configured_tenant_scope",
    "current_tenant_id",
    "current_tenant_id_optional",
    "unscoped",
]
