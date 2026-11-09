"""Resilience + fail-fast config validation (Wave 9)."""

from __future__ import annotations

import pytest

from sandloom.core.config.settings import Settings
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.registry import InfraError, InfraRegistry
from sandloom.sdk.testing import validate_settings
from sandloom_billing.config import BillingSettings


def _set_base_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Populate a minimal valid environment, then apply overrides."""
    env = {
        "ENVIRONMENT": "dev",
        "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/db",
        "CACHE_URL": "redis://h:6379/0",
        "BROKER_URL": "redis://h:6379/1",
        "JWT_SECRET": "x" * 40,
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_stripe_provider_requires_keys() -> None:
    """G-BILLING-18: billing's own settings refuse Stripe without its keys
    (the kernel ``Settings`` no longer validate billing: B-C4)."""
    with pytest.raises(SystemExit, match="STRIPE_SECRET_KEY"):
        validate_settings(
            BillingSettings, {"BILLING_PROVIDER": "stripe"}, environment="dev"
        )
    with pytest.raises(SystemExit, match="STRIPE_WEBHOOK_SECRET") as only_one:
        validate_settings(
            BillingSettings,
            {"BILLING_PROVIDER": "stripe", "STRIPE_SECRET_KEY": "sk_test_x"},
            environment="dev",
        )
    assert "sk_test_x" not in str(only_one.value)


def test_stripe_provider_ok_with_keys() -> None:
    cfg = validate_settings(
        BillingSettings,
        {
            "BILLING_PROVIDER": "stripe",
            "STRIPE_SECRET_KEY": "sk_test_x",
            "STRIPE_WEBHOOK_SECRET": "whsec_x",
        },
        environment="prod",
    )
    assert cfg.billing_provider == "stripe"
    assert cfg.stripe_secret_key is not None
    assert "sk_test_x" not in repr(cfg)  # a SecretStr


def test_fake_provider_needs_no_keys() -> None:
    cfg = validate_settings(
        BillingSettings, {"BILLING_PROVIDER": "fake"}, environment="dev"
    )
    assert cfg.billing_provider == "fake"


def test_the_kernel_settings_do_not_hold_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-C1 / B-C4: a process without billing neither parses nor validates its
    variables."""
    _set_base_env(monkeypatch, BILLING_PROVIDER="stripe")
    assert not hasattr(Settings(_env_file=None), "billing_provider")


def test_db_pool_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_base_env(monkeypatch, DB_POOL_SIZE="20", DB_MAX_OVERFLOW="40")
    s = Settings(_env_file=None)
    assert s.db_pool_size == 20
    assert s.db_max_overflow == 40
    assert s.db_pool_timeout >= 1


def test_registry_validate_rejects_unknown_driver() -> None:
    reg = InfraRegistry(
        config={
            "cache": {
                "default": InstanceConfig(
                    capability="cache", name="default", driver="ghost", options={}
                )
            }
        }
    )
    with pytest.raises(InfraError, match="no registered driver"):
        reg.validate()


def test_build_infra_registry_validates() -> None:
    from sandloom.core.drivers.bootstrap import build_infra_registry
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    with pytest.raises(InfraError):
        build_infra_registry(
            InMemorySchemaRegistry(),
            config={
                "cache": {
                    "default": InstanceConfig(
                        capability="cache",
                        name="default",
                        driver="nonsense",
                        options={},
                    )
                }
            },
            load_plugins=False,
        )


def test_build_infra_registry_rejects_unknown_infra_option() -> None:
    """A typo'd INFRA option key fails startup validation (config_schema).

    Regression: the built-in drivers now carry a ``config_schema`` with
    ``extra="forbid"``, so a misspelled connection key (``ur1`` for ``url``)
    is rejected at boot via :meth:`InfraRegistry.validate` instead of being
    silently accepted and falling back to the flat default.
    """
    from sandloom.core.drivers.bootstrap import build_infra_registry
    from sandloom.core.infra.registry import ConfigSchemaError
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    with pytest.raises(ConfigSchemaError) as excinfo:
        build_infra_registry(
            InMemorySchemaRegistry(),
            config={
                "cache": {
                    "sessions": InstanceConfig(
                        capability="cache",
                        name="sessions",
                        driver="redis",
                        options={"ur1": "redis://h:6379/3"},  # typo: should be 'url'
                    )
                }
            },
            load_plugins=False,
        )
    # The actionable fix hint points at the offending INFRA path.
    assert excinfo.value.fix is not None
    assert "cache" in str(excinfo.value)


def test_build_infra_registry_accepts_valid_infra_option() -> None:
    """The correctly-spelled connection option passes validation (no false positive)."""
    from sandloom.core.drivers.bootstrap import build_infra_registry
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    # Build succeeds: validate() runs inside build_infra_registry and does not raise.
    build_infra_registry(
        InMemorySchemaRegistry(),
        config={
            "cache": {
                "sessions": InstanceConfig(
                    capability="cache",
                    name="sessions",
                    driver="redis",
                    options={"url": "redis://h:6379/3"},
                )
            }
        },
        load_plugins=False,
    )


def test_sso_clients_have_timeout() -> None:
    from sandloom_identity.integrations import _SSO_HTTP_TIMEOUT

    # A real timeout is configured (not None on any phase).
    assert _SSO_HTTP_TIMEOUT.read is not None
    assert _SSO_HTTP_TIMEOUT.connect is not None
