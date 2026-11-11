"""Identity's token-claims contributor.

Resolves the user's global roles, the permissions those roles grant and the
issuer's own fields (``email``, ``email_verified``), and returns them as JWT
claims. Runs on the mint cold path (login / refresh) only — the per-request
authorization path then reads these claims with zero I/O.

:func:`identity_claims_columns` is the SINGLE source of truth for the roles
and permissions: two scalar subqueries, each an ``aggregate_list`` of codes
correlated to a user id. The contributor selects them with two more scalar
subqueries (the user's ``email`` and ``is_verified``) in ONE statement, run in
the issuer's session when identity mints the token (its pending rows, e.g. a
registration or an SSO link, are flushed first and seen). The kernel's mint
check takes every one of these keys from identity's contributor only
(ADR-0014 decision 5): no caller hands the mint a claim value. Nothing here
is written for one database: the aggregate is a portable form, and a scalar
subquery needs no ``LATERAL``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Label, Select, bindparam, select
from sqlalchemy.orm import QueryableAttribute

from sandloom.sdk import db
from sandloom.sdk.data import aggregate_list
from sandloom_identity.models import Permission, Role, RolePermission, User, UserRole

_UserId = ColumnElement[Any] | QueryableAttribute[Any] | UUID


def identity_claims_columns(
    user_id: _UserId,
) -> tuple[Label[list[Any]], Label[list[Any]]]:
    """Roles and permissions of ``user_id``: ``role_codes`` and
    ``permission_codes``, each a scalar subquery answering a list (``[]``
    without any). Correlated to the outer statement when ``user_id`` is a
    column of it (``User.id``), so the claims ride a statement that loads
    the user; each subquery is one aggregate, so it always answers one value
    and never multiplies or drops the outer row.
    """
    role_codes = (
        select(aggregate_list(Role.code))
        .select_from(UserRole)
        .join(Role, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
        .scalar_subquery()
    )
    permission_codes = (
        select(aggregate_list(Permission.code))
        .select_from(UserRole)
        .join(RolePermission, UserRole.role_id == RolePermission.role_id)
        .join(Permission, RolePermission.permission_id == Permission.id)
        .where(UserRole.user_id == user_id)
        .scalar_subquery()
    )
    return role_codes.label("role_codes"), permission_codes.label("permission_codes")


def claims_from_codes(
    role_codes: Iterable[str | None] | None,
    permission_codes: Iterable[str | None] | None,
) -> dict[str, Any]:
    """``{"roles": [...], "permissions": [...]}`` from the aggregated codes."""
    return {
        "roles": sorted({c for c in role_codes or () if c is not None}),
        "permissions": sorted({c for c in permission_codes or () if c is not None}),
    }


def _user_column(column: QueryableAttribute[Any], label: str) -> Label[Any]:
    """``column`` of the user ``b_user_id`` as a scalar subquery: ``NULL``
    for a missing user, so the statement still answers one row."""
    return (
        select(column)
        .where(User.id == bindparam("b_user_id"))
        .scalar_subquery()
        .label(label)
    )


#: The contributor's own statement, built once (the user id is bound per call).
_CONTRIBUTOR_QUERY: Select[tuple[list[Any], list[Any], Any, Any]] = select(
    *identity_claims_columns(bindparam("b_user_id")),
    _user_column(User.email, "email"),
    _user_column(User.is_verified, "is_verified"),
)


async def contribute_identity_claims(user_id: UUID) -> dict[str, Any]:
    """Return ``{"roles": [...], "permissions": [...], "email": ...,
    "email_verified": ...}`` for ``user_id``.

    ONE query in identity's contract session (the issuer's own request
    transaction when identity mints the token): the user's role codes and the
    codes of the permissions they grant, each aggregated
    (:func:`identity_claims_columns`), and the user's email and whether it is
    verified. ``email_verified`` is ``True`` only for a verified user: other
    modules (e.g. tenancy's invitation accept) trust ``email`` only when the
    user proved ownership of the address.
    """
    session = db()
    row = (await session.execute(_CONTRIBUTOR_QUERY, {"b_user_id": user_id})).one()
    claims = claims_from_codes(row.role_codes, row.permission_codes)
    claims["email"] = row.email
    claims["email_verified"] = row.is_verified is True
    return claims
