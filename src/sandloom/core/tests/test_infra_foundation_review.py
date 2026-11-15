"""The review findings on the infra-drivers foundation (PR #190, idk-foundationb):
the contract major read from every version form, a driver's features capped by
its declaration, kernel-only capabilities refused to modules, removed feature
names pointing at their replacement, health details without free text, SMTP
errors split into permanent and transient, and the exports the slices build on."""

from __future__ import annotations

import smtplib
import typing
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core.descriptors import CapabilityRequirement, HealthResult
from sandloom.core.infra.active import use_infra
from sandloom.core.infra.capabilities import (
    ATOMIC_STORE_CAPABILITY,
    BROADCAST_CAPABILITY,
    BUILTIN_CAPABILITIES,
    LEASE_CAPABILITY,
    NOTIFICATION_CAPABILITY,
    CapabilityRef,
)
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import (
    InvalidArgument,
    PermissionDenied,
    Unavailable,
)
from sandloom.core.infra.ports._base import CapabilitySpec, InfraProvider
from sandloom.core.infra.ports.notification import Notification, NotifierProvider
from sandloom.core.infra.registry import (
    DriverLoadError,
    FeatureUnsupportedError,
    InfraRegistry,
    KernelOnlyCapabilityRefused,
)

# -- S1: the contract major from every version form ---------------------------


class _Base(InfraProvider):
    capability = "probe"


def _implemented(contract: type, version: int) -> type[InfraProvider]:
    """A driver class implementing every abstract method of ``contract``."""

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    body: dict[str, Any] = dict.fromkeys(
        getattr(contract, "__abstractmethods__", ()), _noop
    )
    body["contract_version"] = version
    return typing.cast("type[InfraProvider]", type("Probe", (contract,), body))


@pytest.mark.parametrize("version", ["v2", "2.0", "2", "V2", "v2.1.0", " v2 "])
def test_every_version_form_gives_the_major(version: str) -> None:
    spec = CapabilitySpec(name="probe", version=version, provider_contract=_Base)
    assert spec.major == 2
    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(spec)
    with pytest.raises(DriverLoadError, match="written for contract_version 1"):
        registry.register_driver("probe", "old", _implemented(_Base, 1))
    registry.register_driver("probe", "current", _implemented(_Base, 2))


def test_a_capability_without_a_readable_major_loads_no_driver() -> None:
    spec = CapabilitySpec(name="probe", version="beta", provider_contract=_Base)
    assert spec.major is None
    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(spec)
    with pytest.raises(DriverLoadError, match="no integer major") as refused:
        registry.register_driver("probe", "any", _implemented(_Base, 7))
    assert refused.value.sandloom_code == "SANDLOOM-B022"


# -- S2: features_for never claims an undeclared feature -----------------------


class _Notifier(NotifierProvider):
    contract_version = 2
    supported_features = frozenset({"email"})

    async def send(self, notification: Notification) -> None:
        return None


class _Overclaims(_Notifier):
    @classmethod
    def features_for(cls, options: Any) -> frozenset[str]:
        return frozenset({"email", "sms"})


def _notifier_registry(driver: type[InfraProvider]) -> InfraRegistry:
    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(NOTIFICATION_CAPABILITY)
    registry.register_driver("notification", "x", driver)
    registry.set_config(
        {"notification": {"default": InstanceConfig("notification", "default", "x")}}
    )
    return registry


def test_an_undeclared_feature_from_features_for_is_not_advertised() -> None:
    registry = _notifier_registry(_Overclaims)
    assert registry.declared_features("notification", "default") == frozenset({"email"})
    with pytest.raises(FeatureUnsupportedError, match="sms"):
        registry.validate_requirement("notification", features=frozenset({"sms"}))
    provider = registry.provider("notification")
    assert provider.features() == {"email"}
    with pytest.raises(FeatureUnsupportedError, match="sms"):
        registry.resolve("notification", features=frozenset({"sms"}))


# -- S4: kernel-only capabilities are refused to modules -----------------------

_KERNEL_ONLY = (ATOMIC_STORE_CAPABILITY, LEASE_CAPABILITY, BROADCAST_CAPABILITY)


def test_the_kernel_facing_capabilities_are_kernel_only() -> None:
    kernel_only = {spec.name for spec in BUILTIN_CAPABILITIES if spec.kernel_only}
    assert kernel_only == {"atomic_store", "lease", "broadcast"}


def _kernel_registry(spec: CapabilitySpec) -> InfraRegistry:
    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(spec)
    registry.register_driver(
        spec.name, "probe", _implemented(spec.provider_contract, spec.major or 1)
    )
    registry.set_config(
        {spec.name: {"default": InstanceConfig(spec.name, "default", "probe")}}
    )
    return registry


@pytest.mark.parametrize("spec", _KERNEL_ONLY, ids=lambda s: s.name)
def test_a_module_resolving_a_kernel_only_capability_is_refused(
    spec: CapabilitySpec,
) -> None:
    from sandloom.core.facade import CoreFacade
    from sandloom.sdk.infra import capability

    registry = _kernel_registry(spec)
    with use_infra(registry), pytest.raises(KernelOnlyCapabilityRefused) as refused:
        capability(CapabilityRef(spec.name))
    assert refused.value.sandloom_code == "SANDLOOM-B041"
    assert spec.name in str(refused.value)

    core = CoreFacade(app=FastAPI())
    core.set_infra(registry)
    with pytest.raises(KernelOnlyCapabilityRefused):
        core.runtime_view().require_capability(spec.name)
    with pytest.raises(KernelOnlyCapabilityRefused):
        core.capability(spec.name)
    # The kernel's own door still reaches it.
    assert registry.provider(spec.name).name == "default"


@pytest.mark.parametrize("required", [True, False])
def test_a_module_requirement_on_a_kernel_only_capability_is_refused(
    required: bool,
) -> None:
    registry = _kernel_registry(ATOMIC_STORE_CAPABILITY)
    requirement = CapabilityRequirement("atomic_store", required=required)
    with pytest.raises(KernelOnlyCapabilityRefused, match="'billing'") as refused:
        registry.check_module_requirements("billing", [requirement])
    assert refused.value.sandloom_code == "SANDLOOM-B041"
    with pytest.raises(KernelOnlyCapabilityRefused):
        registry.validate_requirement("atomic_store")


def test_the_kernel_only_code_is_explained() -> None:
    from sandloom.core.error_codes import EXCEPTION_CODES, get

    assert get("SANDLOOM-B041").slug == "kernel-capability-refused"
    assert EXCEPTION_CODES["KernelOnlyCapabilityRefused"] == "SANDLOOM-B041"


# -- C1: a removed feature name points at its replacement ----------------------


def _renamed_spec() -> CapabilitySpec:
    return CapabilitySpec(
        name="probe",
        version="v1",
        provider_contract=_Base,
        feature_kinds={"durable_delivery": "guarantee"},
        removed_features={"durable": "durable_delivery"},
    )


def test_a_removed_feature_is_refused_naming_its_replacement() -> None:
    spec = _renamed_spec()
    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(spec)
    with pytest.raises(FeatureUnsupportedError, match="durable_delivery"):
        registry.register_driver(
            "probe",
            "factory",
            lambda cfg: _implemented(_Base, 1)(
                name=cfg.name, driver=cfg.driver, options=cfg.options
            ),
            features=frozenset({"durable"}),
        )
    driver = _implemented(_Base, 1)
    driver.supported_features = frozenset({"durable_delivery"})
    registry.register_driver("probe", "x", driver)
    registry.set_config({"probe": {"default": InstanceConfig("probe", "default", "x")}})
    for refuse in (
        lambda: registry.validate_requirement("probe", features=frozenset({"durable"})),
        lambda: registry.resolve("probe", features=frozenset({"durable"})),
    ):
        with pytest.raises(FeatureUnsupportedError, match="renamed durable_delivery"):
            refuse()


def test_a_removed_feature_is_no_feature_of_the_spec() -> None:
    with pytest.raises(ValueError, match="durable"):
        CapabilitySpec(
            name="probe",
            version="v1",
            provider_contract=_Base,
            feature_kinds={"durable": "guarantee"},
            removed_features={"durable": "durable_delivery"},
        )


# -- S3: health details carry no free text -------------------------------------


class _Chatty(_Notifier):
    async def health(self) -> HealthResult:
        return HealthResult(
            status="fail",
            details={
                "error": "redis://user:s3cret-marker@host:6379/0 refused",
                "kind": "ConnectionError",
                "latency_ms": 12,
                "nested": {"why": "token s3cret-marker"},
            },
        )


@pytest.mark.asyncio
async def test_the_registry_drops_free_text_from_driver_health_details() -> None:
    registry = _notifier_registry(_Chatty)
    result = (await registry.health_all())["notification:default"]
    assert result.status == "fail"
    assert "s3cret-marker" not in repr(result)
    assert result.details is not None
    assert result.details["kind"] == "ConnectionError"
    assert result.details["latency_ms"] == 12
    assert result.details["error"] == "<redacted>"


class _ChattyKeys(_Notifier):
    async def health(self) -> HealthResult:
        return HealthResult(
            status="fail",
            details={
                "smtp://user:s3cret-marker@mail.test:25": False,
                "error": "ConnectionError",
                "nested": {"redis://u:s3cret-marker@h:6379": 1, "ok": True},
            },
        )


@pytest.mark.asyncio
async def test_the_registry_redacts_a_health_detail_keyed_by_free_text() -> None:
    registry = _notifier_registry(_ChattyKeys)
    result = (await registry.health_all())["notification:default"]
    assert "s3cret-marker" not in repr(result)
    assert result.details == {
        "<redacted>": "<redacted>",
        "error": "ConnectionError",
        "nested": {"<redacted>": "<redacted>", "ok": True},
    }


@pytest.mark.asyncio
async def test_the_redis_health_names_the_error_type() -> None:
    from sandloom.core.drivers.redis_cache import create_redis_cache

    provider = create_redis_cache(
        InstanceConfig(
            "cache",
            "probe",
            "redis",
            {"url": "redis://user:s3cret-marker@127.0.0.1:1/0"},
        )
    )
    try:
        result = await provider.health()
    finally:
        await provider.aclose()
    assert result.status == "fail"
    assert result.details is not None
    assert str(result.details["error"]).isidentifier()


@pytest.mark.asyncio
async def test_the_database_health_names_the_error_type() -> None:
    from sandloom.core.drivers.sqlalchemy_database import create_sqlalchemy_database

    provider = create_sqlalchemy_database(
        InstanceConfig(
            "database",
            "probe",
            "sqlalchemy",
            {"url": "postgresql+asyncpg://u:s3cret-marker@127.0.0.1:1/db"},
        )
    )
    try:
        result = await provider.health()
    finally:
        await provider.aclose()
    assert result.status == "fail"
    assert result.details is not None
    assert str(result.details["error"]).isidentifier()


async def _leaks() -> HealthResult:
    raise ConnectionError("redis://user:s3cret-marker@host:6379/0 refused")


@pytest.mark.asyncio
async def test_a_module_health_failure_names_the_error_type() -> None:
    from sandloom.core.lifecycle import aggregate_health

    core = SimpleNamespace(health_checks={"x": _leaks}, module_descriptors=[])
    result = (await aggregate_health(typing.cast("Any", core)))["x"]
    assert result.details == {"error": "ConnectionError"}


@pytest.mark.asyncio
async def test_a_capability_health_failure_names_the_error_type() -> None:
    from sandloom.core.app_factory.lifecycle import _capability_health

    class _Core:
        def capability(self, capability: str, name: str) -> Any:
            raise ConnectionError("redis://user:s3cret-marker@host:6379/0 refused")

    result = await _capability_health(typing.cast("Any", _Core()), "cache")
    assert result.details == {"error": "ConnectionError"}


# -- S5: permanent SMTP refusals are not retried --------------------------------


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (smtplib.SMTPSenderRefused(550, b"no", "a@x"), InvalidArgument),
        (smtplib.SMTPSenderRefused(451, b"later", "a@x"), Unavailable),
        (smtplib.SMTPDataError(554, b"rejected"), InvalidArgument),
        (smtplib.SMTPDataError(452, b"full"), Unavailable),
        (smtplib.SMTPRecipientsRefused({"b@x": (550, b"no")}), InvalidArgument),
        (smtplib.SMTPRecipientsRefused({"b@x": (450, b"busy")}), Unavailable),
        (
            smtplib.SMTPRecipientsRefused({"b@x": (450, b"busy"), "c@x": (550, b"")}),
            InvalidArgument,
        ),
        (smtplib.SMTPAuthenticationError(535, b"bad"), PermissionDenied),
        (smtplib.SMTPAuthenticationError(454, b"later"), Unavailable),
        (smtplib.SMTPServerDisconnected("gone"), Unavailable),
    ],
)
def test_smtp_errors_split_into_permanent_and_transient(
    exc: Exception, error: type[Exception]
) -> None:
    from sandloom.core.drivers.smtp_notifier import _delivery_error

    mapped = _delivery_error(exc, "default")
    assert type(mapped) is error


# -- C3, C4, C5: where the contracts live and what the driver kit exports ---------


def test_the_database_spec_lives_in_its_port() -> None:
    from sandloom.core.infra import capabilities, contracts
    from sandloom.core.infra.ports import database

    assert contracts.DatabaseProvider is database.DatabaseProvider
    assert database.DATABASE_CAPABILITY.provider_contract is database.DatabaseProvider

    assert capabilities.DATABASE_CAPABILITY is database.DATABASE_CAPABILITY
    assert capabilities.DATABASE_RENDERER_FEATURES is (
        database.DATABASE_RENDERER_FEATURES
    )


def test_the_driver_kit_exports_the_kernel_facing_contracts() -> None:
    import sandloom.driverkit as kit
    from sandloom.core.infra.ports import atomic_store, broadcast, lease

    for name, port in (
        ("AtomicStoreProvider", atomic_store),
        ("LeaseProvider", lease),
        ("Lease", lease),
        ("LeaseHolder", lease),
        ("BroadcastProvider", broadcast),
        ("BroadcastMessage", broadcast),
        ("Subscription", broadcast),
    ):
        assert getattr(kit, name) is getattr(port, name)
        assert name in kit.__all__


@pytest.mark.asyncio
async def test_object_content_takes_no_unsized_reader() -> None:
    from sandloom.core.infra.ports import objectstore
    from sandloom.core.infra.ports.objectstore import (
        ObjectReader,
        iter_object_chunks,
    )

    assert not hasattr(objectstore, "_UnsizedObjectReader")
    assert set(typing.get_args(objectstore.ObjectContent)) == {bytes, ObjectReader}

    class Unsized:
        async def read(self) -> bytes:
            return b"x"

    with pytest.raises(TypeError):
        async for _chunk in iter_object_chunks(typing.cast("Any", Unsized()), 4):
            pass


def test_the_memoised_database_features_follow_a_replaced_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.drivers.sql_dialects import SqliteSql
    from sandloom.core.drivers.sqlalchemy_database import SQLAlchemyDatabaseProvider
    from sandloom.core.sql_alchemy import portable

    class Older(SqliteSql):
        def features(self) -> frozenset[str]:
            return super().features() - {"list_aggregates"}

    options = {"url": "sqlite+aiosqlite:///never-opened-memo.db"}
    assert "list_aggregates" in SQLAlchemyDatabaseProvider.features_for(options)
    older = Older()
    monkeypatch.setattr(portable, "sql_dialect", lambda _name: older)
    assert "list_aggregates" not in SQLAlchemyDatabaseProvider.features_for(options)
    assert "list_aggregates" not in SQLAlchemyDatabaseProvider.features_for(options)
    monkeypatch.undo()
    assert "list_aggregates" in SQLAlchemyDatabaseProvider.features_for(options)


def test_health_failure_details_name_the_missing_module_and_nothing_else() -> None:
    from sandloom.core.infra.ports._base import health_failure_details

    missing = ModuleNotFoundError("No module named 'redis'", name="redis")
    assert health_failure_details(missing) == {
        "error": "ModuleNotFoundError",
        "missing_module": "redis",
    }
    leaky = ConnectionError("redis://user:secret@host:6379/0 refused")
    assert health_failure_details(leaky) == {"error": "ConnectionError"}
    odd = ImportError("x", name="has space/and slash")
    assert health_failure_details(odd) == {"error": "ImportError"}
