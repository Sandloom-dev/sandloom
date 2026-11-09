"""Integration tests: ``skip_unless_db_feature``'s marker and the generated
access atoms.

The kernel's tests register the marker in ``src/sandloom/core/tests/conftest.py``
and the pytest plugin registers it for applications; a run that collects only
this directory needs it too (``--strict-markers``). The autouse fixture that
applies it comes with ``sandloom.core.testing.fixtures``' star import.

``generated_atoms`` is the access atoms a consumer imports from its stubs of
``tenancy.membership.v1`` and ``billing.entitlements.v1``, rendered as
``sandloom generate`` renders them (KB-34: the kernel keeps no domain atom).
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from types import SimpleNamespace

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "sandloom_db_feature(*features, database='default'): skip unless the "
        "database instance has the features (skip_unless_db_feature)",
    )


@pytest.fixture(scope="session")
def generated_atoms(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[SimpleNamespace]:
    """``OrgRole`` (tenancy's stub) and ``SubscriptionActive``, ``Credits``,
    ``Subscribed``, ``Feature``, ``WithinLimit``, ``LiveFeature``,
    ``LiveWithinLimit`` (billing's), imported as a consumer imports them."""
    from sandloom.core.cli.generated import render_stub
    from sandloom_billing.contracts import EntitlementsContract
    from sandloom_tenancy.contracts import MembershipReadContract

    folder = tmp_path_factory.mktemp("generated_atoms")
    modules = {}
    for contract, provider in (
        ("tenancy.membership.v1", MembershipReadContract),
        ("billing.entitlements.v1", EntitlementsContract),
    ):
        stem = contract.replace(".", "_")
        path = folder / f"{stem}.py"
        path.write_text(render_stub(contract, provider))
        spec = importlib.util.spec_from_file_location(f"_generated_atoms_{stem}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        modules[contract] = module
    tenancy = modules["tenancy.membership.v1"]
    billing = modules["billing.entitlements.v1"]
    try:
        yield SimpleNamespace(
            OrgRole=tenancy.OrgRole,
            **{
                name: getattr(billing, name)
                for name in (
                    "SubscriptionActive",
                    "Credits",
                    "Subscribed",
                    "Feature",
                    "WithinLimit",
                    "LiveFeature",
                    "LiveWithinLimit",
                )
            },
        )
    finally:
        for module in modules.values():
            sys.modules.pop(module.__name__, None)
