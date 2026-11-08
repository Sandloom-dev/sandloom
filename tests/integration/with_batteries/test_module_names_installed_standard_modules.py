"""The installed standard modules by their module names (moved from the
kernel's ``test_module_names.py``, KB-05c).

Discovery reads the ``sandloom.modules`` entry points the bundled module
distributions register; configuration names them by entry-point name. The
kernel's own suite keeps the naming rule and the ambiguity refusal on
synthetic entry points.
"""

from __future__ import annotations

from sandloom.core.descriptors import module_names
from sandloom.core.descriptors.module_names import module_name


def test_the_installed_standard_modules_are_discovered() -> None:
    names = {m.name: m.path for m in module_names.installed_modules()}
    assert names["identity"] == "sandloom_identity"
    assert names["tenancy"] == "sandloom_tenancy"
    assert names["billing"] == "sandloom_billing"
    assert module_name("sandloom_identity") == "identity"


def test_configuration_names_installed_modules_by_entry_point_name() -> None:
    from sandloom.core.testing.boot import load_topology

    _core, summary = load_topology(["identity", "tenancy"], exact=True)
    assert [d.name for d in summary.loaded] == ["sandloom_identity", "sandloom_tenancy"]
    assert summary.explicit == ["sandloom_identity", "sandloom_tenancy"]
