"""L1 reads module names with the kernel's rule (entry-point name, else the
last segment), without importing the kernel."""

from sandloom.core.descriptors import module_names
from sandloom.sdk._decl import _owner


def test_the_sdk_owner_rule_agrees_with_the_kernel() -> None:
    installed = module_names.installed_modules()
    assert {m.name for m in installed} >= {"identity", "tenancy", "billing"}
    for module in installed:
        assert _owner.short_name(module.path) == module_names.module_name(module.path)
    assert _owner.short_name("app.modules.notes") == "notes"
