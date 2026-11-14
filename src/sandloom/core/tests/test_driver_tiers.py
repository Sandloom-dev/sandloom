"""Driver tiers (spec 2.5): the registration carries one, production reads it."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

import sandloom.core.descriptors.module_names as module_names
from sandloom.core.cli.doctor import Report, check_infra
from sandloom.core.descriptors import HealthResult
from sandloom.core.drivers.bootstrap import (
    BUILTIN_DRIVER_ROWS,
    BUILTIN_DRIVER_TIERS,
    build_infra_registry,
    register_builtin_drivers,
)
from sandloom.core.infra.capabilities import NOTIFICATION_CAPABILITY
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import Notification, NotifierProvider
from sandloom.core.infra.registry import (
    DevelopmentOnlyDriverRefused,
    DriverLoadError,
    ExperimentalCapabilityRefused,
    ExperimentalDriverRefused,
    InfraRegistry,
    load_selected_plugins,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry


class _Notifier(NotifierProvider):
    contract_version = 2
    supported_features = frozenset({"email"})

    async def send(self, notification: Notification) -> None:
        return None

    async def isolation_identity(self) -> str | None:
        return f"{self.driver}:shared"


class _Beta(_Notifier):
    tier = "beta"


class _Stable(_Notifier):
    tier = "stable"


class _Unknown(_Notifier):
    tier = "gold"  # type: ignore[assignment]


def _strict() -> InfraRegistry:
    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(NOTIFICATION_CAPABILITY)
    return registry


def _prod(*risks: str) -> Any:
    return SimpleNamespace(
        is_prod=True, accepts_risk=lambda risk: risk in risks, infra={}
    )


def _config(driver: str) -> dict[str, dict[str, InstanceConfig]]:
    return {
        "notification": {
            "default": InstanceConfig("notification", "default", driver, {})
        }
    }


def _install(monkeypatch: pytest.MonkeyPatch, drivers: dict[str, Any]) -> None:
    """Entry points of the group ``sandloom.drivers``, one per ``name: class``."""

    def entry(name: str, target: Any) -> SimpleNamespace:
        return SimpleNamespace(
            name=name,
            value="pkg.driver:X",
            dist=SimpleNamespace(name="pkg"),
            load=lambda: target,
        )

    found = [entry(name, target) for name, target in drivers.items()]

    class _Entries:
        def select(self, *, group: str, name: str | None = None) -> list[Any]:
            if group != "sandloom.drivers":
                return []
            return [e for e in found if name is None or e.name == name]

    monkeypatch.setattr(
        "sandloom.core.infra.registry.metadata.entry_points", lambda: _Entries()
    )
    monkeypatch.setattr(module_names, "_cache", None)


def _plugin_registry(
    monkeypatch: pytest.MonkeyPatch, **drivers: type[_Notifier]
) -> tuple[InfraRegistry, dict[str, dict[str, InstanceConfig]]]:
    _install(monkeypatch, {f"notification.{n}": c for n, c in drivers.items()})
    (name,) = drivers
    registry = _strict()
    config = _config(name)
    load_selected_plugins(registry, config)
    return registry, config


# ------------------------------------------------------------ the registration
def test_a_registration_without_a_tier_is_experimental() -> None:
    registry = _strict()
    registry.register_driver("notification", "x", _Notifier)
    assert registry.driver_tier("notification", "x") == "experimental"
    assert registry.driver_tier("notification", "absent") is None


def test_register_driver_takes_the_tier_it_is_given() -> None:
    registry = _strict()
    registry.register_driver("notification", "x", _Notifier, tier="beta")
    registry.register_driver("notification", "y", "pkg.mod:Cls", tier="stable")
    assert registry.driver_tier("notification", "x") == "beta"
    # Read from the registration: a lazy target is not imported to answer.
    assert registry.driver_tier("notification", "y") == "stable"


def test_register_driver_refuses_an_unknown_tier() -> None:
    with pytest.raises(ValueError, match="tier="):
        _strict().register_driver(
            "notification",
            "x",
            _Notifier,
            tier="gold",  # type: ignore[arg-type]
        )


# ------------------------------------------------------------------- built-ins
def test_every_builtin_row_has_a_tier_and_no_tier_is_orphaned() -> None:
    rows = {(row[0], row[1]) for row in BUILTIN_DRIVER_ROWS}
    assert set(BUILTIN_DRIVER_TIERS) == rows


def test_the_builtin_drivers_are_at_least_beta_and_smtp_waits_for_its_lane() -> None:
    registry = InfraRegistry(strict_capabilities=True)
    register_builtin_drivers(registry, InMemorySchemaRegistry())
    tiers = {
        f"{c}/{d}": registry.driver_tier(c, d) for c, d, *_rest in BUILTIN_DRIVER_ROWS
    }
    # smtp is beta once .github/workflows/notification-smtp.yml is green on main.
    assert tiers.pop("notification/smtp") == "experimental"
    assert set(tiers.values()) <= {"beta", "stable"}


# --------------------------------------------------------------- entry points
def test_an_entry_point_driver_without_a_tier_is_experimental(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _ = _plugin_registry(monkeypatch, plain=_Notifier)
    assert registry.driver_tier("notification", "plain") == "experimental"


def test_the_registry_copies_the_class_tier_into_the_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _ = _plugin_registry(monkeypatch, tried=_Beta)
    assert registry.driver_tier("notification", "tried") == "beta"
    registry, _ = _plugin_registry(monkeypatch, steady=_Stable)
    assert registry.driver_tier("notification", "steady") == "stable"


def test_an_entry_point_driver_with_an_unknown_tier_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(DriverLoadError, match="tier"):
        _plugin_registry(monkeypatch, odd=_Unknown)


# ----------------------------------------------------------------- production
def test_an_experimental_driver_is_refused_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, {"notification.plain": _Notifier})
    with pytest.raises(ExperimentalDriverRefused, match="notification/default") as exc:
        build_infra_registry(
            InMemorySchemaRegistry(), settings=_prod(), config=_config("plain")
        )
    assert exc.value.sandloom_code == "SANDLOOM-B023"
    assert isinstance(exc.value, ExperimentalCapabilityRefused)
    assert "experimental_driver" in str(exc.value.fix)


def test_the_risk_experimental_driver_lets_it_start_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install(monkeypatch, {"notification.plain": _Notifier})
    with caplog.at_level(logging.WARNING, logger="sandloom.core.infra"):
        build_infra_registry(
            InMemorySchemaRegistry(),
            settings=_prod("experimental_driver"),
            config=_config("plain"),
        )
    assert "notification/default (plain) experimental" in caplog.text


def test_the_capability_risk_does_not_accept_an_experimental_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, {"notification.plain": _Notifier})
    with pytest.raises(ExperimentalDriverRefused):
        build_infra_registry(
            InMemorySchemaRegistry(),
            settings=_prod("experimental_capability"),
            config=_config("plain"),
        )


def test_a_beta_driver_logs_one_startup_warning_in_production(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install(monkeypatch, {"notification.tried": _Beta})
    with caplog.at_level(logging.WARNING, logger="sandloom.core.infra"):
        build_infra_registry(
            InMemorySchemaRegistry(), settings=_prod(), config=_config("tried")
        )
    warnings = [r for r in caplog.records if "below stable" in r.getMessage()]
    assert len(warnings) == 1
    assert "notification/default (tried) beta" in warnings[0].getMessage()


def test_a_stable_driver_is_silent_in_production(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install(monkeypatch, {"notification.steady": _Stable})
    with caplog.at_level(logging.WARNING, logger="sandloom.core.infra"):
        build_infra_registry(
            InMemorySchemaRegistry(), settings=_prod(), config=_config("steady")
        )
    assert "below stable" not in caplog.text


def test_outside_production_an_experimental_driver_starts_silently(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install(monkeypatch, {"notification.plain": _Notifier})
    settings: Any = SimpleNamespace(
        is_prod=False, accepts_risk=lambda _risk: False, infra={}
    )
    with caplog.at_level(logging.WARNING, logger="sandloom.core.infra"):
        build_infra_registry(
            InMemorySchemaRegistry(), settings=settings, config=_config("plain")
        )
    assert "below stable" not in caplog.text


def test_smtp_selected_in_production_needs_the_risk_until_its_lane_is_green() -> None:
    config = {
        "notification": {
            "default": InstanceConfig(
                "notification", "default", "smtp", {"host": "mail.example.com"}
            )
        }
    }
    with pytest.raises(ExperimentalDriverRefused, match="notification/default"):
        build_infra_registry(InMemorySchemaRegistry(), settings=_prod(), config=config)
    build_infra_registry(
        InMemorySchemaRegistry(), settings=_prod("experimental_driver"), config=config
    )


# ---------------------------------------------------------------- diagnostics
def test_describe_names_the_tier_of_each_driver_and_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, config = _plugin_registry(monkeypatch, tried=_Beta)
    registry.set_config(config)
    described = registry.describe()
    assert described["driver_tiers"]["notification/tried"] == "beta"
    assert described["instances"]["notification"]["default"]["tier"] == "beta"


def test_doctor_shows_the_tier_of_an_instance() -> None:
    class _Infra:
        async def health_all(self) -> dict[str, HealthResult]:
            return {"notification:default": HealthResult(status="ok", details={})}

        def describe(self) -> dict[str, Any]:
            return {
                "instances": {
                    "notification": {"default": {"driver": "smtp", "tier": "beta"}}
                }
            }

    report = Report()
    asyncio.run(check_infra(report, SimpleNamespace(infra=_Infra()), timeout=5))
    (check,) = report.checks
    assert check.message == "driver=smtp tier=beta"
    assert check.details["tier"] == "beta"


# ------------------------------------------------- the tier is checked at runtime
class _DevStable(_Notifier):
    development_only = True
    tier = "stable"


class _DevExperimental(_Notifier):
    development_only = True


def _runtime_prod(monkeypatch: pytest.MonkeyPatch, *risks: str) -> None:
    """The settings ``provider()`` and ``validate()`` read are the registry's own."""
    monkeypatch.setattr(
        "sandloom.core.infra.registry.settings",
        SimpleNamespace(
            is_prod=True,
            accepts_risk=lambda risk: risk in risks,
            health_check_timeout_seconds=5,
        ),
    )


def test_provider_refuses_an_experimental_driver_configured_after_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _strict()
    registry.register_driver("notification", "tried", _Beta, tier="beta")
    registry.set_config(_config("tried"))
    registry.validate()
    _runtime_prod(monkeypatch)
    assert isinstance(registry.provider("notification"), _Beta)
    # Registered and configured once the boot check has run.
    registry.register_driver("notification", "late", _Notifier)
    registry.configure_instance(InstanceConfig("notification", "late", "late", {}))
    with pytest.raises(ExperimentalDriverRefused, match="notification/late") as exc:
        registry.provider("notification", "late")
    assert exc.value.sandloom_code == "SANDLOOM-B023"
    assert "experimental_driver" in str(exc.value.fix)


def test_provider_lets_an_experimental_driver_run_with_the_risk_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _strict()
    registry.register_driver("notification", "late", _Notifier)
    registry.set_config(_config("late"))
    _runtime_prod(monkeypatch, "experimental_driver")
    assert isinstance(registry.provider("notification"), _Notifier)


def test_provider_runs_an_experimental_driver_outside_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _strict()
    registry.register_driver("notification", "late", _Notifier)
    registry.set_config(_config("late"))
    monkeypatch.setattr(
        "sandloom.core.infra.registry.settings",
        SimpleNamespace(is_prod=False, accepts_risk=lambda _risk: False),
    )
    assert isinstance(registry.provider("notification"), _Notifier)


# ------------------------------------------------- development_only comes first
def test_a_development_only_driver_is_refused_whatever_its_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, {"notification.dev": _DevStable})
    _runtime_prod(monkeypatch, "experimental_driver")
    with pytest.raises(DevelopmentOnlyDriverRefused) as exc:
        build_infra_registry(
            InMemorySchemaRegistry(),
            settings=_prod("experimental_driver"),
            config=_config("dev"),
        )
    assert exc.value.sandloom_code == "SANDLOOM-B022"


def test_a_development_only_experimental_driver_gets_b022_not_b023(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, {"notification.dev": _DevExperimental})
    _runtime_prod(monkeypatch)
    with pytest.raises(DevelopmentOnlyDriverRefused) as exc:
        build_infra_registry(
            InMemorySchemaRegistry(), settings=_prod(), config=_config("dev")
        )
    assert exc.value.sandloom_code == "SANDLOOM-B022"


@pytest.mark.xfail(
    strict=True,
    reason="idk-tiersb: _check_driver_tiers_in_production (core/drivers/bootstrap.py) "
    "does not skip development_only drivers yet; that file was claimed by "
    "idk-lease. Drop this marker with the one-line skip.",
)
def test_a_synthesized_default_with_a_development_only_driver_gets_b022_not_b023(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # validate() skips an implicit instance's B022, so the tier check must not
    # answer first with B023.
    _install(monkeypatch, {"notification.dev": _DevExperimental})
    _runtime_prod(monkeypatch)
    config = {
        "notification": {
            "default": InstanceConfig(
                "notification", "default", "dev", {}, implicit=True
            )
        }
    }
    with pytest.raises(DevelopmentOnlyDriverRefused):
        build_infra_registry(InMemorySchemaRegistry(), settings=_prod(), config=config)


def test_provider_refuses_a_development_only_experimental_driver_with_b022(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _strict()
    registry.register_driver("notification", "dev", _DevExperimental)
    registry.set_config(_config("dev"))
    _runtime_prod(monkeypatch)
    with pytest.raises(DevelopmentOnlyDriverRefused):
        registry.provider("notification")
