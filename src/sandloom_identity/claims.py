"""Identity's token-claims contributor.

Resolves the user's global roles and the permissions those roles grant, and
returns them as JWT claims. Runs on the mint cold path (login / refresh) only —
the per-request authorization path then reads these claims with zero I/O.

:func:`identity_claims_lateral` is the SINGLE source of truth for these claims:
the contributor runs it on its own, and the refresh rotation joins it into its
compare-and-set statement and hands the result to claims collection as
precomputed for :func:`contribute_identity_claims` (one round trip less per
refresh). Both paths build the claims with :func:`claims_from_codes`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Select, bindparam, func, select
from sqlalchemy.orm import QueryableAttribute
from sqlalchemy.sql.selectable import LateralFromClause

from sandloom.core.module_api import contract_session
from sandloom_identity.models import Permission, Role, RolePermission, UserRole


def identity_claims_select(
    user_id: ColumnElement[Any] | QueryableAttribute[Any] | UUID,
) -> Select[Sequence[str], Sequence[str]]:
    """Roles and permissions of ``user_id`` as ONE aggregate row.

    Columns ``role_codes`` / ``permission_codes``: arrays (``NULL`` when the
    user has no roles; a role without permissions adds a ``NULL`` permission).
    An aggregate without ``GROUP BY`` always yields exactly one row, so the
    LATERAL form joined ``ON true`` never multiplies or drops the outer row.
    """
    return (
        select(
            func.array_agg(Role.code).label("role_codes"),
            func.array_agg(Permission.code).label("permission_codes"),
        )
        .select_from(UserRole)
        .join(Role, UserRole.role_id == Role.id)
        .outerjoin(RolePermission, Role.id == RolePermission.role_id)
        .outerjoin(Permission, RolePermission.permission_id == Permission.id)
        .where(UserRole.user_id == user_id)
    )


def identity_claims_lateral(
    user_id: ColumnElement[Any] | QueryableAttribute[Any],
) -> LateralFromClause:
    """:func:`identity_claims_select` correlated to ``user_id`` (LATERAL)."""
    return identity_claims_select(user_id).lateral("identity_claims")


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
_CONTRIBUTOR_QUERY = identity_claims_select(bindparam("b_user_id"))


async def contribute_identity_claims(user_id: UUID) -> dict[str, Any]:
    """Return ``{"roles": [...], "permissions": [...]}`` for ``user_id``.

    ONE query in identity's contract session (the issuer's own request
    transaction when identity mints the token): the user's roles LEFT JOINed
    to the permissions they grant, aggregated (:func:`identity_claims_select`).
    """
    session = contract_session()
    row = (await session.execute(_CONTRIBUTOR_QUERY, {"b_user_id": user_id})).one()
    return claims_from_codes(row.role_codes, row.permission_codes)
