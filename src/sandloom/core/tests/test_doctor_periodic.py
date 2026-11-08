"""``sandloom doctor``'s ``periodic`` section and ``/diagnostics/core``'s
``periodic`` block (KB-IC-3; owner decision on infrastructure dependencies,
answers 1 and 3).

For each loaded module whose taskqueue requirement asks for ``periodic``:
does a beat schedule its jobs for THIS composition? The beat is seen through
its singleton lease (the key a beat of the same composition computes). A
required need without a beat fails; an optional one (identity's token
cleanup) warns: the module works and degrades as its docs describe.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from sandloom.core import beat_lease
from sandloom.core.cli import doctor
from sandloom.core.config.settings import settings
from sandloom.core.descriptors import CapabilityRequirement, ModuleDescriptor
from sandloom.core.infra.config import InstanceConfig

_TOKEN = "kbic3-diagnostics-token-0123456789abcdef0123456789"
_PERIODIC = frozenset({"periodic"})
_REQUIRED = ModuleDescriptor(
    name="kbic3_reconciles",
    version="1",
    capabilities=[CapabilityRequirement("taskqueue", features=_PERIODIC)],
)
_OPTIONAL = ModuleDescriptor(
    name="kbic3_cleans_up",
    version="1",
    capabilities=[
        CapabilityRequirement("taskqueue", features=_PERIODIC, required=False)
    ],
)
_NONE = ModuleDescriptor(name="kbic3_plain", version="1")


class _Cache:
    def __init__(self, value: Any = None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.keys: list[str] = []

    async def get(self, key: str) -> Any:
        self.keys.append(key)
        if self.error is not None:
            raise self.error
        return self.value


class _Infra:
    def __init__(
        self, error: Exception | None = None, broker: str | None = None
    ) -> None:
        self.error = error
        self.broker = broker

    def provider(self, capability: str, name: str = "default") -> Any:
        if self.error is not None:
            raise self.error
        return SimpleNamespace(driver="celery")

    def instance_config(self, capability: str, name: str = "default") -> Any:
        options = {"broker": self.broker} if self.broker else {}
        return InstanceConfig(capability, name, "celery", options)


def _core(
    *descriptors: ModuleDescriptor, cache: _Cache, infra: _Infra | None = None
) -> Any:
    return SimpleNamespace(
        loaded_modules=[d.name for d in descriptors],
        module_descriptors=list(descriptors),
        infra=infra or _Infra(),
        cache=lambda name: cache,
    )


async def _periodic(core: Any) -> dict[str, doctor.Check]:
    report = await doctor.run_doctor(
        skip=frozenset(doctor.SECTIONS) - {"periodic"}, core_loader=lambda: core
    )
    return {c.name: c for c in report.checks if c.section == "periodic"}


async def test_a_held_lease_is_ok_and_names_the_beat() -> None:
    cache = _Cache("7:beat-host:4242:abcdef123456")

    checks = await _periodic(_core(_REQUIRED, _OPTIONAL, _NONE, cache=cache))

    assert set(checks) == {
        "kbic3_reconciles periodic jobs",
        "kbic3_cleans_up periodic jobs",
    }
    for check in checks.values():
        assert check.status == doctor.OK
        assert "beat-host:4242" in check.message
    assert len(cache.keys) == 1  # one read per taskqueue instance


async def test_no_beat_fails_a_required_need_and_warns_an_optional_one() -> None:
    checks = await _periodic(_core(_REQUIRED, _OPTIONAL, cache=_Cache(None)))

    required = checks["kbic3_reconciles periodic jobs"]
    optional = checks["kbic3_cleans_up periodic jobs"]
    assert required.status == doctor.FAIL
    assert "no beat holds the lease" in required.message
    assert "celery -A sandloom.core.worker beat" in required.message
    assert optional.status == doctor.WARN
    assert "optional: the module works without them" in optional.message


async def test_only_an_optional_need_without_a_beat_is_a_warning_exit() -> None:
    core = _core(_OPTIONAL, cache=_Cache(None))
    report = await doctor.run_doctor(
        skip=frozenset(doctor.SECTIONS) - {"periodic"}, core_loader=lambda: core
    )

    assert report.exit_code == doctor.EXIT_WARN


async def test_an_unreadable_lease_is_a_warning_not_a_verdict() -> None:
    core = _core(_REQUIRED, cache=_Cache(error=ConnectionError("cache down")))

    check = (await _periodic(core))["kbic3_reconciles periodic jobs"]

    assert check.status == doctor.WARN
    assert "cannot be read" in check.message


async def test_no_periodic_need_skips_the_section() -> None:
    checks = await _periodic(_core(_NONE, cache=_Cache(None)))

    assert [c.status for c in checks.values()] == [doctor.SKIP]


async def test_the_key_read_is_the_one_a_beat_of_this_composition_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scope a beat computes (``build_lease``): broker of the taskqueue
    instance, the instance, the loaded module set; ``BEAT_LEASE_NAME`` wins."""
    monkeypatch.setattr(settings, "beat_lease_name", "")
    cache = _Cache(None)
    core = _core(_REQUIRED, _NONE, cache=cache, infra=_Infra(broker="redis://b:6379/3"))

    await beat_lease.beat_lease_holder(core)

    expected = beat_lease.lease_scope(
        "redis://b:6379/3", "default", ["kbic3_reconciles", "kbic3_plain"]
    )
    assert cache.keys == [beat_lease.LEASE_KEY_PREFIX + expected]

    monkeypatch.setattr(settings, "beat_lease_name", "pinned")
    await beat_lease.beat_lease_holder(core)
    assert cache.keys[-1] == beat_lease.LEASE_KEY_PREFIX + "pinned"


async def test_diagnostics_core_reports_the_periodic_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/diagnostics/core`` (internal token) says whether a beat schedules
    each module's periodic jobs: one cache read per call, never on a probe."""
    from sandloom.core.app_factory import _install_ops_endpoints
    from sandloom.core.drivers.bootstrap import build_infra_registry
    from sandloom.core.facade import CoreFacade
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    app = FastAPI()
    core = CoreFacade(app=app)
    core.set_infra(build_infra_registry(InMemorySchemaRegistry(), load_plugins=False))
    core.loaded_modules = [_REQUIRED.name, _OPTIONAL.name]
    core.module_descriptors = [_REQUIRED, _OPTIONAL]
    cache = _Cache(None)
    monkeypatch.setattr(core, "cache", lambda name="default": cache)
    monkeypatch.setattr(settings, "internal_api_token", _TOKEN)
    monkeypatch.setattr(settings, "service_bindings", {})
    _install_ops_endpoints(app, core)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        body = (
            await client.get("/diagnostics/core", headers={"X-Internal-Token": _TOKEN})
        ).json()

    assert body["periodic"] == [
        {
            "module": "kbic3_reconciles",
            "instance": "default",
            "required": True,
            "beat": "absent",
        },
        {
            "module": "kbic3_cleans_up",
            "instance": "default",
            "required": False,
            "beat": "absent",
        },
    ]
    assert len(cache.keys) == 1
