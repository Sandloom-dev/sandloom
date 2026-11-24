"""The beginner tier after dx-module-less-boilerplate (G-SDK-04, SANDLOOM-T009).

The module contract sends authors to sagas, projections, a refusal, ``UpdatedAt``
and ``bind_tenant``; a marker on each of those imports was noise. The tier is a
declaration in each public module (``__beginner__``), read statically by the
T009 check and at runtime by the generated reference; the two must agree.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import sandloom.sdk
from sandloom.core.archcheck import api10_rules

SDK = Path(__file__).resolve().parents[2] / "src" / "sandloom" / "sdk"

#: The proposal under "Owner decision" of the pull request.
_TIERS: dict[str, set[str]] = {
    "sandloom.sdk.sagas": {"Saga", "SagaContext", "local_step", "start_saga", "step"},
    "sandloom.sdk.projections": {
        "Projection",
        "ProjectionView",
        "SourceGroup",
        "View",
        "full_state",
        "tombstone",
    },
    "sandloom.sdk.tenant": {"bind_tenant"},
    "sandloom.sdk.errors": {"Conflict", "NotFound", "Refused"},
}
_NEW_ROOT_NAMES = {"NotFound", "Conflict", "Refused", "UpdatedAt", "bind_tenant"}


@pytest.mark.parametrize(("module", "names"), sorted(_TIERS.items()))
def test_the_tier_of_each_module_is_the_proposed_list(
    module: str, names: set[str]
) -> None:
    assert set(importlib.import_module(module).__beginner__) == names


def test_the_root_tier_is_everything_it_exports() -> None:
    assert set(sandloom.sdk.__beginner__) == set(sandloom.sdk.__all__)
    assert len(sandloom.sdk.__all__) == 25
    assert _NEW_ROOT_NAMES <= set(sandloom.sdk.__all__)


def test_the_new_root_names_are_the_ones_of_their_homes() -> None:
    from sandloom.sdk import data, errors, tenant

    assert sandloom.sdk.NotFound is errors.NotFound
    assert sandloom.sdk.Conflict is errors.Conflict
    assert sandloom.sdk.Refused is errors.Refused
    assert sandloom.sdk.UpdatedAt is data.UpdatedAt
    assert sandloom.sdk.bind_tenant is tenant.bind_tenant


def test_every_beginner_name_is_exported_by_its_module() -> None:
    for module in (*_TIERS, "sandloom.sdk.data"):
        mod = importlib.import_module(module)
        assert set(mod.__beginner__) <= set(mod.__all__), module
        for name in mod.__beginner__:
            assert hasattr(mod, name), f"{module}.{name}"


def test_the_static_reading_agrees_with_the_runtime_declaration() -> None:
    """T009 reads the tiers from source: a tier it read differently would flag a
    name the reference calls beginner (or the reverse)."""
    static = api10_rules.beginner_names(SDK)
    for module in ("sandloom.sdk", *_TIERS, "sandloom.sdk.data"):
        runtime = set(importlib.import_module(module).__beginner__)
        assert static[module] == runtime, module


def _problems(tmp_path: Path, source: str) -> list[str]:
    py = tmp_path / "m.py"
    py.write_text(source)
    return api10_rules.find_beginner_tier_problems(py, api10_rules.beginner_names(SDK))


@pytest.mark.parametrize(
    "source",
    [
        "from sandloom.sdk import NotFound, Conflict, Refused, UpdatedAt, bind_tenant\n",
        "from sandloom.sdk.sagas import Saga, SagaContext, local_step, start_saga, step\n",
        "from sandloom.sdk.projections import Projection, SourceGroup, View\n",
        "from sandloom.sdk.projections import full_state, tombstone, ProjectionView\n",
        "from sandloom.sdk.data import UpdatedAt\n",
    ],
)
def test_a_beginner_name_needs_no_marker(tmp_path: Path, source: str) -> None:
    assert _problems(tmp_path, source) == []


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ("from sandloom.sdk.sagas import SagaStep\n", "SagaStep"),
        ("from sandloom.sdk.sagas import saga_status\n", "saga_status"),
        ("from sandloom.sdk.projections import monotonic_upsert\n", "monotonic_upsert"),
        ("from sandloom.sdk.data import Versioned\n", "Versioned"),
        ("from sandloom.sdk.data import open_uow\n", "open_uow"),
        ("from sandloom.sdk.tenant import unscoped\n", "unscoped"),
    ],
)
def test_an_advanced_name_still_needs_its_marker(
    tmp_path: Path, source: str, name: str
) -> None:
    """The bite of the tier: widening it must not have opened everything."""
    found = _problems(tmp_path, source)
    assert len(found) == 1 and name in found[0]
    marked = source.rstrip("\n") + "  # sandloom: advanced a reason\n"
    assert _problems(tmp_path, marked) == []
