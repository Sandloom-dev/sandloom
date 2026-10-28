"""Configurable argon2id parameters: presets, overrides, the prod OWASP floor,
the configured hasher and the never-blocking rehash helper."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from argon2 import PasswordHasher
from pydantic import ValidationError

from sandloom.core import di
from sandloom.core.auth import hashing
from sandloom.core.config.password_params import (
    OWASP_ARGON2ID_MINIMUMS,
    PasswordHashParams,
    meets_owasp_minimum,
    resolve_password_hash_params,
)
from sandloom.core.config.settings import Settings

_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
    "CACHE_URL": "redis://h:6379/0",
    "BROKER_URL": "redis://h:6379/1",
    "JWT_SECRET": "x" * 40,
    "JWT_PRIVATE_KEY": "",
}
_PROD_ENV = {
    "ENVIRONMENT": "prod",
    "JWT_SECRET": "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b",
    "SANDLOOM_ACCEPTED_RISKS": "fake_psp_in_prod",
}
#: A hash made with today's (``strong``) parameters by the former passlib setup.
STRONG_HASH_OF_X = (
    "$argon2id$v=19$m=65536,t=3,p=4$BYBQak0J4VwLgTAGoFTq3Q$"
    "lmXuTi3Uu24+VkCXRAXNgiJMcmrywJZVepUtBJ/JidI"
)
CHEAP = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


def _settings(**env: Any) -> Settings:
    return Settings(_env_file=None, **{**_BASE_ENV, **env})  # type: ignore[call-arg]


# -- resolution ---------------------------------------------------------------------


def test_defaults_are_unchanged() -> None:
    cfg = _settings()
    assert cfg.password_hash_profile == "strong"
    assert cfg.password_hash_params == PasswordHashParams(3, 65536, 4)
    assert hashing.hash_password("x").startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    hasher = di.password_hasher()
    assert (hasher.time_cost, hasher.memory_cost, hasher.parallelism) == (3, 65536, 4)
    assert (hasher.hash_len, hasher.salt_len) == (32, 16)


def test_owasp_min_preset() -> None:
    cfg = _settings(PASSWORD_HASH_PROFILE="owasp_min")
    assert cfg.password_hash_params == PasswordHashParams(2, 19456, 1)
    assert meets_owasp_minimum(cfg.password_hash_params)


def test_explicit_values_override_the_preset() -> None:
    cfg = _settings(PASSWORD_HASH_PROFILE="owasp_min", PASSWORD_HASH_MEMORY_KIB=47104)
    assert cfg.password_hash_params == PasswordHashParams(2, 47104, 1)
    cfg = _settings(PASSWORD_HASH_TIME_COST=4)
    assert cfg.password_hash_params == PasswordHashParams(4, 65536, 4)


def test_custom_requires_every_value() -> None:
    with pytest.raises(ValidationError, match="PASSWORD_HASH_MEMORY_KIB"):
        _settings(
            PASSWORD_HASH_PROFILE="custom",
            PASSWORD_HASH_TIME_COST=2,
            PASSWORD_HASH_PARALLELISM=1,
        )
    cfg = _settings(
        PASSWORD_HASH_PROFILE="custom",
        PASSWORD_HASH_TIME_COST=1,
        PASSWORD_HASH_MEMORY_KIB=47104,
        PASSWORD_HASH_PARALLELISM=2,
    )
    assert cfg.password_hash_params == PasswordHashParams(1, 47104, 2)


def test_unknown_profile_and_bad_memory_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(PASSWORD_HASH_PROFILE="paranoid")
    with pytest.raises(ValidationError, match="8 \\* parallelism"):
        _settings(PASSWORD_HASH_MEMORY_KIB=16, PASSWORD_HASH_PARALLELISM=4)
    with pytest.raises(ValueError, match="unknown"):
        resolve_password_hash_params("nope")


@pytest.mark.parametrize(("memory", "time_cost"), OWASP_ARGON2ID_MINIMUMS)
def test_every_owasp_equivalent_meets_the_floor(memory: int, time_cost: int) -> None:
    assert meets_owasp_minimum(PasswordHashParams(time_cost, memory, 1))
    assert not meets_owasp_minimum(PasswordHashParams(time_cost, memory - 1, 1))
    if time_cost > 1:
        # Less time at the same memory is only fine if a lower-t minimum's
        # memory is met — for these pairs it is not.
        assert not meets_owasp_minimum(PasswordHashParams(time_cost - 1, memory, 1))


# -- the production floor -----------------------------------------------------------


def test_prod_refuses_weak_parameters_with_a_fix() -> None:
    # m=64 MiB with t=1 still dominates the OWASP m=46 MiB/t=1 minimum.
    assert _settings(**_PROD_ENV, PASSWORD_HASH_TIME_COST=1).is_prod
    with pytest.raises(ValidationError, match="below the OWASP") as exc:
        _settings(
            **_PROD_ENV,
            PASSWORD_HASH_PROFILE="owasp_min",
            PASSWORD_HASH_TIME_COST=1,
        )
    assert 'SANDLOOM_ACCEPTED_RISKS=["weak_password_hash"]' in str(exc.value)


def test_prod_accepts_presets_and_the_explicit_opt_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sandloom.core.app_factory import warn_weak_password_hashing

    log = logging.getLogger("sandloom.test.argon2")
    assert _settings(**_PROD_ENV, PASSWORD_HASH_PROFILE="owasp_min").is_prod
    strong = _settings(**_PROD_ENV)
    assert warn_weak_password_hashing(strong, log) is False

    weak = _settings(
        **{
            **_PROD_ENV,
            "SANDLOOM_ACCEPTED_RISKS": "fake_psp_in_prod,weak_password_hash",
        },
        PASSWORD_HASH_PROFILE="owasp_min",
        PASSWORD_HASH_TIME_COST=1,
    )
    with caplog.at_level(logging.WARNING, logger="sandloom.test.argon2"):
        assert warn_weak_password_hashing(weak, log) is True
    assert "SECURITY" in caplog.text
    assert "the risk weak_password_hash is accepted" in caplog.text


def test_weak_parameters_are_allowed_outside_prod_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sandloom.core.app_factory import warn_weak_password_hashing

    cfg = _settings(
        PASSWORD_HASH_PROFILE="custom",
        PASSWORD_HASH_TIME_COST=1,
        PASSWORD_HASH_MEMORY_KIB=8,
        PASSWORD_HASH_PARALLELISM=1,
    )
    log = logging.getLogger("sandloom.test.argon2")
    with caplog.at_level(logging.WARNING, logger="sandloom.test.argon2"):
        assert warn_weak_password_hashing(cfg, log) is True
    assert "below the OWASP minimum" in caplog.text


# -- the configured hasher ----------------------------------------------------------


def test_configured_hasher_follows_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hashing.settings, "password_hash_profile", "owasp_min")
    hasher = di.password_hasher()
    assert (hasher.time_cost, hasher.memory_cost, hasher.parallelism) == (2, 19456, 1)
    new = hasher.hash("x")
    assert new.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    # Old (strong) hashes keep verifying under the new configuration.
    assert hashing.verify_password("x", STRONG_HASH_OF_X, hasher=hasher)
    assert hashing.password_needs_rehash(STRONG_HASH_OF_X, hasher=hasher)


# -- rehash helper ------------------------------------------------------------------


async def test_rehash_upgrade_downgrade_and_noop() -> None:
    stronger = PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)
    old = CHEAP.hash("pw")
    upgraded = await di.rehash_password_if_needed("pw", old, hasher=stronger)
    assert upgraded is not None and "$m=16,t=2,p=1$" in upgraded
    assert stronger.verify(upgraded, "pw")
    downgraded = await di.rehash_password_if_needed("pw", upgraded, hasher=CHEAP)
    assert downgraded is not None and "$m=8,t=1,p=1$" in downgraded
    assert await di.rehash_password_if_needed("pw", downgraded, hasher=CHEAP) is None


class _BrokenHasher(PasswordHasher):
    def hash(self, password: str | bytes, *, salt: bytes | None = None) -> str:
        raise RuntimeError("boom")


async def test_rehash_failure_never_raises_and_is_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def failed() -> float:
        return float(
            hashing.password_rehash_total.labels(outcome="failed")._value.get()
        )

    before = failed()
    broken = _BrokenHasher(time_cost=2, memory_cost=16, parallelism=1)
    with caplog.at_level(logging.WARNING, logger="sandloom.core.auth.hashing"):
        assert (
            await di.rehash_password_if_needed(
                "s3cret-pw", CHEAP.hash("s3cret-pw"), hasher=broken
            )
            is None
        )
    assert failed() == before + 1
    assert "password rehash failed" in caplog.text
    assert "s3cret-pw" not in caplog.text
