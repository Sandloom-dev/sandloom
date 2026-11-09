"""Driver availability in the preflight (KB-IC-1, SANDLOOM-B037).

A built-in driver is registered whether or not its package is installed, so
the preflight used to pass ``taskqueue`` and ``cache`` with Celery and redis-py
absent. Each driver now lists the import names it needs; the preflight checks
them with ``importlib.util.find_spec`` (nothing imported, no connection) and
refuses with SANDLOOM-B037 naming the module, capability, instance, driver,
the missing feature or package and the fix. A dependency that fails to import
while the driver is built gets the same code. A synthesized default instance
nothing requires is not opened, probed or listed as failing, while the
instances the kernel itself uses (database, the revocation/security cache,
the event bus) stay required.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from sandloom.core import error_codes
from sandloom.core.config.settings import settings
from sandloom.core.descriptors import CapabilityRequirement, HealthResult
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.capabilities import CapabilitySpec
from sandloom.core.infra.config import (
    KERNEL_INSTANCES,
    InstanceConfig,
    load_infra_config,
)
from sandloom.core.infra.contracts import InfraProvider
from sandloom.core.infra.registry import (
    CapabilityUnavailable,
    DriverUnavailableError,
    FeatureUnsupportedError,
    InfraRegistry,
    InfraStartupError,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry

#: An import name no environment has.
_ABSENT = "sandloom_ic1_absent_driver_dependency"
#: An importable package whose import leaves a mark (written per test).
_PROBE = "sandloom_ic1_probe_dependency"


class _Provider(InfraProvider):
    """A provider that records what was done to it."""

    opened: list[str] = []
    probed: list[str] = []

    async def aopen(self) -> None:
        self.opened.append(f"{self.capability}/{self.name}")

    async def health(self) -> HealthResult:
        self.probed.append(f"{self.capability}/{self.name}")
        return HealthResult(status="ok")


class _Spy:
    """A driver factory that counts its builds."""

    def __init__(self, capability: str) -> None:
        self.provider_class: type[_Provider] = type(
            f"_{capability}_provider", (_Provider,), {"capability": capability}
        )
        self.builds: list[str] = []

    def __call__(self, cfg: InstanceConfig) -> InfraProvider:
        self.builds.append(cfg.name)
        return self.provider_class(
            name=cfg.name, driver=cfg.driver, options=cfg.options
        )


@pytest.fixture(autouse=True)
def _fresh_records() -> None:
    _Provider.opened = []
    _Provider.probed = []


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every socket connection attempt is recorded and refused."""
    attempts: list[Any] = []

    def refuse(self: socket.socket, address: Any) -> None:
        attempts.append(address)
        raise AssertionError(f"network I/O during a build-free check: {address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    return attempts


@pytest.fixture
def probe_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An installed package that writes a marker file when imported."""
    marker = tmp_path / "imported"
    package = tmp_path / _PROBE
    package.mkdir()
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, _PROBE, raising=False)
    return marker


def _instance(
    capability: str, driver: str, *, name: str = "default", implicit: bool = False
) -> InstanceConfig:
    return InstanceConfig(
        capability=capability, name=name, driver=driver, implicit=implicit
    )


def _registry(*instances: InstanceConfig) -> tuple[InfraRegistry, dict[str, _Spy]]:
    registry = InfraRegistry(config={i.capability: {i.name: i} for i in instances})
    spies: dict[str, _Spy] = {}
    for inst in instances:
        spies[inst.capability] = _Spy(inst.capability)
        registry.register_capability(
            CapabilitySpec(
                name=inst.capability,
                version="v1",
                provider_contract=InfraProvider,
                features=frozenset({"delay"}),
            )
        )
    return registry, spies


def _real_registry() -> InfraRegistry:
    """The registry ``create_app`` builds from the test settings."""
    return build_infra_registry(InMemorySchemaRegistry(), load_plugins=False)


# ------------------------------------------------------------- preflight ----


def test_a_missing_package_refuses_the_requirement_build_free(
    no_network: list[Any], probe_package: Path
) -> None:
    """``find_spec`` alone: a present package is not imported, an absent one
    is a B037 naming capability, instance, driver, package and both fixes."""
    registry, spies = _registry(_instance("taskqueue", "jobs"))
    registry.register_driver(
        "taskqueue",
        "jobs",
        spies["taskqueue"],
        requires=(_PROBE,),
        install="pip install 'jobs-driver'",
    )
    registry.validate_requirement("taskqueue")
    assert _PROBE not in sys.modules
    assert not probe_package.exists(), "the availability check imported the package"

    registry.register_driver(
        "taskqueue",
        "jobs",
        spies["taskqueue"],
        requires=(_PROBE, _ABSENT),
        install="pip install 'jobs-driver'",
    )
    with pytest.raises(DriverUnavailableError) as caught:
        registry.validate_requirement("taskqueue", features=None)
    exc = caught.value
    assert error_codes.code_for_exception(exc) == "SANDLOOM-B037"
    assert (exc.capability, exc.instance, exc.driver) == (
        "taskqueue",
        "default",
        "jobs",
    )
    assert exc.missing == (_ABSENT,)
    assert f"'{_ABSENT}'" in str(exc) and "'jobs'" in str(exc)
    assert "pip install 'jobs-driver'" in str(exc)
    assert "INFRA['taskqueue']['default']" in str(exc)
    assert spies["taskqueue"].builds == []
    assert no_network == []


def test_find_spec_runs_once_per_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A preflight per module costs no repeated lookups."""
    calls: list[str] = []
    real = importlib.util.find_spec

    def counting(name: str, package: str | None = None) -> Any:
        calls.append(name)
        return real(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", counting)
    registry, spies = _registry(_instance("taskqueue", "jobs"))
    registry.register_driver(
        "taskqueue", "jobs", spies["taskqueue"], requires=(_ABSENT,)
    )
    for _ in range(3):
        with pytest.raises(DriverUnavailableError):
            registry.validate_requirement("taskqueue")
    assert calls == [_ABSENT]


def test_requires_takes_top_level_names_only() -> None:
    """``find_spec("a.b")`` imports ``a``: a dotted name is refused."""
    registry, spies = _registry(_instance("cache", "x"))
    with pytest.raises(ValueError, match="top-level"):
        registry.register_driver(
            "cache", "x", spies["cache"], requires=("redis.asyncio",)
        )


@pytest.mark.parametrize(
    ("capability", "package", "hint"),
    [
        ("taskqueue", "celery", "pip install 'sandloom[celery]'"),
        ("cache", "redis", "pip install 'sandloom[redis]'"),
    ],
)
def test_builtin_drivers_name_their_package_and_extra(
    monkeypatch: pytest.MonkeyPatch,
    no_network: list[Any],
    capability: str,
    package: str,
    hint: str,
) -> None:
    """The research's false positive (1.7, check 1): with the package
    unimportable, ``validate_requirement`` passed. It refuses now, and the
    package is still not imported."""
    monkeypatch.setitem(sys.modules, package, None)
    registry = _real_registry()
    with pytest.raises(DriverUnavailableError) as caught:
        registry.validate_requirement(capability)
    assert caught.value.missing == (package,)
    assert hint in str(caught.value)
    assert sys.modules[package] is None
    assert no_network == []


def test_the_fix_names_the_features_an_alternative_must_advertise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "celery", None)
    with pytest.raises(DriverUnavailableError) as caught:
        _real_registry().validate_requirement(
            "taskqueue", features=frozenset({"delay"})
        )
    assert "taskqueue driver that advertises ['delay']" in str(caught.value)


def test_memory_and_stdlib_drivers_need_no_package() -> None:
    registry = _real_registry()
    for capability, driver in (("eventbus", "memory"), ("notification", "log")):
        registry.configure_instance(_instance(capability, driver, name="probe"))
        assert registry.missing_packages(capability, "probe") == ()


def test_a_module_gets_every_unmet_requirement_in_one_refusal(
    monkeypatch: pytest.MonkeyPatch, no_network: list[Any]
) -> None:
    """B037 names the module and, per requirement, capability, instance,
    driver, features, what is missing and the fix; optional ones are handed
    back, not raised."""
    monkeypatch.setitem(sys.modules, "celery", None)
    registry = _real_registry()
    requirements = [
        CapabilityRequirement("database"),
        CapabilityRequirement("taskqueue", features=frozenset({"delay"})),
        CapabilityRequirement("objectstore", features=frozenset({"no_such_feature"})),
        CapabilityRequirement("search", required=False),
    ]
    with pytest.raises(CapabilityUnavailable) as caught:
        registry.check_module_requirements("shop_orders", requirements)
    message = str(caught.value)
    assert error_codes.code_for_exception(caught.value) == "SANDLOOM-B037"
    assert "module 'shop_orders'" in message
    assert (
        "capability 'taskqueue' (instance 'default', driver 'celery', "
        "features ['delay'])" in message
    )
    assert "needs the package 'celery', which is not installed" in message
    assert "pip install 'sandloom[celery]'" in message
    assert "capability 'objectstore'" in message and "no_such_feature" in message
    assert "capability 'database'" not in message
    assert message.count("[SANDLOOM-B037]") == 1
    # A handler of the precise errors keeps matching the aggregated refusal.
    assert isinstance(caught.value, FeatureUnsupportedError)
    assert isinstance(caught.value, DriverUnavailableError)
    assert no_network == []

    optional = registry.check_module_requirements(
        "shop_orders", [CapabilityRequirement("search", required=False)]
    )
    assert [req.capability for req, _exc in optional] == ["search"]


def test_an_import_error_while_building_is_b037(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``find_spec`` found the driver's package; a transitive dependency is
    missing when it is built: the same code, the cause kept."""
    registry, _ = _registry(_instance("taskqueue", "jobs"))

    def broken(cfg: InstanceConfig) -> InfraProvider:
        raise ModuleNotFoundError("No module named 'kombu'", name="kombu")

    registry.register_driver(
        "taskqueue", "jobs", broken, install="pip install 'sandloom[celery]'"
    )
    with pytest.raises(DriverUnavailableError) as caught:
        registry.provider("taskqueue")
    exc = caught.value
    assert error_codes.code_for_exception(exc) == "SANDLOOM-B037"
    assert exc.missing == ("kombu",)
    assert "No module named 'kombu'" in str(exc)
    assert "pip install 'sandloom[celery]'" in str(exc)
    assert isinstance(exc.__cause__, ModuleNotFoundError)


def test_an_explicit_instance_on_an_uninstalled_driver_refuses_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator who selects a driver in INFRA gets B037 at the build of
    the registry, before any module loads; a synthesized default does not."""
    monkeypatch.setitem(sys.modules, "celery", None)
    _real_registry()  # the implicit taskqueue/default alone: no refusal
    config = load_infra_config(settings)
    config["taskqueue"]["jobs"] = InstanceConfig(
        capability="taskqueue",
        name="jobs",
        driver="celery",
        options={"broker": "redis://127.0.0.1:1/0"},
    )
    with pytest.raises(DriverUnavailableError, match=r"SANDLOOM-B037") as caught:
        build_infra_registry(
            InMemorySchemaRegistry(), config=config, load_plugins=False
        )
    assert "taskqueue/jobs: driver 'celery' needs the package 'celery'" in str(
        caught.value
    )


# --------------------------------------------------- implicit instances ----


def test_synthesized_defaults_are_implicit_until_infra_names_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "infra", {"taskqueue": {"default": {"driver": "celery"}}}
    )
    config = load_infra_config(settings)
    assert config["taskqueue"]["default"].implicit is False
    assert all(
        cfg.implicit
        for capability, instances in config.items()
        if capability != "taskqueue"
        for cfg in instances.values()
    )


@pytest.mark.asyncio
async def test_an_unused_synthesized_instance_is_not_opened_or_probed() -> None:
    """Nothing requires taskqueue/default: no build, no aopen, no probe,
    listed as unused. The kernel's cache is opened and probed regardless."""
    registry, spies = _registry(
        _instance("taskqueue", "jobs", implicit=True),
        _instance("cache", "kv", implicit=True),
    )
    for capability, driver in (("taskqueue", "jobs"), ("cache", "kv")):
        registry.register_driver(capability, driver, spies[capability])

    await registry.aopen_all(strict=True)
    health = await registry.health_all()

    assert spies["taskqueue"].builds == []
    assert _Provider.opened == ["cache/default"]
    assert set(health) == {"cache:default"}
    assert _Provider.probed == ["cache/default"]
    described = registry.describe()["instances"]
    assert described["taskqueue"]["default"]["status"] == "unused"
    assert described["taskqueue"]["default"]["in_use"] is False
    assert described["cache"]["default"]["in_use"] is True
    assert "status" not in described["cache"]["default"]


@pytest.mark.asyncio
async def test_a_requirement_puts_the_instance_in_use() -> None:
    registry, spies = _registry(_instance("taskqueue", "jobs", implicit=True))
    registry.register_driver("taskqueue", "jobs", spies["taskqueue"])
    assert registry.in_use("taskqueue") is False
    registry.check_module_requirements("billing", [CapabilityRequirement("taskqueue")])
    assert registry.in_use("taskqueue") is True
    await registry.aopen_all(strict=True)
    assert _Provider.opened == ["taskqueue/default"]


@pytest.mark.asyncio
async def test_a_failed_optional_requirement_does_not_put_it_in_use() -> None:
    registry, spies = _registry(_instance("taskqueue", "jobs", implicit=True))
    registry.register_driver(
        "taskqueue", "jobs", spies["taskqueue"], requires=(_ABSENT,)
    )
    optional = registry.check_module_requirements(
        "identity", [CapabilityRequirement("taskqueue", required=False)]
    )
    assert len(optional) == 1
    assert registry.in_use("taskqueue") is False
    await registry.aopen_all(strict=True)
    assert spies["taskqueue"].builds == []


def test_the_kernel_instances_stay_required_in_the_real_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping unneeded probes keeps the revocation/security cache: it is
    in use with no module naming it; the taskqueue, object store and
    notifier defaults are not."""
    monkeypatch.setattr(settings, "infra", {})
    registry = _real_registry()
    assert {
        ("database", "default"),
        ("cache", "default"),
        ("eventbus", "default"),
    } == set(KERNEL_INSTANCES)
    for capability, name in KERNEL_INSTANCES:
        assert registry.in_use(capability, name), capability
    for capability in ("taskqueue", "objectstore", "notification"):
        assert not registry.in_use(capability), capability


@pytest.mark.asyncio
async def test_strict_startup_names_a_kernel_instance_whose_package_is_missing() -> (
    None
):
    """The kernel's cache stays required: in production its missing driver
    package aborts startup as B037 with the install hint, and the driver is
    never built; in development it is a warning."""
    registry, spies = _registry(_instance("cache", "kv", implicit=True))
    registry.register_driver(
        "cache", "kv", spies["cache"], requires=(_ABSENT,), install="pip install kv"
    )
    with pytest.raises(InfraStartupError) as caught:
        await registry.aopen_all(strict=True)
    assert error_codes.code_for_exception(caught.value) == "SANDLOOM-B037"
    assert f"cache/default: driver 'kv' needs the package '{_ABSENT}'" in str(
        caught.value
    )
    assert "pip install kv" in str(caught.value)
    assert spies["cache"].builds == []

    await registry.aopen_all(strict=False)
    assert spies["cache"].builds == []


@pytest.mark.asyncio
async def test_a_reachability_failure_keeps_its_uncoded_startup_error() -> None:
    registry, _ = _registry(_instance("cache", "kv"))

    class _Down(_Provider):
        async def aopen(self) -> None:
            raise ConnectionError("cannot reach cache")

    registry.register_driver(
        "cache",
        "kv",
        lambda cfg: _Down(name=cfg.name, driver=cfg.driver, options=cfg.options),
    )
    with pytest.raises(InfraStartupError) as caught:
        await registry.aopen_all(strict=True)
    assert error_codes.code_for_exception(caught.value) is None


# ----------------------------------------------------------------- loader ----


def test_the_module_preflight_refuses_with_b037_and_the_load_names_it(
    monkeypatch: pytest.MonkeyPatch, no_network: list[Any]
) -> None:
    """The loader's preflight raises the module's B037; the aggregated load
    failure carries B037, not the generic B001."""
    from fastapi import FastAPI

    from sandloom.core.descriptors import (
        FailedModule,
        ModuleDescriptor,
        ModuleLoadSummary,
    )
    from sandloom.core.facade import CoreFacade
    from sandloom.core.loader.plugin_loader import (
        ModuleLoadError,
        _preflight_capabilities,
    )

    monkeypatch.setitem(sys.modules, "celery", None)
    core = CoreFacade(app=FastAPI())
    core.set_infra(_real_registry())
    desc = ModuleDescriptor(
        name="ic1_orders",
        version="1",
        capabilities=[
            CapabilityRequirement("database"),
            CapabilityRequirement("taskqueue", features=frozenset({"delay"})),
            CapabilityRequirement("search", required=False),
        ],
    )
    with pytest.raises(CapabilityUnavailable) as caught:
        _preflight_capabilities(core, desc)
    assert "module 'ic1_orders'" in str(caught.value)
    assert no_network == []

    def load_error(*errors: str) -> ModuleLoadError:
        summary = ModuleLoadSummary(explicit=[])
        for i, error in enumerate(errors):
            summary.failed.append(
                FailedModule(name=f"m{i}", error=error, required=True)
            )
        return ModuleLoadError(summary)

    refused = load_error(str(caught.value))
    assert error_codes.code_for_exception(refused) == "SANDLOOM-B037"
    mixed = load_error(str(caught.value), "ImportError: no module named 'x'")
    assert error_codes.code_for_exception(mixed) == "SANDLOOM-B001"


# -------------------------------------------------------------- catalogue ----


def test_b037_is_in_the_catalogue_and_explained() -> None:
    entry = error_codes.get("SANDLOOM-B037")
    assert entry.kind == "boot"
    assert entry.slug == "capability-unavailable"
    text = error_codes.explain_text(entry)
    assert "find_spec" in text and "INFRA" in text
    for name in ("DriverUnavailableError", "CapabilityUnavailable"):
        assert error_codes.EXCEPTION_CODES[name] == "SANDLOOM-B037"
