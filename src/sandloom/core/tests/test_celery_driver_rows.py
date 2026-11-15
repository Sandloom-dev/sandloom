"""The kernel's row for the Celery driver of ``sandloom-celery`` and the entry
point that package publishes are one driver."""

from __future__ import annotations

from importlib import metadata

import pytest
from sandloom_celery.taskqueue import CeleryTaskQueueProvider

import sandloom.core.infra.registry as registry_module
from sandloom.core.drivers.bootstrap import (
    BUILTIN_DRIVER_ROWS,
    BUILTIN_DRIVER_TIERS,
    build_infra_registry,
)
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.registry import (
    DriverConflictError,
    DriverLoadError,
    DriverRegistration,
    DriverUnavailableError,
    InfraRegistry,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry


def _row() -> tuple[str, tuple[str, ...]]:
    rows = [
        (target, requires)
        for capability, driver, target, requires, _install, _by in BUILTIN_DRIVER_ROWS
        if (capability, driver) == ("taskqueue", "celery")
    ]
    assert len(rows) == 1
    return rows[0]


def test_the_row_targets_the_entry_point_of_the_package() -> None:
    entries = {
        entry.name: entry.value
        for entry in metadata.entry_points(group="sandloom.drivers")
        if entry.dist is not None and entry.dist.name == "sandloom-celery"
    }
    target, requires = _row()
    assert entries == {"taskqueue.celery": target}
    assert "sandloom_celery" in requires  # preflight names the missing package
    assert "celery" in requires  # and the client library


def test_the_row_resolves_to_the_package_class() -> None:
    registry = build_infra_registry(InMemorySchemaRegistry(), load_plugins=False)
    assert registry.driver_class("taskqueue", "celery") is CeleryTaskQueueProvider
    # The tier the row is registered with is the class's own declaration.
    assert CeleryTaskQueueProvider.tier == BUILTIN_DRIVER_TIERS[("taskqueue", "celery")]
    assert registry.driver_tier("taskqueue", "celery") == CeleryTaskQueueProvider.tier


def test_the_entry_point_of_the_row_is_the_same_driver_not_a_conflict() -> None:
    config = {
        "taskqueue": {
            "default": InstanceConfig(
                "taskqueue", "default", "celery", {"broker": "redis://h/1"}
            )
        }
    }
    registry = build_infra_registry(InMemorySchemaRegistry(), config=config)
    assert registry.driver_class("taskqueue", "celery") is CeleryTaskQueueProvider
    assert registry.driver_target("taskqueue", "celery") == _row()[0]


def test_another_package_cannot_publish_the_name_of_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Entry:
        name = "taskqueue.celery"
        value = "other_pkg.queue:Provider"
        dist = type("_Dist", (), {"name": "other-pkg"})()

        @staticmethod
        def load() -> type:
            return CeleryTaskQueueProvider

    class _Entries:
        def select(self, *, group: str, name: str | None = None) -> list[object]:
            if group == "sandloom.drivers" and name in (None, "taskqueue.celery"):
                return [_Entry()]
            return []

    monkeypatch.setattr(
        "sandloom.core.infra.registry.metadata.entry_points", lambda: _Entries()
    )
    config = {
        "taskqueue": {
            "default": InstanceConfig(
                "taskqueue", "default", "celery", {"broker": "redis://h/1"}
            )
        }
    }
    with pytest.raises(DriverConflictError, match="built in"):
        build_infra_registry(InMemorySchemaRegistry(), config=config)


def _without_the_package(monkeypatch: pytest.MonkeyPatch) -> None:
    original = InfraRegistry._is_installed

    def is_installed(self: InfraRegistry, name: str) -> bool:
        return False if name == "sandloom_celery" else original(self, name)

    monkeypatch.setattr(InfraRegistry, "_is_installed", is_installed)


def _implicit_registry() -> InfraRegistry:
    """A ``celery`` taskqueue the kernel synthesised (``INFRA`` names none)."""
    config = {
        "taskqueue": {
            "default": InstanceConfig(
                "taskqueue",
                "default",
                "celery",
                {"broker": "redis://h/1"},
                implicit=True,
            )
        }
    }
    return build_infra_registry(
        InMemorySchemaRegistry(), config=config, load_plugins=False
    )


def test_a_kernel_without_the_package_boots_on_its_default_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without_the_package(monkeypatch)
    registry = _implicit_registry()
    assert registry.missing_packages("taskqueue", "default") == ("sandloom_celery",)


def test_an_instance_infra_names_is_refused_without_the_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without_the_package(monkeypatch)
    config = {
        "taskqueue": {
            "default": InstanceConfig(
                "taskqueue", "default", "celery", {"broker": "redis://h/1"}
            )
        }
    }
    with pytest.raises(DriverUnavailableError, match="sandloom_celery") as caught:
        build_infra_registry(
            InMemorySchemaRegistry(), config=config, load_plugins=False
        )
    assert "sandloom[celery]" in str(caught.value.install)


def test_a_module_requirement_without_the_package_names_the_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module that requires ``periodic`` makes the registry read the driver's
    advertised features, which loads its class: the refusal comes first, with
    the install hint, not a failed import."""
    _without_the_package(monkeypatch)
    registry = _implicit_registry()

    def import_target(
        capability: str, driver: str, registration: DriverRegistration
    ) -> object:
        raise DriverLoadError("sandloom_celery is not importable", fix="-")

    monkeypatch.setattr(registry_module, "_import_target", import_target)
    with pytest.raises(DriverUnavailableError) as caught:
        registry.validate_requirement(
            "taskqueue", "default", features=frozenset({"periodic"})
        )
    assert caught.value.sandloom_code == "SANDLOOM-B037"
    assert "sandloom_celery" in caught.value.missing
    assert "pip install 'sandloom[celery]'" in str(caught.value.install)


def test_first_use_of_the_lazy_row_without_the_package_names_the_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without_the_package(monkeypatch)
    real_import = registry_module._import_target

    def import_target(
        capability: str, driver: str, registration: DriverRegistration
    ) -> object:
        if (registration.target or "").startswith("sandloom_celery"):
            raise DriverLoadError("sandloom_celery is not importable", fix="-")
        return real_import(capability, driver, registration)

    monkeypatch.setattr(registry_module, "_import_target", import_target)
    registry = _implicit_registry()
    with pytest.raises(DriverUnavailableError) as caught:
        registry.provider("taskqueue", "default")
    assert caught.value.sandloom_code == "SANDLOOM-B037"
    assert "sandloom_celery" in caught.value.missing
    assert "pip install 'sandloom[celery]'" in str(caught.value)
