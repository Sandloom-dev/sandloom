"""Incident-response commands of identity.

* ``POST /api/v1/users/{id}/reset-2fa`` — the audited class-A operator reset
  of a lost second factor (A-09): secret + backup codes gone, sessions end.
* ``identity.security.v1`` — the contract behind ``sandloom security
  revoke-sessions`` / ``force-password-reset``: bulk, audited per user, and a
  forced reset refuses password sign-in until the reset flow clears it.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.testing import bind_contract_session, bind_test_uow
from sandloom_identity.contracts import SECURITY_SERVICE_V1
from sandloom_identity.models.refresh_token import RefreshToken
from sandloom_identity.models.twofa_backup_code import TwoFABackupCode
from sandloom_identity.models.twofa_secret import TwoFASecret
from sandloom_identity.models.user import User
from sandloom_identity.services.twofa_service import TwoFAService, _encrypt_secret

from ..conftest import _login_headers

REASON = {"reason": "lost phone and backup codes, ticket SUP-7"}
PASSWORD = "TestPassword123!"


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


async def _audit(db: AsyncSession, user: User) -> list[tuple[str, str, Any]]:
    rows = await db.execute(
        text(
            "SELECT action, actor_type, data FROM audit_log "
            "WHERE resource_id = :rid ORDER BY occurred_at"
        ),
        {"rid": str(user.id)},
    )
    return [(r[0], r[1], r[2]) for r in rows]


async def _enable_2fa(db: AsyncSession, user: User) -> None:
    db.add(
        TwoFASecret(
            user_id=user.id,
            secret=_encrypt_secret(pyotp.random_base32()),
            is_enabled=True,
        )
    )
    await TwoFAService._save_backup_codes(user, ["a" * 20, "b" * 20], db)
    await db.commit()


async def _login(client: AsyncClient, user: User) -> Any:
    return await client.post(
        "/auth/login", json={"email": user.email, "password": PASSWORD}
    )


@pytest.mark.asyncio
async def test_operator_reset_2fa_removes_factor_ends_sessions_and_audits(
    async_client: AsyncClient,
    test_db: AsyncSession,
    test_user: User,
    admin_user: User,
    admin_headers: dict[str, str],
) -> None:
    before = await _login(async_client, test_user)
    assert before.status_code == 200, before.text
    await _enable_2fa(test_db, test_user)
    needs_otp = await _login(async_client, test_user)
    assert needs_otp.status_code == 401
    assert needs_otp.headers.get("X-2FA-Required") == "true"

    url = f"/api/v1/users/{test_user.id}/reset-2fa"
    resp = await async_client.post(
        url, json=REASON, headers={**admin_headers, **_key()}
    )
    assert resp.status_code == 204, resp.text

    secrets = await test_db.scalar(
        select(func.count())
        .select_from(TwoFASecret)
        .where(TwoFASecret.user_id == test_user.id)
    )
    codes = await test_db.scalar(
        select(func.count())
        .select_from(TwoFABackupCode)
        .where(TwoFABackupCode.user_id == test_user.id)
    )
    assert secrets == codes == 0
    revoked = (
        await test_db.execute(
            select(RefreshToken.is_revoked)
            .where(RefreshToken.user_id == test_user.id)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    assert all(revoked)
    refreshed = await async_client.post(
        "/auth/refresh", json={"refresh_token": before.json()["refresh_token"]}
    )
    assert refreshed.status_code == 401
    audit = await _audit(test_db, test_user)
    assert audit[0][0] == "identity.twofa.reset"
    assert audit[0][2] == {"reason": REASON["reason"], "was_enabled": True}
    actor = await test_db.scalar(
        text(
            "SELECT actor_id FROM audit_log WHERE action = 'identity.twofa.reset' "
            "AND resource_id = :rid"
        ),
        {"rid": str(test_user.id)},
    )
    assert str(actor) == str(admin_user.id)
    # The password alone signs in again (the user re-enrols).
    assert (await _login(async_client, test_user)).status_code == 200

    # Nothing left to reset: 404.
    again = await async_client.post(
        url, json=REASON, headers={**admin_headers, **_key()}
    )
    assert again.status_code == 404


@pytest.mark.asyncio
async def test_reset_2fa_is_admin_only_not_self_and_needs_idempotency_key(
    async_client: AsyncClient,
    test_db: AsyncSession,
    test_user: User,
    admin_user: User,
    admin_headers: dict[str, str],
    sandloom_app: Any,
) -> None:
    user_headers = await _login_headers(test_user, test_db, sandloom_app.state.core)
    url = f"/api/v1/users/{admin_user.id}/reset-2fa"
    forbidden = await async_client.post(
        url, json=REASON, headers={**user_headers, **_key()}
    )
    assert forbidden.status_code == 403
    own = await async_client.post(url, json=REASON, headers={**admin_headers, **_key()})
    assert own.status_code == 409
    missing_key = await async_client.post(
        f"/api/v1/users/{test_user.id}/reset-2fa", json=REASON, headers=admin_headers
    )
    assert missing_key.status_code == 400


@pytest.mark.asyncio
async def test_security_contract_revokes_sessions_in_bulk_with_operator_audit(
    async_client: AsyncClient,
    test_db: AsyncSession,
    test_user: User,
    admin_user: User,
    sandloom_app: Any,
) -> None:
    login = await _login(async_client, test_user)
    assert login.status_code == 200
    contract = sandloom_app.state.core.get_service(SECURITY_SERVICE_V1)
    with bind_contract_session(test_db), bind_test_uow(test_db):
        page = await contract.list_user_ids(None, 10)
        assert {test_user.id, admin_user.id} <= set(page)
        assert page == sorted(page)
        tail = await contract.list_user_ids(page[0], 10)
        assert page[0] not in tail
        affected = await contract.revoke_sessions(
            [test_user.id, uuid4()], "credential leak SEC-9", "alice", "op-1"
        )
    assert affected == 1
    refreshed = await async_client.post(
        "/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    assert refreshed.status_code == 401
    assert await _audit(test_db, test_user) == [
        (
            "identity.user.sessions_revoked",
            "operator",
            {
                "reason": "credential leak SEC-9",
                "operator": "alice",
                "operation_key": "op-1",
            },
        )
    ]


@pytest.mark.asyncio
async def test_forced_password_reset_refuses_password_login_until_reset(
    async_client: AsyncClient,
    test_db: AsyncSession,
    test_user: User,
    sandloom_app: Any,
) -> None:
    from sandloom_identity.services.auth_service import AuthService
    from sandloom_identity.utils import email_token_utils

    contract = sandloom_app.state.core.get_service(SECURITY_SERVICE_V1)
    with bind_contract_session(test_db), bind_test_uow(test_db):
        assert (
            await contract.require_password_reset(
                [test_user.id], "database leak SEC-10", "bob", "op-2"
            )
            == 1
        )
    await test_db.refresh(test_user)
    assert test_user.password_reset_required is True

    refused = await _login(async_client, test_user)
    assert refused.status_code == 403, refused.text
    assert refused.headers.get("X-Password-Reset-Required") == "true"
    # A wrong password learns nothing about the flag.
    wrong = await async_client.post(
        "/auth/login", json={"email": test_user.email, "password": "Wrong123!xyz"}
    )
    assert wrong.status_code == 401
    assert [a[0] for a in await _audit(test_db, test_user)] == [
        "identity.user.password_reset_required"
    ]

    # The emailed reset flow clears the flag.
    test_user.password_reset_nonce = "n" * 32
    await test_db.commit()
    token = email_token_utils.generate_password_reset_token(
        test_user.id,
        nonce=test_user.password_reset_nonce,
        hashed_password=test_user.hashed_password,
    )
    await AuthService.reset_password(token, "BrandNewPassw0rd!", test_db)
    await test_db.commit()
    await test_db.refresh(test_user)
    assert test_user.password_reset_required is False
    signed_in = await async_client.post(
        "/auth/login",
        json={"email": test_user.email, "password": "BrandNewPassw0rd!"},
    )
    assert signed_in.status_code == 200, signed_in.text


@pytest.mark.asyncio
async def test_security_contract_rejects_oversized_batches(
    test_db: AsyncSession, sandloom_app: Any
) -> None:
    from sandloom.sdk.errors import ContractCallError

    contract = sandloom_app.state.core.get_service(SECURITY_SERVICE_V1)
    with bind_contract_session(test_db), pytest.raises(ContractCallError) as info:
        await contract.revoke_sessions(
            [uuid4() for _ in range(1001)], "too many", "eve", "op-3"
        )
    assert info.value.status_code == 422
