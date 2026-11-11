"""The refresh-session lifetimes are identity's declared settings (ADR-0015
decision 9, owner decision #13): the kernel ``Settings`` no longer holds them,
the env names are unchanged, and the access-JWT parameters stay in the kernel.
"""

from __future__ import annotations

import pytest

from sandloom.sdk.infra import settings
from sandloom.sdk.testing import ModuleSettingsError, validate_settings
from sandloom_identity.config import IdentitySettings

_MOVED = ("refresh_token_expire_days", "max_session_days")
_STAYS = (
    "access_token_expire_minutes",
    "jwt_secret",
    "jwt_leeway_seconds",
    "auth_revocation_fail_closed",
)


def test_the_kernel_settings_hold_no_refresh_session_lifetime() -> None:
    fields = type(settings).model_fields
    for name in _MOVED:
        assert name not in fields, name
    for name in _STAYS:
        assert name in fields, name


def test_identity_declares_both_lifetimes_under_the_same_env_names() -> None:
    fields = IdentitySettings.model_fields
    assert fields["refresh_token_expire_days"].alias == "REFRESH_TOKEN_EXPIRE_DAYS"
    assert fields["max_session_days"].alias == "MAX_SESSION_DAYS"
    assert fields["refresh_token_expire_days"].default == 14
    assert fields["max_session_days"].default == 30


@pytest.mark.parametrize("value", ["0", "366"])
def test_the_absolute_session_lifetime_is_bounded_by_the_declaration(
    value: str,
) -> None:
    with pytest.raises(ModuleSettingsError):
        validate_settings(IdentitySettings, env={"MAX_SESSION_DAYS": value})
