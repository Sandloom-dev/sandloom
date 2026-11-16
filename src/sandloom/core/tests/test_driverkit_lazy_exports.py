"""``sandloom.driverkit`` loads its exports on first use, and names every one.

A harness family imports ``sandloom.driverkit.testing_family`` before the
settings snapshot; the package ``__init__`` ran first and pulled in the
settings, SQLAlchemy and the infra and eventing layers. The exports are lazy
now (PEP 562): importing the package, or a submodule that needs no export,
loads none of them.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import sandloom.driverkit as driverkit

#: What importing the family-author module must leave unloaded.
HEAVY = (
    "sandloom.core.config.settings",
    "sqlalchemy",
    "sandloom.core.infra.config",
    "sandloom.core.eventing",
)


def _loaded_after(statement: str) -> list[str]:
    """The ``HEAVY`` modules a fresh interpreter has loaded after ``statement``."""
    code = (
        f"import json, sys; {statement}; "
        f"print(json.dumps([m for m in {HEAVY!r} if m in sys.modules]))"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    loaded: list[str] = json.loads(done.stdout)
    return loaded


def test_every_exported_name_has_a_home_and_resolves() -> None:
    assert set(driverkit.__all__) == set(driverkit._HOME)
    assert len(driverkit.__all__) == len(set(driverkit.__all__))
    for name in driverkit.__all__:
        assert getattr(driverkit, name) is not None, name
    assert set(driverkit.__all__) <= set(dir(driverkit))


def test_a_name_is_resolved_from_its_home_module() -> None:
    from sandloom.core.infra.errors import InfraError

    assert driverkit.InfraError is InfraError


def test_an_unknown_name_is_an_attribute_error() -> None:
    with pytest.raises(AttributeError, match="no attribute 'NoSuchName'"):
        driverkit.NoSuchName  # noqa: B018 - the lookup is the test


def test_importing_the_package_loads_none_of_the_kernel() -> None:
    assert _loaded_after("import sandloom.driverkit") == []


def test_the_family_author_module_loads_none_of_the_kernel_settings() -> None:
    assert _loaded_after("import sandloom.driverkit.testing_family") == []


def test_using_an_export_loads_its_module_on_demand() -> None:
    # The probe itself bites: an export does load the layer behind it.
    loaded = _loaded_after("import sandloom.driverkit as d; d.DEFAULT_INSTANCE")
    assert "sandloom.core.infra.config" in loaded
