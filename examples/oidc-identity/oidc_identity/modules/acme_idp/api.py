"""Sign-in with the external issuer's id_token; its back-channel logout.

``POST /api/v1/acme_idp/login`` exchanges a verified id_token for this
application's access token: the external subject is mapped to a local
account (created on first sign-in), its platform roles are derived from the
provider's groups, and the token's claims are collected by the kernel from
every claim authority (acme_idp itself, tenancy's ``tenant_ids``, billing's
subscription claims). No refresh token: the client signs in again with a
fresh id_token (a stateless issuer, ``refresh_sessions=False``).

``POST /api/v1/acme_idp/backchannel-logout`` is the provider's call ending a
subject's sessions: every access token minted for the account is revoked
when it commits.

``POST /api/v1/acme_idp/accounts/{user_id}/block`` is a platform
administrator's emergency block (the kernel's ``block_user``): every token
of the account is refused at once, and the next sign-in too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from sandloom.sdk import CurrentUser, module_router, public
from sandloom.sdk.infra import (  # sandloom: advanced claims collection
    CoreRuntime,
    get_core,
)
from sandloom.sdk.security import (  # sandloom: advanced token issuing
    block_user,
    create_access_token,
    ensure_user_not_blocked,
    revoke_user_tokens_on_commit,
)
from sandloom.sdk.web import (  # sandloom: advanced session, route policy
    DbSession,
    no_idempotency,
    require_platform_admin,
)

from .claims import contribute
from .config import acme_idp_settings
from .oidc import ExternalTokenRejected, verify_id_token, verify_logout_token
from .services import mark_signed_out, sign_in_account

router = module_router()


class LoginIn(BaseModel):
    id_token: str


class LogoutIn(BaseModel):
    #: OIDC BCL posts it form-encoded; JSON here (python-multipart, which a
    #: FastAPI form needs, is not a framework dependency).
    logout_token: str


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: UUID
    claims: dict[str, Any]


@router.post(
    "/login",
    dependencies=[
        public(reason="OIDC sign-in: the caller has no token yet", issues_tokens=True),
        no_idempotency(reason="a sign-in mints a new token per call"),
    ],
)
async def login(
    body: LoginIn, db: DbSession, core: CoreRuntime = Depends(get_core)
) -> LoginOut:
    """Exchange the external issuer's id_token for an access token."""
    cfg = acme_idp_settings()
    try:
        external = await verify_id_token(cfg, body.id_token)
    except ExternalTokenRejected as exc:
        raise HTTPException(401, "id_token rejected") from exc
    groups = external.get("groups") or []
    user_id = await sign_in_account(
        db,
        issuer=external["iss"],
        subject=external["sub"],
        email=str(external["email"]),
        email_verified=external.get("email_verified") is True,
        roles=["admin"] if cfg.admin_group in groups else [],
    )
    await ensure_user_not_blocked(user_id)
    sealed = await core.collect_claims(user_id, db, issuer=contribute)
    token = create_access_token(
        data={
            "sub": str(user_id),
            "token_type": "access",
            "auth_time": int(datetime.now(UTC).timestamp()),
        },
        claims=sealed,
    )
    return LoginOut(access_token=token, user_id=user_id, claims=dict(sealed))


@router.post(
    "/backchannel-logout",
    dependencies=[
        public(reason="OIDC back-channel logout: the provider signs the request"),
        no_idempotency(reason="revoking twice is revoking once"),
    ],
)
async def backchannel_logout(body: LogoutIn, db: DbSession) -> dict[str, Any]:
    """End every session of the logout token's subject (OIDC BCL 1.0)."""
    cfg = acme_idp_settings()
    try:
        claims = await verify_logout_token(cfg, body.logout_token)
    except ExternalTokenRejected as exc:
        raise HTTPException(400, "logout_token rejected") from exc
    user_id = await mark_signed_out(db, issuer=claims["iss"], subject=claims["sub"])
    if user_id is None:
        return {"revoked": False}
    valid_after = await revoke_user_tokens_on_commit(
        user_id, reason="oidc back-channel logout", require_immediate=True
    )
    return {"revoked": True, "user_id": str(user_id), "valid_after": valid_after}


@router.post(
    "/accounts/{user_id}/block",
    dependencies=[
        Depends(require_platform_admin),
        no_idempotency(reason="blocking twice is blocking once"),
    ],
)
async def block_account(
    user_id: UUID, operator: CurrentUser, db: DbSession
) -> dict[str, bool]:
    """Emergency block: the account's tokens and sign-ins are refused."""
    await block_user(
        user_id, reason="acme_idp operator block", actor_id=operator.user_id
    )
    return {"blocked": True}
