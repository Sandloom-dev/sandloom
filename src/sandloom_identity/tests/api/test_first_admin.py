"""The first administrator: ``identity.security.v1`` create / take-over / links.

``sandloom admin create`` creates an administrator with no usable password
(a command: no secret in its arguments or its stored result), then mints a
set-password link (a read: never stored). The link is a token of its own
purpose (15 minutes, its own salt) that identity's reset path accepts once.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from itsdangerous.timed import TimestampSigner
from sqlalchemy import MetaData, Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sandloom.sdk.errors import ContractCallError
from sandloom.sdk.testing import bind_contract_session, bind_test_uow, set_environment
from sandloom_identity import lockout
from sandloom_identity.contracts import SECURITY_SERVICE_V1
from sandloom_identity.models.role import Role
from sandloom_identity.models.user import User
from sandloom_identity.models.user_role import UserRole
from sandloom_identity.services.auth_service import AuthService
from sandloom_identity.tests.utils.kernel_tables import AUDIT_LOG
from sandloom_identity.utils import SET_PASSWORD_TOKEN_SECONDS

NEW_PASSWORD = "Fresh-Admin-Passw0rd!"
REASON, OPERATOR = "first administrator", "alice"


def _security(app: Any) -> Any:
    return app.state.core.get_service(SECURITY_SERVICE_V1)


async def _create(app: Any, db: AsyncSession, email: str, key: str = "op-c1") -> UUID:
    with bind_contract_session(db), bind_test_uow(db):
        user_id: UUID = await _security(app).create_platform_admin(
            email, "First Admin", REASON, OPERATOR, key
        )
    return user_id


async def _mint(app: Any, db: AsyncSession, user_id: UUID) -> str | None:
    with bind_contract_session(db):
        token: str | None = await _security(app).set_password_token(user_id)
    return token


async def _login(client: AsyncClient, email: str, password: str) -> Any:
    return await client.post("/auth/login", json={"email": email, "password": password})


async def _reset(client: AsyncClient, token: str, password: str = NEW_PASSWORD) -> Any:
    return await client.post(
        "/auth/reset-password", json={"token": token, "new_password": password}
    )


async def test_create_makes_a_verified_admin_without_a_usable_password(
    sandloom_app: Any, async_client: AsyncClient, test_db: AsyncSession
) -> None:
    user_id = await _create(sandloom_app, test_db, "owner@example.com")
    user = await test_db.get(User, user_id)
    assert user is not None
    assert user.hashed_password is None
    assert user.password_reset_required is True
    assert user.password_reset_nonce
    assert user.is_verified is True
    roles = await test_db.execute(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    assert list(roles.scalars()) == ["admin"]
    audit = await test_db.execute(
        select(AUDIT_LOG.c.action, AUDIT_LOG.c.actor_type).where(
            AUDIT_LOG.c.resource_id == str(user_id)
        )
    )
    assert ("identity.user.platform_admin_created", "operator") in [
        tuple(r) for r in audit
    ]
    with bind_contract_session(test_db):
        assert await _security(sandloom_app).platform_admin_exists() is True


async def test_the_link_works_once_and_the_admin_signs_in(
    sandloom_app: Any, async_client: AsyncClient, test_db: AsyncSession
) -> None:
    user_id = await _create(sandloom_app, test_db, "owner@example.com")
    token = await _mint(sandloom_app, test_db, user_id)
    assert token
    first = await _reset(async_client, token)
    assert first.status_code == 204, first.text
    again = await _reset(async_client, token, "Another-Passw0rd!")
    assert again.status_code == 400
    signed_in = await _login(async_client, "owner@example.com", NEW_PASSWORD)
    assert signed_in.status_code == 200, signed_in.text
    # Nothing left to mint once the password is set.
    assert await _mint(sandloom_app, test_db, user_id) is None


async def test_setting_the_first_password_is_audited_as_a_set(
    sandloom_app: Any, async_client: AsyncClient, test_db: AsyncSession
) -> None:
    """A set-password link (``admin create`` / ``invite``) sets a password the
    account never had: the journal says so, not ``identity.password.reset``
    (which a user's own reset request still records)."""
    user_id = await _create(sandloom_app, test_db, "owner@example.com")
    token = await _mint(sandloom_app, test_db, user_id)
    assert token
    assert (await _reset(async_client, token)).status_code == 204
    audit = await test_db.execute(
        select(AUDIT_LOG.c.action).where(AUDIT_LOG.c.resource_id == str(user_id))
    )
    actions = list(audit.scalars())
    assert "identity.user.password_set" in actions
    assert "identity.password.reset" not in actions


async def test_list_platform_admins_tells_which_have_a_password(
    sandloom_app: Any, async_client: AsyncClient, test_db: AsyncSession
) -> None:
    first = await _create(sandloom_app, test_db, "owner@example.com")
    await _create(sandloom_app, test_db, "second@example.com", key="op-c2")
    token = await _mint(sandloom_app, test_db, first)
    assert token and (await _reset(async_client, token)).status_code == 204
    assert (
        await _login(async_client, "owner@example.com", NEW_PASSWORD)
    ).status_code == 200
    with bind_contract_session(test_db):
        admins = await _security(sandloom_app).list_platform_admins()
    by_email = {a.email: a for a in admins}
    assert set(by_email) == {"owner@example.com", "second@example.com"}
    assert by_email["owner@example.com"].password_set is True
    assert by_email["owner@example.com"].last_login_at is not None
    assert by_email["second@example.com"].password_set is False
    assert by_email["second@example.com"].last_login_at is None


async def test_an_audited_listing_records_who_asked_and_why(
    sandloom_app: Any, test_db: AsyncSession
) -> None:
    """``sandloom admin list`` in production: who asked, why, and whether the
    full addresses were shown; no address in the record."""
    with bind_contract_session(test_db), bind_test_uow(test_db):
        assert await _security(sandloom_app).record_admin_listing(
            "incident 42", "cli:alice", True, "op-list-1"
        )
    rows = await test_db.execute(
        select(AUDIT_LOG.c.action, AUDIT_LOG.c.actor_type, AUDIT_LOG.c.data).where(
            AUDIT_LOG.c.action == "identity.admin.listed"
        )
    )
    (_, actor_type, data) = rows.one()
    assert actor_type == "operator"
    assert data["reason"] == "incident 42" and data["operator"] == "cli:alice"
    assert data["show_emails"] is True
    assert "@" not in str(data)


async def test_the_link_expires_after_fifteen_minutes(
    sandloom_app: Any,
    async_client: AsyncClient,
    test_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert SET_PASSWORD_TOKEN_SECONDS == 15 * 60
    user_id = await _create(sandloom_app, test_db, "owner@example.com")
    token = await _mint(sandloom_app, test_db, user_id)
    assert token
    later = int(time.time()) + SET_PASSWORD_TOKEN_SECONDS + 5
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: later)
    expired = await _reset(async_client, token)
    assert expired.status_code == 400


async def test_a_public_reset_request_invalidates_a_pending_link(
    sandloom_app: Any, async_client: AsyncClient, test_db: AsyncSession
) -> None:
    user_id = await _create(sandloom_app, test_db, "owner@example.com")
    token = await _mint(sandloom_app, test_db, user_id)
    assert token
    asked = await async_client.post(
        "/auth/request-password-reset", json={"email": "owner@example.com"}
    )
    assert asked.status_code == 204
    assert (await _reset(async_client, token)).status_code == 400
    # The operator issues a new one.
    with bind_contract_session(test_db), bind_test_uow(test_db):
        assert await _security(sandloom_app).reissue_set_password(
            user_id, "new link", OPERATOR, "op-i1"
        )
    fresh = await _mint(sandloom_app, test_db, user_id)
    assert fresh and (await _reset(async_client, fresh)).status_code == 204


async def test_setting_the_password_clears_the_lockout(
    sandloom_app: Any, async_client: AsyncClient, test_db: AsyncSession
) -> None:
    user_id = await _create(sandloom_app, test_db, "owner@example.com")
    subject = lockout.subject_for_email("owner@example.com")
    for _ in range(lockout.FAIL_THRESHOLD):
        await lockout.register_failure(subject, client_ip="127.0.0.1")
    locked = await _login(async_client, "owner@example.com", NEW_PASSWORD)
    assert locked.status_code == 429
    token = await _mint(sandloom_app, test_db, user_id)
    assert token and (await _reset(async_client, token)).status_code == 204
    signed_in = await _login(async_client, "owner@example.com", NEW_PASSWORD)
    assert signed_in.status_code == 200, signed_in.text


async def test_an_existing_account_is_refused(
    sandloom_app: Any, test_db: AsyncSession, test_user: User
) -> None:
    with pytest.raises(ContractCallError) as info:
        await _create(sandloom_app, test_db, test_user.email.upper())
    assert info.value.status_code == 409
    with bind_contract_session(test_db):
        standing = await _security(sandloom_app).account_standing(test_user.email)
    assert standing is not None
    assert standing.user_id == test_user.id
    assert standing.is_platform_admin is False


async def test_minting_needs_an_admin_who_must_set_a_password(
    sandloom_app: Any, test_db: AsyncSession, test_user: User, admin_user: User
) -> None:
    test_user.password_reset_nonce = "n" * 32
    test_user.password_reset_required = True
    await test_db.commit()
    assert await _mint(sandloom_app, test_db, test_user.id) is None
    assert await _mint(sandloom_app, test_db, admin_user.id) is None


async def test_granting_an_unverified_account_is_refused_take_over_is_not(
    sandloom_app: Any,
    async_client: AsyncClient,
    test_db: AsyncSession,
    make_user: Any,
) -> None:
    squatter = await make_user(
        "owner@example.com", password="Squatter-Passw0rd!", verified=False
    )
    contract = _security(sandloom_app)
    with bind_contract_session(test_db), bind_test_uow(test_db):
        with pytest.raises(ContractCallError) as info:
            await contract.appoint_platform_admin(squatter.id, REASON, OPERATOR, "g1")
        assert info.value.status_code == 409
        assert await contract.take_over_platform_admin(
            squatter.id, REASON, OPERATOR, "t1"
        )
    await test_db.refresh(squatter)
    assert squatter.hashed_password is None
    assert squatter.is_verified is True
    refused = await _login(async_client, "owner@example.com", "Squatter-Passw0rd!")
    assert refused.status_code in {401, 403}
    token = await _mint(sandloom_app, test_db, squatter.id)
    assert token and (await _reset(async_client, token)).status_code == 204
    assert (
        await _login(async_client, "owner@example.com", NEW_PASSWORD)
    ).status_code == 200


async def test_taking_over_a_deactivated_account_reactivates_it(
    sandloom_app: Any,
    async_client: AsyncClient,
    test_db: AsyncSession,
    make_user: Any,
) -> None:
    """Never an administrator without a password and a link: the take-over
    reactivates the account, so the link it mints works."""
    squatter = await make_user(
        "owner@example.com", password="Squatter-Passw0rd!", verified=False, active=False
    )
    contract = _security(sandloom_app)
    with bind_contract_session(test_db), bind_test_uow(test_db):
        assert await contract.take_over_platform_admin(
            squatter.id, REASON, OPERATOR, "t-inactive"
        )
    await test_db.refresh(squatter)
    assert squatter.is_active is True
    token = await _mint(sandloom_app, test_db, squatter.id)
    assert token and (await _reset(async_client, token)).status_code == 204
    assert (
        await _login(async_client, "owner@example.com", NEW_PASSWORD)
    ).status_code == 200


async def test_setup_state_tells_whether_an_admin_exists(
    sandloom_app: Any, async_client: AsyncClient, test_db: AsyncSession
) -> None:
    empty = await async_client.get("/api/v1/identity/setup-state")
    assert empty.status_code == 200
    assert empty.json() == {"has_admin": False}
    await _create(sandloom_app, test_db, "owner@example.com")
    full = await async_client.get("/api/v1/identity/setup-state")
    assert full.json() == {"has_admin": True}


async def test_setup_state_is_404_in_production(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_environment(monkeypatch, "prod")
    assert (await async_client.get("/api/v1/identity/setup-state")).status_code == 404


# --------------------------------------------- the real command log (commits) --


async def _command_log(engine: Any) -> list[str]:
    """Every column of the kernel's command log rows for the contract: the
    table is reflected (its model is the kernel's), so a column added later
    is scanned too."""
    async with engine.connect() as connection:
        log = await connection.run_sync(
            lambda sync: Table("contract_command_log", MetaData(), autoload_with=sync)
        )
        rows = await connection.execute(
            select(log).where(log.c.contract.like(f"%{SECURITY_SERVICE_V1}%"))
        )
        return [repr(tuple(row)) for row in rows]


async def test_the_command_log_holds_no_token_or_password(
    sandloom_app: Any, sandloom_core: Any, real_uow_client: AsyncClient, db_engine: Any
) -> None:
    contract = sandloom_core.get_service(SECURITY_SERVICE_V1)
    user_id = await contract.create_platform_admin(
        "owner@example.com", "First Admin", REASON, OPERATOR, "op-log-1"
    )
    token = await contract.set_password_token(user_id)
    assert token
    assert await contract.set_password_with_token(token, NEW_PASSWORD, "op-log-2")
    rows = await _command_log(db_engine)
    assert len(rows) == 2
    for row in rows:
        assert token not in row
        assert NEW_PASSWORD not in row
        for part in filter(None, token.split(".")):
            assert part not in row
    async with async_sessionmaker(db_engine)() as session:
        user = await session.scalar(select(User).where(User.id == user_id))
        assert user is not None and user.hashed_password is not None
        assert await AuthService.averify_password(NEW_PASSWORD, user.hashed_password)


async def test_no_stored_record_depends_on_the_password(
    sandloom_app: Any, sandloom_core: Any, real_uow_client: AsyncClient, db_engine: Any
) -> None:
    """The token and the password are left out of the stored args digest:
    the same operation key with another password replays the first result
    (a digest over the password would refuse it as a conflict)."""
    contract = sandloom_core.get_service(SECURITY_SERVICE_V1)
    user_id = await contract.create_platform_admin(
        "owner@example.com", "First Admin", REASON, OPERATOR, "op-digest-1"
    )
    token = await contract.set_password_token(user_id)
    assert token
    assert await contract.set_password_with_token(token, NEW_PASSWORD, "op-digest-2")
    other = "Another-Admin-Passw0rd!"
    assert await contract.set_password_with_token("x.y.z", other, "op-digest-2")
    async with async_sessionmaker(db_engine)() as session:
        user = await session.scalar(select(User).where(User.id == user_id))
        assert user is not None and user.hashed_password is not None
        assert await AuthService.averify_password(NEW_PASSWORD, user.hashed_password)
        assert not await AuthService.averify_password(other, user.hashed_password)


async def test_two_concurrent_creates_make_one_user(
    sandloom_core: Any, real_uow_client: AsyncClient, db_engine: Any
) -> None:
    contract = sandloom_core.get_service(SECURITY_SERVICE_V1)
    results = await asyncio.gather(
        *(
            contract.create_platform_admin(
                "owner@example.com", "First Admin", REASON, f"agent-{n}", f"op-race-{n}"
            )
            for n in range(2)
        ),
        return_exceptions=True,
    )
    created = [r for r in results if isinstance(r, UUID)]
    refused = [r for r in results if isinstance(r, BaseException)]
    assert len(created) == 1, results
    assert len(refused) == 1 and getattr(refused[0], "status_code", None) == 409
    async with async_sessionmaker(db_engine)() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email == "owner@example.com")
        )
    assert count == 1
