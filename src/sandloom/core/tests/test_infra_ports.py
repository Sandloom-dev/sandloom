"""The ports package and the built-in rows are lazy tables (spec 1, "Registry
files"): importing them, and registering every built-in driver, imports no
driver module; a driver module is imported when an instance selects it."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import sandloom.core.infra.contracts as contracts
import sandloom.core.infra.ports as ports
from sandloom.core.infra.ports import _base, notification, objectstore

_PROBE = """
import json, sys
import sandloom.core.infra.ports as ports
import sandloom.core.infra.access
from sandloom.core.drivers.bootstrap import register_builtin_drivers
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.registry import InfraRegistry

def drivers():
    return sorted(
        m for m in sys.modules
        if m.startswith("sandloom.core.drivers.") and m != "sandloom.core.drivers.bootstrap"
    )

after_import = drivers()
ports_loaded = sorted(m for m in sys.modules if m.startswith("sandloom.core.infra.ports."))
registry = InfraRegistry(strict_capabilities=True)
register_builtin_drivers(registry, None)
after_register = drivers()
registry.set_config(
    {"notification": {"default": InstanceConfig("notification", "default", "log", {})}}
)
registry.validate()
after_select = drivers()
print(json.dumps([after_import, after_register, after_select, ports_loaded]))
"""


def test_no_driver_module_is_imported_until_an_instance_selects_it() -> None:
    out = subprocess.run(
        [sys.executable, "-c", _PROBE],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    after_import, after_register, after_select, _ports = json.loads(
        out.strip().splitlines()[-1]
    )
    assert after_import == []
    assert after_register == []
    # Selecting the log notifier imports its module (and what it imports),
    # never a driver no instance selected.
    assert "sandloom.core.drivers.log_notifier" in after_select
    for unselected in (
        "sandloom.core.drivers.redis_cache",
        "sandloom.core.drivers.celery_taskqueue",
        "sandloom.core.drivers.event_bus_driver",
        "sandloom.core.drivers.sqlalchemy_database",
        "sandloom.core.drivers.local_objectstore",
    ):
        assert unselected not in after_select


def test_the_kernel_door_lists_the_kernel_facing_names() -> None:
    assert set(ports.__all__) == {
        "AtomicStore",
        "Broadcast",
        "EventTransport",
        "LeaseStore",
        "ProviderLifecycle",
        "WorkerHost",
    }
    assert ports.ProviderLifecycle is _base.ProviderLifecycle
    from sandloom.core.infra.ports.atomic_store import AtomicStore

    assert ports.AtomicStore is AtomicStore


@pytest.mark.parametrize(
    ("name", "slice_"),
    [("EventTransport", "idk-eventbus-contract"), ("WorkerHost", "idk-worker-port")],
)
def test_a_name_not_delivered_yet_names_its_slice(name: str, slice_: str) -> None:
    with pytest.raises(AttributeError, match=slice_):
        getattr(ports, name)


def test_an_unknown_name_is_a_plain_attribute_error() -> None:
    with pytest.raises(AttributeError, match="no attribute 'Nope'"):
        ports.Nope  # noqa: B018 - the access is the test


def test_the_old_contract_paths_keep_working() -> None:
    assert contracts.InfraProvider is _base.InfraProvider
    assert contracts.ObjectStoreProvider is objectstore.ObjectStoreProvider
    assert contracts.validate_object_key is objectstore.validate_object_key
    assert contracts.NotifierProvider is notification.NotifierProvider
    assert contracts.Notification is notification.Notification
