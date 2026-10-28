"""Emergency account safety: block, unblock, end every session, reset 2FA.

Class ``A`` platform-administrator commands. The kernel's ``block_user`` /
``unblock_user`` write their own ``required=True`` audit row and the block
mark; ``revoke-sessions`` and ``reset-2fa`` audit here. Every command
re-checks the admin role at commit (``admin_in_transaction``) and is
deduplicated fail-closed on the ``Idempotency-Key`` header, so a retried
request never repeats the effect.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from sandloom.sdk import record_audit
from sandloom.sdk.security import block_user, is_user_blocked, unblock_user
from sandloom.sdk.web import (
    DbSession,
    current_user_id,
    idempotency_guard,
    require_platform_admin,
)
from sandloom_identity.access import admin_in_transaction
from sandloom_identity.schemas.safety import BlockStatusOut, SafetyReason
from sandloom_identity.services.token_service import TokenService
from sandloom_identity.services.twofa_service import TwoFAService
from sandloom_identity.services.user_service import UserService
from sandloom_identity.sessions import revoke_sessions

user_safety_router = APIRouter(
    prefix="/api/v1",
    tags=["user-safety"],
    dependencies=[
        Depends(require_platform_admin),
    ],
)

_COMMAND = [
    Depends(admin_in_transaction),
]


def _not_self(user_id: UUID, actor_id: UUID) -> None:
    if user_id == actor_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Operators cannot apply this to their own account"
        )


@user_safety_router.get(
    "/users/{user_id}/block",
    response_model=BlockStatusOut,
)
async def get_block_status(user_id: UUID, db: DbSession) -> BlockStatusOut:
    """Live block state (one cache read; 503 when the cache is unreachable)."""
    await UserService.get_by_id(user_id, db)
    try:
        blocked = await is_user_blocked(user_id)
    except Exception as exc:  # surfaced as an explicit 503
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Block state is unavailable"
        ) from exc
    return BlockStatusOut(id=str(user_id), blocked=blocked)


@user_safety_router.post(
    "/users/{user_id}/block",
    response_model=BlockStatusOut,
    dependencies=[
        *_COMMAND,
        Depends(idempotency_guard(scope="identity.user.block", fail_closed=True)),
    ],
)
async def block(
    user_id: UUID,
    data: SafetyReason,
    db: DbSession,
    actor_id: UUID = Depends(current_user_id),
) -> BlockStatusOut:
    """Emergency stop: every request and sign-in of the user is refused."""
    _not_self(user_id, actor_id)
    await UserService.get_by_id(user_id, db)
    await block_user(user_id, reason=data.reason, actor_id=actor_id)
    return BlockStatusOut(id=str(user_id), blocked=True)


@user_safety_router.post(
    "/users/{user_id}/unblock",
    response_model=BlockStatusOut,
    dependencies=[
        *_COMMAND,
        Depends(idempotency_guard(scope="identity.user.unblock", fail_closed=True)),
    ],
)
async def unblock(
    user_id: UUID,
    data: SafetyReason,
    db: DbSession,
    actor_id: UUID = Depends(current_user_id),
) -> BlockStatusOut:
    """Lift a block. Sessions ended by the block stay ended."""
    await UserService.get_by_id(user_id, db)
    await unblock_user(user_id, reason=data.reason, actor_id=actor_id)
    return BlockStatusOut(id=str(user_id), blocked=False)


@user_safety_router.post(
    "/users/{user_id}/revoke-sessions",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        *_COMMAND,
        Depends(
            idempotency_guard(scope="identity.user.revoke_sessions", fail_closed=True)
        ),
    ],
)
async def revoke_user_sessions(
    user_id: UUID,
    data: SafetyReason,
    db: DbSession,
    actor_id: UUID = Depends(current_user_id),
) -> None:
    """End every session: refresh tokens revoked, access tokens rejected."""
    _not_self(user_id, actor_id)
    await UserService.get_by_id(user_id, db)
    record_audit(
        "identity.user.sessions_revoked",
        actor_id=actor_id,
        resource_type="user",
        resource_id=str(user_id),
        data={"reason": data.reason},
        required=True,
    )
    await TokenService.revoke_all_user_tokens(user_id, db)
    # Last fallible step: the mark is written before commit (or 503 + rollback).
    await revoke_sessions(
        user_id, reason=f"operator revoked sessions: {data.reason}", actor_id=actor_id
    )


@user_safety_router.post(
    "/users/{user_id}/reset-2fa",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        *_COMMAND,
        Depends(idempotency_guard(scope="identity.user.reset_2fa", fail_closed=True)),
    ],
)
async def reset_user_2fa(
    user_id: UUID,
    data: SafetyReason,
    db: DbSession,
    actor_id: UUID = Depends(current_user_id),
) -> None:
    """Remove a user's second factor (lost authenticator AND backup codes).

    Verify the user's identity out of band first: this lets the password
    alone sign in until they enrol again. Every session ends with it.
    """
    _not_self(user_id, actor_id)
    await UserService.get_by_id(user_id, db)
    await TwoFAService.reset_by_operator(
        user_id, db, actor_id=actor_id, reason=data.reason
    )
    await TokenService.revoke_all_user_tokens(user_id, db)
    # Last fallible step: the mark is written before commit (or 503 + rollback).
    await revoke_sessions(
        user_id, reason=f"operator reset 2FA: {data.reason}", actor_id=actor_id
    )
