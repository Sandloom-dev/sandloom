"""C022 remediation: real built-in routes, committed effects and audit rows.

Risk classification must not grant authority or impose credential step-up on
membership operations. An old/missing auth_time is intentional in these tests.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from sandloom.core.config.settings import settings
from sandloom.core.unit_of_work.request import RequestScope, bind_request_scope
from sandloom.sdk.testing import (
    as_user,
    email_of,
    in_process_rpc_client,
    rpc_proxy_for,
    rpc_settings,
)
from sandloom.sdk.testing.fixtures import *  # noqa: F403
from sandloom_identity.models.user import User

pytestmark = pytest.mark.asyncio


def _user(auth_age: int | None = 3600) -> tuple[uuid.UUID, dict[str, str]]:
    claims = {} if auth_age is None else {"auth_time": int(time.time()) - auth_age}
    return as_user(tenant=False, **claims)


async def _automatic_audits(engine: Any, actor: uuid.UUID, route: str) -> list[Any]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT data FROM audit_log WHERE actor_id = :actor "
                "AND action = 'sandloom.sensitive_write' "
                "AND data ->> 'route' = :route"
            ),
            {"actor": actor, "route": route},
        )
        return list(rows.scalars())


def _assert_admin_audit(rows: list[Any], status: int) -> None:
    assert len(rows) == 1, rows
    assert set(rows[0]["risks"]) == {"admin"}
    assert rows[0]["status"] == status
    assert "body" not in rows[0]


@pytest.mark.parametrize("auth_age", [3600, None], ids=["old-login", "no-auth-time"])
async def test_self_profile_update_is_audited_without_granting_admin_authority(
    real_uow_client: Any, db_engine: Any, auth_age: int | None
) -> None:
    actor, headers = _user(auth_age)
    other = uuid.uuid4()
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions.begin() as db:
        db.add_all(
            [
                User(id=uid, email=f"{uid.hex}@example.com", full_name="Before")
                for uid in (actor, other)
            ]
        )
    route = "/api/v1/users/{user_id}"
    response = await real_uow_client.put(
        f"/api/v1/users/{actor}",
        json={"full_name": "Private new name"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    audits = await _automatic_audits(db_engine, actor, route)
    _assert_admin_audit(audits, 200)
    assert "Private new name" not in str(audits)

    denied = await real_uow_client.put(
        f"/api/v1/users/{other}", json={"full_name": "Forbidden"}, headers=headers
    )
    assert denied.status_code == 403, denied.text
    async with sessions() as db:
        assert (
            await db.scalar(select(User.full_name).where(User.id == other)) == "Before"
        )
    assert await _automatic_audits(db_engine, actor, route) == audits


async def _invitation(
    client: Any, owner_headers: dict[str, str], member: uuid.UUID
) -> tuple[str, str]:
    response = await client.post(
        "/api/v1/orgs",
        json={"name": "Risk audit", "slug": f"risk-{uuid.uuid4().hex[:12]}"},
        headers=owner_headers,
    )
    assert response.status_code == 201, response.text
    org = response.json()["id"]
    response = await client.post(
        f"/api/v1/orgs/{org}/invitations",
        json={"email": email_of(member)},
        headers=owner_headers,
    )
    assert response.status_code == 201, response.text
    return org, response.json()["token"]


@pytest.mark.parametrize("auth_age", [3600, None], ids=["old-login", "no-auth-time"])
async def test_invitation_and_self_leave_keep_their_own_authority_and_one_audit(
    real_uow_client: Any, db_engine: Any, auth_age: int | None
) -> None:
    _, owner_headers = _user(auth_age)
    member, headers = _user(auth_age)
    attacker, attacker_headers = _user(auth_age)
    org, token = await _invitation(real_uow_client, owner_headers, member)
    accept_route = "/api/v1/invitations/accept"
    denied = await real_uow_client.post(
        accept_route, json={"token": token}, headers=attacker_headers
    )
    assert denied.status_code == 403, denied.text
    assert await _automatic_audits(db_engine, attacker, accept_route) == []
    response = await real_uow_client.post(
        accept_route, json={"token": token}, headers=headers
    )
    assert response.status_code == 200, response.text
    _assert_admin_audit(await _automatic_audits(db_engine, member, accept_route), 200)
    response = await real_uow_client.delete(
        f"/api/v1/orgs/{org}/members/{member}", headers=headers
    )
    assert response.status_code == 204, response.text
    leave_route = "/api/v1/orgs/{org_id}/members/{member_user_id}"
    audits = await _automatic_audits(db_engine, member, leave_route)
    _assert_admin_audit(audits, 204)
    assert len(audits[0]["risks"]["admin"]) == 2  # route + effect, one audit
    assert (
        await real_uow_client.get(f"/api/v1/orgs/{org}", headers=headers)
    ).status_code == 401  # self-revocation committed without rejecting the leave


@pytest.mark.parametrize(
    "operation", ["change_role", "remove", "delete_org", "transfer"]
)
async def test_membership_administration_keeps_old_login_and_revocation_guarantees(
    real_uow_client: Any, db_engine: Any, operation: str
) -> None:
    owner, headers = _user()
    member, member_headers = _user()
    org, token = await _invitation(real_uow_client, headers, member)
    accepted = await real_uow_client.post(
        "/api/v1/invitations/accept", json={"token": token}, headers=member_headers
    )
    assert accepted.status_code == 200, accepted.text
    if operation == "change_role":
        route = "/api/v1/orgs/{org_id}/members/{member_user_id}"
        response = await real_uow_client.patch(
            f"/api/v1/orgs/{org}/members/{member}",
            json={"role": "admin"},
            headers=headers,
        )
        status = 200
    elif operation == "remove":
        route = "/api/v1/orgs/{org_id}/members/{member_user_id}"
        response = await real_uow_client.delete(
            f"/api/v1/orgs/{org}/members/{member}", headers=headers
        )
        status = 204
    elif operation == "delete_org":
        route = "/api/v1/orgs/{org_id}"
        response = await real_uow_client.delete(f"/api/v1/orgs/{org}", headers=headers)
        status = 204
    else:
        route = "/api/v1/orgs/{org_id}/transfer-ownership/{new_owner_id}"
        response = await real_uow_client.post(
            f"/api/v1/orgs/{org}/transfer-ownership/{member}", headers=headers
        )
        status = 200
    assert response.status_code == status, response.text
    _assert_admin_audit(await _automatic_audits(db_engine, owner, route), status)
    assert (
        await real_uow_client.get(f"/api/v1/orgs/{org}", headers=member_headers)
    ).status_code == 401


@pytest.mark.parametrize("remote", [False, True], ids=["local", "rpc"])
async def test_operator_revocation_reports_admin_risk_on_execution_and_replay(
    real_uow_client: Any,
    db_engine: Any,
    sandloom_core: Any,
    monkeypatch: pytest.MonkeyPatch,
    remote: bool,
) -> None:
    """Exercise the actual operator contract, including durable replay. The
    real_uow_client fixture supplies the app's real database/cache wiring."""
    actor = uuid.uuid4()
    target = uuid.uuid4()
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions.begin() as db:
        db.add(User(id=target, email=f"{target.hex}@example.com", full_name="Target"))
    contract = "identity.security.v1"
    for name, value in rpc_settings("c022", allowed_calls={"c022": [contract]}).items():
        monkeypatch.setattr(settings, name, value)
    key = uuid.uuid4().hex
    async with in_process_rpc_client(sandloom_core) as client:
        proxy = (
            rpc_proxy_for(sandloom_core, contract, client)
            if remote
            else sandloom_core.get_service(contract)
        )
        for _ in range(2):
            scope = RequestScope(core=sandloom_core, policy=None)
            scope.subject = str(actor)
            scope.claims = {
                "sub": str(actor),
                "iat": int(time.time()),
                "auth_time": int(time.time()) - 3600,
            }
            with bind_request_scope(scope):
                count = await proxy.revoke_sessions(
                    user_ids=[target],
                    reason="C022 regression",
                    operator="test",
                    operation_key=key,
                )
            assert count == 1
            assert scope.risk is not None
            assert scope.risk.risks == {
                "admin": {"sandloom_identity.contracts._end_sessions"}
            }
            assert scope.risk.step_up == []
    async with db_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT action FROM audit_log WHERE resource_id = :target"),
            {"target": str(target)},
        )
        assert list(rows.scalars()) == ["identity.user.sessions_revoked"]
