"""Internal resolution helpers shared by the live atoms and access facts.

- :func:`resolve_contract` — resolve a sync service contract through
  ``core.get_service`` (local provider first, then the remote RPC proxy from
  ``SERVICE_BINDINGS``), mapping "not available in this deployment" to a typed
  :class:`ContractUnavailable` instead of probing the local registry only.
- :func:`resolve_org_id` — the organization a tenant-scoped atom acts on,
  taken from the request/tenant context (never the user's own id).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sandloom.shared.authz.results import AccessDecision, Category, PolicyIndeterminate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.shared.authz.context import AuthzContext

TENANT_HEADER = "X-Tenant-Id"


class ContractUnavailable(LookupError):
    """No local provider and no remote binding for a contract (or no core)."""


def resolve_contract(ctx: AuthzContext, name: str) -> Any:
    """Return the provider of contract ``name`` or raise :class:`ContractUnavailable`.

    Delegates to ``core.get_service`` so a split deployment's generated RPC
    proxy (``SERVICE_BINDINGS``) is honored. Only the facade's typed
    "unavailable in this deployment" signal (``ServiceUnavailableInDeployment``
    / its base ``ServiceNotFound``, re-exported by ``sandloom.core.di``) is mapped;
    any other error propagates.
    """
    from sandloom.core.di import ServiceNotFound  # noqa: PLC0415 - lazy (no cycle)

    core = ctx.core()
    if core is None or not callable(getattr(core, "get_service", None)):
        raise ContractUnavailable(f"no core bound to resolve service {name!r}")
    try:
        return core.get_service(name)
    except ServiceNotFound as exc:  # incl. ServiceUnavailableInDeployment
        raise ContractUnavailable(
            f"service {name!r} is not available in this deployment"
        ) from exc


def _parse_uuid(raw: Any) -> UUID | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None


def subject_tenant_allowlist(ctx: AuthzContext) -> set[UUID]:
    """Tenants the signed token grants (``tenant_id``/``org_id``/``tenant_ids``)."""
    allowed: set[UUID] = set()
    for key in ("tenant_id", "org_id"):
        parsed = _parse_uuid(ctx.subject.claim(key))
        if parsed is not None:
            allowed.add(parsed)
    values = ctx.subject.claim("tenant_ids")
    if isinstance(values, (list, tuple, set, frozenset)):
        for value in values:
            parsed = _parse_uuid(value)
            if parsed is not None:
                allowed.add(parsed)
    return allowed


def _bound_tenant() -> UUID | None:
    from sandloom.core.di import current_tenant_id_optional  # noqa: PLC0415 - lazy

    bound = current_tenant_id_optional()
    return _parse_uuid(bound)


def _indeterminate(
    code: str, reason: str, status: int, requirement: str
) -> PolicyIndeterminate:
    return PolicyIndeterminate(
        AccessDecision.error(
            code=code,
            category=Category.FORBIDDEN,
            http_status=status,
            reason=reason,
            failed_requirement=requirement,
        )
    )


def _raw_param(request: Any, org_param: str | None) -> Any:
    """The raw ``org_param`` of the request: the path parameter, else the
    query parameter of that name (``None``: not given, or no parameter)."""
    if org_param is None:
        return None
    raw = (getattr(request, "path_params", None) or {}).get(org_param)
    if raw is None:
        raw = (getattr(request, "query_params", None) or {}).get(org_param)
    return raw


def resolve_org_id(
    ctx: AuthzContext,
    *,
    org_param: str | None,
    requirement: str,
    validate_against_token: bool,
) -> UUID:
    """Organization id for a tenant-scoped atom; raises :class:`PolicyIndeterminate`.

    Order: the request path (then query) parameter ``org_param`` → the
    ``X-Tenant-Id`` header → the bound tenant (``TenantScope`` / ``bind_tenant``
    / single-tenant mode); ``org_param=None`` skips the parameter. An explicit
    parameter/header value must agree with a bound tenant, and —
    when ``validate_against_token`` — must be granted by the signed token's
    tenant allowlist (the bound tenant was already validated when it was bound).
    A missing or unauthorized organization is *indeterminate* (the question
    cannot be asked), so it denies even under ``Not``.
    """
    request = ctx.request
    explicit: UUID | None = None
    raw_path = None
    if request is not None:
        raw_path = _raw_param(request, org_param)
        explicit = _parse_uuid(raw_path)
        if explicit is None and raw_path is None:
            headers = getattr(request, "headers", None) or {}
            raw_header = headers.get(TENANT_HEADER)
            explicit = _parse_uuid(raw_header)
            if raw_header and explicit is None:
                raise _indeterminate(
                    "invalid_tenant", "invalid tenant id", 400, requirement
                )
        elif explicit is None:
            raise _indeterminate(
                "invalid_tenant", f"invalid '{org_param}'", 400, requirement
            )
    bound = _bound_tenant()
    if explicit is None:
        if bound is None:
            raise _indeterminate(
                "tenant_context_missing",
                "organization context required",
                400,
                requirement,
            )
        return bound
    if bound is not None and bound != explicit:
        raise _indeterminate(
            "tenant_mismatch",
            "organization does not match the bound tenant",
            403,
            requirement,
        )
    if (
        validate_against_token
        and bound is None
        and explicit not in subject_tenant_allowlist(ctx)
    ):
        raise _indeterminate(
            "tenant_not_authorized",
            "organization is not authorized by the access token",
            403,
            requirement,
        )
    return explicit
