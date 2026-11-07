"""Identity's token-claims contributor.

Resolves the user's global roles and the permissions those roles grant, and
returns them as JWT claims. Runs on the mint cold path (login / refresh) only —
the per-request authorization path then reads these claims with zero I/O.

:func:`identity_claims_columns` is the SINGLE source of truth for these claims:
two scalar subqueries, each an ``aggregate_list`` of codes correlated to a user
id. The contributor selects them on their own; the refresh rotation adds them
to the statement that loads the presented token and its user, and hands the
result to claims collection as precomputed for :func:`contribute_identity_claims`
(one round trip less per refresh). Both paths build the claims with
:func:`claims_from_codes`. Nothing here is written for one database: the
aggregate is a portable form, and a scalar subquery needs no ``LATERAL``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Label, Select, bindparam, select
from sqlalchemy.orm import QueryableAttribute

from sandloom.sdk import db
from sandloom.sdk.data import aggregate_list
from sandloom_identity.models import Permission, Role, RolePermission, UserRole

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


#: The contributor's own statement, built once (the user id is bound per call).
_CONTRIBUTOR_QUERY: Select[tuple[list[Any], list[Any]]] = select(
    *identity_claims_columns(bindparam("b_user_id"))
)


async def contribute_identity_claims(user_id: UUID) -> dict[str, Any]:
    """Return ``{"roles": [...], "permissions": [...]}`` for ``user_id``.

    ONE query in identity's contract session (the issuer's own request
    transaction when identity mints the token): the user's role codes and the
    codes of the permissions they grant, each aggregated
    (:func:`identity_claims_columns`).
    """
    session = db()
    row = (await session.execute(_CONTRIBUTOR_QUERY, {"b_user_id": user_id})).one()
    return claims_from_codes(row.role_codes, row.permission_codes)
