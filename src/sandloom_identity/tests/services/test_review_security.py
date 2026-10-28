"""Regression tests for identity boundaries found in the 22 October review."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from sandloom_identity.schemas.twofa import TwoFADisableIn
from sandloom_identity.services.reauth import require_reauth
from sandloom_identity.services.sso_service import SSOService
from sandloom_identity.services.twofa_service import TwoFAService


@pytest.mark.asyncio
async def test_sso_link_refuses_stolen_bearer_before_consuming_oauth_state(
    make_user, test_db
) -> None:
    user = await make_user("link-step-up@example.com")
    with (
        patch.object(
            SSOService, "_validate_and_consume_state", new_callable=AsyncMock
        ) as oauth,
        patch.object(SSOService, "_exchange", new_callable=AsyncMock) as exchange,
    ):
        with pytest.raises(HTTPException) as exc:
            await SSOService.link_provider(user, "google", "code", "state", test_db)
    assert exc.value.status_code == 401
    oauth.assert_not_awaited()
    exchange.assert_not_awaited()


@pytest.mark.asyncio
async def test_sso_unlink_requires_step_up_before_querying_link(make_user, test_db):
    user = await make_user("unlink-step-up@example.com")
    with pytest.raises(HTTPException) as exc:
        await SSOService.unlink_provider(user, "google", test_db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_wrong_step_up_password_counts_toward_user_lockout(make_user):
    user = await make_user("step-up-limit@example.com")
    with (
        patch(
            "sandloom_identity.lockout.ensure_not_locked", new_callable=AsyncMock
        ) as check,
        patch(
            "sandloom_identity.lockout.register_failure", new_callable=AsyncMock
        ) as fail,
    ):
        with pytest.raises(HTTPException):
            await require_reauth(user, "incorrect-password")
    check.assert_awaited_once()
    fail.assert_awaited_once()


@pytest.mark.asyncio
async def test_backup_code_can_disable_second_factor_after_password_step_up(
    make_user, test_db
):
    from sandloom_identity.tests.services.test_twofa_service import _enabled_user

    user, setup = await _enabled_user(test_db, make_user, "recover-disable@example.com")
    code = setup.backup_codes[0]
    assert TwoFADisableIn(otp=code).otp == code
    result = await TwoFAService.disable_2fa(
        user, code, test_db, current_password="TestPassword123!"
    )
    assert result.enabled is False


@pytest.mark.asyncio
async def test_sso_signup_without_name_produces_valid_profile(make_user, test_db):
    from sandloom_identity.schemas.user import UserOut

    user, via = await SSOService._resolve_unlinked(
        "google",
        {"id": "nameless", "email": "nameless@example.com", "email_verified": True},
        test_db,
    )
    assert via == "signup"
    assert UserOut.model_validate(user).full_name


@pytest.mark.asyncio
async def test_concurrent_password_reset_consumes_nonce_only_once(
    make_user, test_db, test_engine, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sandloom.sdk.testing import committing_uow
    from sandloom_identity.services.auth_service import AuthService
    from sandloom_identity.tests.services.test_auth_hardening import _issue_reset_token

    user = await make_user("reset-concurrent-review@example.com")
    token = await _issue_reset_token(test_db, user)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    original = AuthService.ahash_password

    async def slow_hash(password):
        await asyncio.sleep(0.03)
        return await original(password)

    monkeypatch.setattr(AuthService, "ahash_password", slow_hash)

    async def reset():
        try:
            async with committing_uow(maker) as session:
                await AuthService.reset_password(token, "NewResetPassword123!", session)
            return "applied"
        except HTTPException as exc:
            return exc.status_code

    assert sorted(await asyncio.gather(reset(), reset()), key=str) == [400, "applied"]


@pytest.mark.asyncio
async def test_refresh_cannot_extend_primary_session_past_absolute_limit(
    make_user, test_db
):
    from sandloom.sdk.infra import settings
    from sandloom_identity.services.token_service import TokenService

    user = await make_user("absolute-session@example.com")
    tokens = await TokenService.create_tokens(
        user,
        test_db,
        auth_time=datetime.now(UTC) - timedelta(days=settings.max_session_days + 1),
    )
    await test_db.commit()
    with pytest.raises(HTTPException) as exc:
        await TokenService.refresh_token(tokens.refresh_token, test_db)
    assert exc.value.status_code == 401
    assert "Session lifetime expired" in exc.value.detail


@pytest.mark.asyncio
async def test_duplicate_username_is_a_conflict_and_does_not_poison_uow(
    make_user, test_db
):
    from sqlalchemy import select

    from sandloom_identity.models.user import User
    from sandloom_identity.schemas.user import UserUpdate
    from sandloom_identity.services.user_service import UserService

    owner = await make_user("name-owner@example.com")
    owner.username = "already_taken"
    await test_db.commit()
    user = await make_user("name-contender@example.com")
    user_id = user.id
    with pytest.raises(HTTPException) as exc:
        await UserService.update_user(
            user_id, UserUpdate(username="already_taken"), test_db
        )
    assert exc.value.status_code == 409
    assert (
        await test_db.execute(select(User.id).where(User.id == user_id))
    ).scalar_one()


@pytest.mark.asyncio
async def test_concurrent_unlink_cannot_remove_every_login_method(
    make_user, test_db, test_engine
):
    import time

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sandloom.sdk.testing import committing_uow
    from sandloom_identity.models.external_identity import ExternalIdentity, Provider

    user = await make_user("concurrent-unlink@example.com", password=None)
    user_id = user.id
    test_db.add_all(
        [
            ExternalIdentity(
                user_id=user_id, provider=Provider.GOOGLE, external_id="last-google"
            ),
            ExternalIdentity(
                user_id=user_id, provider=Provider.GITHUB, external_id="last-github"
            ),
        ]
    )
    await test_db.commit()
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    start = asyncio.Event()

    async def unlink(provider):
        await start.wait()
        try:
            async with committing_uow(maker) as session:
                await SSOService.unlink_provider(
                    user_id, provider, session, token_claims={"auth_time": time.time()}
                )
            return "removed"
        except HTTPException as exc:
            return exc.status_code

    tasks = [asyncio.create_task(unlink(provider)) for provider in ("google", "github")]
    start.set()
    assert sorted(await asyncio.gather(*tasks), key=str) == [400, "removed"]
    assert (
        await test_db.scalar(
            select(func.count())
            .select_from(ExternalIdentity)
            .where(ExternalIdentity.user_id == user_id)
        )
        == 1
    )
