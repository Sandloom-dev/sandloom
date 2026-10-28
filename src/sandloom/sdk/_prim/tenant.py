"""New L2 tenant primitives of ``sandloom.sdk.tenant`` (spec §7.1).

The tenant kind comes from authoritative sources only: ``canonical`` from
``TENANCY_MODE=single``, ``personal`` when the tenant is the caller's user
id, ``organization`` when the membership contract confirms it, else
``unknown`` (memoised per request; H1, WS-A).
"""

from __future__ import annotations

from sandloom.core.di import (
    bind_tenant,
    current_tenant_id,
    current_tenant_id_optional,
    unscoped,
)
from sandloom.shared.authz.account import TenantKind, tenant_kind

__all__ = [
    "TenantKind",
    "bind_tenant",
    "current_tenant_id",
    "current_tenant_id_optional",
    "tenant_kind",
    "unscoped",
]
