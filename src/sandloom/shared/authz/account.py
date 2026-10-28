"""Authorization subjects: which billing account an atom checks (spec §7).

Three separate notions (§7.1): the tenant IDENTITY (the bound tenant UUID,
which scopes data), the tenant KIND (:func:`tenant_kind`, from authoritative
sources only) and the authorization SUBJECT — the billing account an atom
checks, stated in code with ``of=`` (required, no default, §7.2):

* ``Account.user()`` — the caller's personal account (today's
  ``tenant_type="user"``; ``Subscribed(of=Account.user())`` stays claims-only);
* ``Account.org(param="org_id")`` — the organization from the path or query
  parameter ``param``, else ``X-Tenant-Id``, else the bound tenant, validated
  against the signed allowlist / membership; ``param=None`` reads only the
  header or the bound tenant. No organization is indeterminate, never the
  caller's own id;
* ``Account.bound_tenant()`` — by the bound tenant's kind: personal → the
  user account, organization → that organization's account, canonical or
  unknown → indeterminate.

The binding of a route (``TenantScope``) is about isolation only; it never
chooses a billing account by itself.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sandloom.core.di import settings
from sandloom.shared.authz._resolve import (
    ContractUnavailable,
    _bound_tenant,
    resolve_contract,
    resolve_org_id,
)
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.live_contracts import MEMBERSHIP, get_live_adapter
from sandloom.shared.authz.results import AccessDecision, Category, PolicyIndeterminate

TenantKind = Literal["personal", "organization", "canonical", "unknown"]
AccountKind = Literal["user", "org", "bound_tenant"]

#: ``request.state`` key of the per-request memo of :func:`tenant_kind`.
_KIND_MEMO = "_authz_tenant_kinds"
#: The authenticated request of this context (set by ``current_context``),
#: for :func:`tenant_kind` called without an ``AuthzContext``.
_current_authz: ContextVar[tuple[Any, Any] | None] = ContextVar(
    "sandloom_authz_current", default=None
)


class AuthzSubjectMissingError(TypeError):
    """A billing atom without ``of=`` (SANDLOOM-B017)."""

    sandloom_code = "SANDLOOM-B017"


@dataclass(frozen=True, slots=True)
class Account:
    """The billing account an atom checks (``of=``); build it with
    :meth:`user`, :meth:`org` or :meth:`bound_tenant`."""

    kind: AccountKind
    param: str | None = None
    reason: str | None = None

    @staticmethod
    def user(*, reason: str | None = None) -> Account:
        """The caller's personal account. ``reason=`` waives SANDLOOM-C027 on a
        route that carries an organization parameter."""
        return Account("user", None, reason)

    @staticmethod
    def org(*, param: str | None = "org_id") -> Account:
        """An organization account: ``param`` (path, then query), else the
        ``X-Tenant-Id`` header, else the bound tenant (``param=None``: header
        or bound tenant only)."""
        if param is not None and (not isinstance(param, str) or not param):
            raise ValueError("Account.org(param=) is a parameter name or None")
        return Account("org", param, None)

    @staticmethod
    def bound_tenant() -> Account:
        """The account of the bound tenant, by its kind (personal or
        organization; canonical / unknown are indeterminate)."""
        return Account("bound_tenant", None, None)

    def describe(self) -> str:
        if self.kind == "org":
            return f"Account.org(param={self.param!r})"
        return f"Account.{self.kind}()"


_MISSING: Any = object()
#: ``org_params`` of an account read from the bound tenant (header or
#: binding): never a path parameter, so a gate solved before ``TenantScope()``
#: is reported (SANDLOOM-B007).
BOUND_TENANT_PARAM = "<bound tenant>"


def account_org_params(account: Account) -> frozenset[str]:
    """The parameters (or the bound tenant) an account may read an
    organization from (the gate-order check, SANDLOOM-B007)."""
    if account.kind == "user":
        return frozenset()
    if account.kind == "org" and account.param is not None:
        return frozenset({account.param})
    return frozenset({BOUND_TENANT_PARAM})


def require_account(of: Any, atom: str) -> Account:
    """``of`` as an :class:`Account`, or :class:`AuthzSubjectMissingError`."""
    if isinstance(of, Account):
        return of
    hint = (
        f"{atom}() needs of=: Account.user() (the caller's personal account), "
        "Account.org(param=...) (a route under /orgs/{...} → "
        'Account.org(param="org_id")) or Account.bound_tenant() (SANDLOOM-B017)'
    )
    if of is _MISSING:
        raise AuthzSubjectMissingError(hint)
    raise AuthzSubjectMissingError(f"{hint}; got {of!r}")


def _indeterminate(code: str, reason: str, status: int, requirement: str) -> Exception:
    return PolicyIndeterminate(
        AccessDecision.error(
            code=code,
            category=Category.FORBIDDEN,
            http_status=status,
            reason=reason,
            failed_requirement=requirement,
        )
    )


def bind_authz_request(request: Any, subject: Any) -> None:
    """Record the authenticated request of this context (``current_context``)."""
    _current_authz.set((request, subject))


def _memo(request: Any) -> dict[UUID, TenantKind] | None:
    state = getattr(request, "scope", {}).get("state") if request is not None else None
    if not isinstance(state, dict):
        return None
    memo = state.get(_KIND_MEMO)
    if memo is None:
        memo = state[_KIND_MEMO] = {}
    return memo


async def _is_member(ctx: AuthzContext, org_id: UUID) -> bool:
    adapter = get_live_adapter(MEMBERSHIP)
    try:
        service = resolve_contract(ctx, adapter.service)
    except ContractUnavailable:
        return False  # no membership authority: the kind stays unknown
    return bool(
        await adapter.has_role_at_least(
            service,
            org_id=org_id,
            user_id=ctx.subject.user_id,
            minimum="member",
            ctx=ctx,
        )
    )


async def tenant_kind(
    tenant_id: UUID, *, ctx: AuthzContext | None = None
) -> TenantKind:
    """The kind of ``tenant_id`` from authoritative sources only (spec §7.1),
    memoised per request: ``canonical`` under ``TENANCY_MODE=single``,
    ``personal`` when it is the caller's user id, ``organization`` when the
    membership contract confirms the caller's membership, else ``unknown``.
    Without ``ctx`` it uses the request ``current_context`` authenticated."""
    if ctx is None:
        current = _current_authz.get()
        if current is None:
            raise RuntimeError(
                "tenant_kind() outside an authenticated request: pass ctx="
            )
        request, subject = current
        ctx = AuthzContext(subject, request=request)
    memo = _memo(ctx.request)
    if memo is not None and tenant_id in memo:
        return memo[tenant_id]
    kind: TenantKind
    if getattr(settings, "tenancy_mode", "multi") == "single":
        kind = "canonical"
    elif tenant_id == ctx.subject.user_id:
        kind = "personal"
    else:
        try:
            kind = "organization" if await _is_member(ctx, tenant_id) else "unknown"
        except PolicyIndeterminate:
            kind = "unknown"
    if memo is not None:
        memo[tenant_id] = kind
    return kind


async def resolve_account(
    ctx: AuthzContext, account: Account, *, requirement: str
) -> tuple[str, UUID]:
    """``(tenant_type, tenant_id)`` the atom checks: ``("user", user_id)`` or
    ``("organization", org_id)``; raises :class:`PolicyIndeterminate` when no
    account can be resolved (never falls back to the caller's own id for an
    organization)."""
    if account.kind == "user":
        return "user", ctx.subject.user_id
    if account.kind == "org":
        org = resolve_org_id(
            ctx,
            org_param=account.param,
            requirement=requirement,
            validate_against_token=True,
        )
        if org == ctx.subject.user_id:
            # the caller's personal tenant is not an organization: never ask
            # about the user's own id as an organization account (G-AUTHZ-07)
            raise _indeterminate(
                "not_an_organization",
                "the resolved tenant is the caller's personal tenant, not an "
                "organization",
                403,
                requirement,
            )
        return "organization", org
    bound = _bound_tenant()
    if bound is None:
        raise _indeterminate(
            "tenant_context_missing",
            "Account.bound_tenant() needs a bound tenant",
            400,
            requirement,
        )
    kind = await tenant_kind(bound, ctx=ctx)
    if kind == "personal":
        return "user", ctx.subject.user_id
    if kind == "organization":
        return "organization", bound
    raise _indeterminate(
        "tenant_kind_unresolved",
        f"the bound tenant is {kind}: no billing account to check "
        "(state of=Account.user() or Account.org(...))",
        403,
        requirement,
    )


__all__ = [
    "BOUND_TENANT_PARAM",
    "Account",
    "account_org_params",
    "AuthzSubjectMissingError",
    "TenantKind",
    "bind_authz_request",
    "require_account",
    "resolve_account",
    "tenant_kind",
]
