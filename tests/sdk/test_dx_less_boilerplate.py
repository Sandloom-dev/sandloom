"""The two tiers after dx-module-less-boilerplate (G-SDK-04, SANDLOOM-T009).

Tier 1 is ``__beginner__``: a name free in any file. Tier 2 is ``__after_first__``,
``{module file: names}``: a name free in the module file ``sandloom add`` writes
for it (``sagas.py``, ``projections.py``, ``tasks.py``) and marked anywhere else.
Both are declarations in each public module, read statically by the T009 check
and at runtime by the generated reference; the two readings must agree.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import sandloom.sdk
from sandloom.core.archcheck import api10_rules

SDK = Path(__file__).resolve().parents[2] / "src" / "sandloom" / "sdk"

#: Tier 1 of the modules the owner decision changed (the root is every tier 1
#: name of the others).
_TIER_1: dict[str, set[str]] = {
    "sandloom.sdk.errors": {"Conflict", "NotFound", "Refused"},
    "sandloom.sdk.tenant": {"bind_tenant"},
    "sandloom.sdk.authz": {"Account", "require"},
    "sandloom.sdk.sagas": set(),
    "sandloom.sdk.projections": set(),
    "sandloom.sdk.tasks": set(),
}
#: Tier 2: ``{module: {file: names}}``.
_TIER_2: dict[str, dict[str, set[str]]] = {
    "sandloom.sdk.sagas": {
        "sagas.py": {"Saga", "SagaContext", "local_step", "start_saga", "step"}
    },
    "sandloom.sdk.projections": {
        "projections.py": {"Projection", "SourceGroup", "full_state", "tombstone"}
    },
    "sandloom.sdk.tasks": {"tasks.py": {"periodic", "task"}},
    # ``open_uow`` is a data name, and belongs to a task body
    "sandloom.sdk.data": {"tasks.py": {"open_uow"}},
}
#: Names the owner decision took out of tier 1: advanced again.
_LEFT_TIER_1 = {"Owner", "authorize_resource", "fake_access_fact"}
_NEW_ROOT_NAMES = {"NotFound", "Conflict", "Refused", "UpdatedAt", "bind_tenant"}


@pytest.mark.parametrize(("module", "names"), sorted(_TIER_1.items()))
def test_tier_1_of_each_module_is_the_decided_list(
    module: str, names: set[str]
) -> None:
    assert set(importlib.import_module(module).__beginner__) == names


@pytest.mark.parametrize(("module", "files"), sorted(_TIER_2.items()))
def test_tier_2_of_each_module_is_the_decided_list(
    module: str, files: dict[str, set[str]]
) -> None:
    declared = importlib.import_module(module).__after_first__
    assert {file: set(names) for file, names in declared.items()} == files


def test_the_root_tier_is_everything_it_exports() -> None:
    assert set(sandloom.sdk.__beginner__) == set(sandloom.sdk.__all__)
    assert len(sandloom.sdk.__all__) == 24
    assert _NEW_ROOT_NAMES <= set(sandloom.sdk.__all__)
    assert "Account" in sandloom.sdk.__all__


def test_the_names_that_left_tier_1_are_advanced_again() -> None:
    from sandloom.sdk import testing

    assert not _LEFT_TIER_1 & set(sandloom.sdk.__all__)
    assert not _LEFT_TIER_1 & set(testing.__beginner__)
    for module in ("sandloom.sdk.authz", "sandloom.sdk.testing"):
        mod = importlib.import_module(module)
        assert not _LEFT_TIER_1 & set(mod.__beginner__), module
    # still public, in the module that owns them
    assert {"Owner", "authorize_resource"} <= set(
        importlib.import_module("sandloom.sdk.authz").__all__
    )
    assert "fake_access_fact" in testing.__all__


def test_the_new_root_names_are_the_ones_of_their_homes() -> None:
    from sandloom.sdk import authz, data, errors, tenant

    assert sandloom.sdk.NotFound is errors.NotFound
    assert sandloom.sdk.Conflict is errors.Conflict
    assert sandloom.sdk.Refused is errors.Refused
    assert sandloom.sdk.UpdatedAt is data.UpdatedAt
    assert sandloom.sdk.bind_tenant is tenant.bind_tenant
    assert sandloom.sdk.Account is authz.Account


def test_every_declared_name_is_exported_by_its_module() -> None:
    for module in (*_TIER_1, *_TIER_2, "sandloom.sdk.data"):
        mod = importlib.import_module(module)
        listed = set(mod.__beginner__) | {
            n for names in getattr(mod, "__after_first__", {}).values() for n in names
        }
        assert listed <= set(mod.__all__), module
        for name in listed:
            assert hasattr(mod, name), f"{module}.{name}"


def test_the_static_reading_agrees_with_the_runtime_declaration() -> None:
    """T009 reads the tiers from source: a tier it read differently would flag a
    name the reference calls free (or the reverse)."""
    beginner = api10_rules.beginner_names(SDK)
    after_first = api10_rules.after_first_names(SDK)
    for module in ("sandloom.sdk", *_TIER_1, *_TIER_2, "sandloom.sdk.data"):
        mod = importlib.import_module(module)
        assert beginner[module] == set(mod.__beginner__), module
        declared = {
            file: set(names)
            for file, names in getattr(mod, "__after_first__", {}).items()
        }
        assert after_first[module] == declared, module


def _problems(
    tmp_path: Path, source: str, *, file: str = "services.py", under: str = "orders"
) -> list[str]:
    """T009 on ``<tmp_path>/<under>/<file>``, ``tmp_path`` being the modules
    directory: ``under="orders"`` is a module's root, ``orders/api`` is not."""
    py = tmp_path / under / file
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text(source)
    return api10_rules.find_beginner_tier_problems(
        py,
        api10_rules.beginner_names(SDK),
        api10_rules.after_first_names(SDK),
        tmp_path,
    )


@pytest.mark.parametrize(
    "source",
    [
        "from sandloom.sdk import NotFound, Conflict, Refused, UpdatedAt, bind_tenant\n",
        "from sandloom.sdk import Account, require\n",
        "from sandloom.sdk.data import UpdatedAt\n",
    ],
)
@pytest.mark.parametrize("file", ["services.py", "sagas.py", "api.py"])
def test_a_tier_1_name_needs_no_marker_in_any_file(
    tmp_path: Path, source: str, file: str
) -> None:
    assert _problems(tmp_path, source, file=file) == []


@pytest.mark.parametrize(
    ("file", "source"),
    [
        (
            "sagas.py",
            "from sandloom.sdk.sagas import Saga, SagaContext, local_step, "
            "start_saga, step\n",
        ),
        (
            "projections.py",
            "from sandloom.sdk.projections import Projection, SourceGroup, "
            "full_state, tombstone\n",
        ),
        ("tasks.py", "from sandloom.sdk.tasks import periodic, task\n"),
        ("tasks.py", "from sandloom.sdk.data import open_uow\n"),
    ],
)
def test_a_tier_2_name_needs_no_marker_in_the_file_made_for_it(
    tmp_path: Path, file: str, source: str
) -> None:
    assert _problems(tmp_path, source, file=file) == []


@pytest.mark.parametrize(
    ("file", "source", "names"),
    [
        ("services.py", "from sandloom.sdk.sagas import Saga, step\n", "Saga, step"),
        ("api.py", "from sandloom.sdk.sagas import start_saga\n", "start_saga"),
        (
            "services.py",
            "from sandloom.sdk.projections import tombstone\n",
            "tombstone",
        ),
        ("services.py", "from sandloom.sdk.tasks import task\n", "task"),
        ("services.py", "from sandloom.sdk.data import open_uow\n", "open_uow"),
        # a tier 2 name is free in ITS file, not in the other two
        ("projections.py", "from sandloom.sdk.sagas import Saga\n", "Saga"),
        ("sagas.py", "from sandloom.sdk.tasks import task\n", "task"),
        ("sagas.py", "from sandloom.sdk.data import open_uow\n", "open_uow"),
    ],
)
def test_a_tier_2_name_elsewhere_needs_its_marker(
    tmp_path: Path, file: str, source: str, names: str
) -> None:
    found = _problems(tmp_path, source, file=file)
    assert len(found) == 1 and names in found[0] and "tier 2" in found[0], found
    marked = source.rstrip("\n") + "  # sandloom: advanced a reason\n"
    assert _problems(tmp_path, marked, file=file) == []


@pytest.mark.parametrize(
    ("file", "source", "name"),
    [
        ("sagas.py", "from sandloom.sdk.sagas import SagaStep\n", "SagaStep"),
        ("sagas.py", "from sandloom.sdk.sagas import saga_status\n", "saga_status"),
        (
            "projections.py",
            "from sandloom.sdk.projections import monotonic_upsert\n",
            "monotonic_upsert",
        ),
        (
            "projections.py",
            "from sandloom.sdk.projections import ProjectionView\n",
            "ProjectionView",
        ),
        ("services.py", "from sandloom.sdk.data import Versioned\n", "Versioned"),
        ("tasks.py", "from sandloom.sdk.tenant import unscoped\n", "unscoped"),
        ("api.py", "from sandloom.sdk.authz import Owner\n", "Owner"),
        (
            "api.py",
            "from sandloom.sdk.authz import authorize_resource\n",
            "authorize_resource",
        ),
    ],
)
def test_an_advanced_name_still_needs_its_marker_in_every_file(
    tmp_path: Path, file: str, source: str, name: str
) -> None:
    """The bite of the tiers: the file rule must not have opened everything. A
    name outside both tiers is flagged even in the file made for its module."""
    found = _problems(tmp_path, source, file=file)
    assert len(found) == 1 and name in found[0], found
    assert "not in the beginner tier" in found[0]
    marked = source.rstrip("\n") + "  # sandloom: advanced a reason\n"
    assert _problems(tmp_path, marked, file=file) == []


@pytest.mark.parametrize(
    ("under", "file", "source", "names"),
    [
        # the probes of the closure review: the file NAME alone is not the rule
        ("orders/api", "tasks.py", "from sandloom.sdk.tasks import task\n", "task"),
        ("orders/tests", "sagas.py", "from sandloom.sdk.sagas import Saga\n", "Saga"),
        (
            "orders/services",
            "projections.py",
            "from sandloom.sdk.projections import Projection\n",
            "Projection",
        ),
        # beside the modules, in no module's directory
        (".", "sagas.py", "from sandloom.sdk.sagas import start_saga\n", "start_saga"),
    ],
)
def test_a_tier_2_name_below_the_module_root_needs_its_marker(
    tmp_path: Path, under: str, file: str, source: str, names: str
) -> None:
    """Tier 2 is free only in ``<modules>/<module>/<file>``, the top of a
    module's own directory: the same file name in a subdirectory, or outside
    every module, is flagged, and a marker waives it."""
    found = _problems(tmp_path, source, file=file, under=under)
    assert len(found) == 1 and names in found[0] and "tier 2" in found[0], found
    assert _problems(tmp_path, source, file=file) == []  # the module root is free
    marked = source.rstrip("\n") + "  # sandloom: advanced a reason\n"
    assert _problems(tmp_path, marked, file=file, under=under) == []


def test_tier_2_declarations_without_a_modules_directory_are_refused(
    tmp_path: Path,
) -> None:
    """``after_first`` without ``modules_path`` cannot tell where a module's
    root is: it raises instead of being read as no tier 2 (or as every file)."""
    py = tmp_path / "sagas.py"
    py.write_text("from sandloom.sdk.sagas import Saga\n")
    with pytest.raises(ValueError, match="modules_path"):
        api10_rules.find_beginner_tier_problems(
            py, api10_rules.beginner_names(SDK), api10_rules.after_first_names(SDK)
        )


def test_without_the_tier_2_declarations_the_file_rule_is_off(tmp_path: Path) -> None:
    """A caller that passes none (the earlier signature) gets tier 1 alone."""
    py = tmp_path / "sagas.py"
    py.write_text("from sandloom.sdk.sagas import Saga\n")
    found = api10_rules.find_beginner_tier_problems(py, api10_rules.beginner_names(SDK))
    assert len(found) == 1 and "Saga" in found[0]
