"""Rolling-upgrade contract compatibility (sandloom.core.contract_compat).

* ``compare_contract`` accepts additive changes and reports breaking ones
  precisely (removed method / parameter, newly required parameter, changed
  kind / idempotency key, incompatible type, reordered positional parameter,
  model field changes);
* the committed per-module ``contracts.snapshot.json`` files match what the
  modules register (the CI diff gate);
* a caller's ``/ready`` fails with a ``contract_incompatible`` diagnostic when
  a bound provider's live describe document breaks the caller's snapshot,
  and passes for an additive provider change;
* ``sandloom contracts check --against <snapshot>`` exits 1 on a breaking change.

Moved from ``sandloom/core/tests/test_contract_compat.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

from fastapi import FastAPI

from sandloom.core import contract_compat
from sandloom.core.facade import CoreFacade


# --------------------------------------------------------------------------- #
# Committed snapshots are current (CI diff gate)
# --------------------------------------------------------------------------- #
def test_committed_module_snapshots_match_registered_contracts() -> None:
    from sandloom_standard.main import core

    docs = contract_compat.module_snapshots(core)
    assert {"sandloom_identity", "sandloom_tenancy", "sandloom_billing"} <= set(docs)
    for module, doc in docs.items():
        path = contract_compat.snapshot_path(module)
        assert path is not None and path.is_file(), f"{module}: no snapshot"
        assert path.read_text(encoding="utf-8") == contract_compat.render_snapshot(
            doc
        ), f"{path} is stale: run `sandloom contracts snapshot --write`"


def test_expectation_is_found_in_the_provider_package_without_importing_it() -> None:
    core = CoreFacade(app=FastAPI())
    core.loaded_modules = ["sandloom_identity"]
    found = contract_compat.expected_contracts(core, ["billing.credits.v1"])
    assert found["billing.credits.v1"].source.endswith(
        "sandloom_billing/contracts.snapshot.json"
    )
    assert "spend" in found["billing.credits.v1"].describe["methods"]


def test_a_bundled_consumer_is_held_to_its_own_expectation() -> None:
    """KB-21: billing's expectation of ``tenancy.membership.v1`` comes from
    beside its own stub (what a consumer-only install carries), verified and
    narrowed to its composed ``consumes``."""
    from sandloom_standard.main import core

    contract = "tenancy.membership.v1"
    plan = contract_compat.resolve_expectations(core, [contract])
    assert plan.unverified == {}
    (found,) = plan.expectations[contract]
    assert found.consumer == "sandloom_billing"
    assert found.source.endswith(
        "sandloom_billing/_generated/contracts/tenancy_membership_v1.describe.json"
    )
    assert found.methods == ("get_member_state", "get_role", "has_role_at_least")
    assert set(found.describe["methods"]) == set(found.methods)
