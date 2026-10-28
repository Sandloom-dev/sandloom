"""Infrastructure failures are not bad credentials or anonymous sessions."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from sandloom_identity.access import get_current_user, get_optional_user


@pytest.mark.parametrize("guard", [get_current_user, get_optional_user])
@pytest.mark.parametrize(
    "error",
    [
        # the kernel's dead-connection error is a RuntimeError (503 upstream)
        RuntimeError("database connection died"),
        ConnectionRefusedError(),
        TimeoutError(),
    ],
)
async def test_database_failure_escapes_the_authentication_guard(guard, error):
    db = AsyncMock()
    db.execute.side_effect = error
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="fixture")
    with patch(
        "sandloom_identity.access.decode_token",
        return_value={
            "token_type": "access",
            "sub": str(uuid4()),
        },
    ):
        with pytest.raises(type(error)) as raised:
            await guard(credentials, db)
    assert raised.value is error


async def test_optional_user_keeps_fail_closed_revocation_unavailability():
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="fixture")
    with (
        patch(
            "sandloom_identity.access.AuthService.get_current_user",
            return_value=object(),
        ),
        patch(
            "sandloom_identity.access._revoked",
            side_effect=HTTPException(503, "Unavailable"),
        ),
    ):
        with pytest.raises(HTTPException) as raised:
            await get_optional_user(credentials, AsyncMock())
    assert raised.value.status_code == 503
