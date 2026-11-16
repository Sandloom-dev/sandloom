"""The guarantee & capability ledger is a gate (``guarantees.yml``).

Every guarantee Sandloom promises and every capability a user relies on is an entry
with a stable id and the tests / ``sandloom check`` rules / import contracts that
prove it (``tools/checks/guarantees.py`` holds the checker). This test fails when:

- an entry's enforcing test (node id), rule or import contract no longer exists;
- an issued id is missing from the ledger without a ``removed:`` record that
  carries a reason and the owner's approval note (``approved_by``);
- an ADR (every ``docs/adr/NNNN-*.md``) has a ``##`` / ``###`` heading that
  ``adr_sections`` does not map to ledger ids (or mark ``informational``) — a
  new ADR, or a new section of one, needs its ledger entries in the same change;
- an entry has no enforcement and is not recorded as a ``gap``;
- ``docs/reference/guarantees.md`` is stale (regenerate with
  ``uv run python tools/checks/guarantees.py render``).

A change that removes or infers a declaration keeps this green and shows the
enforcing tests passing: ``uv run pytest -m guarantees`` with the paths of
``tools/checks/guarantees.py paths``.
"""

from __future__ import annotations

import copy
import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "tools" / "checks" / "guarantees.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sandloom_guarantee_ledger", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


ledger_mod = _load_module()


@pytest.fixture(scope="module")
def raw() -> dict[str, Any]:
    loaded: dict[str, Any] = ledger_mod.load().raw
    return loaded


def _problems(raw: dict[str, Any], adr_dir: Path | None = None) -> list[str]:
    kwargs = {} if adr_dir is None else {"adr_dir": adr_dir}
    problems: list[str] = ledger_mod.check(ledger_mod.parse(raw), **kwargs)
    return problems


def test_ledger_is_green() -> None:
    problems = ledger_mod.check()
    assert not problems, "guarantees.yml:\n  " + "\n  ".join(problems)


def test_reference_page_is_fresh() -> None:
    expected = ledger_mod.render()
    actual = ledger_mod.REFERENCE.read_text(encoding="utf-8")
    assert actual == expected, (
        "docs/reference/guarantees.md is stale — run "
        "`uv run python tools/checks/guarantees.py render`"
    )


def test_ledger_is_not_empty(raw: dict[str, Any]) -> None:
    assert len(raw["guarantees"]) >= 50
    assert len(raw["capabilities"]) >= 15


# --------------------------------------------------------------------------- #
# rests_on: the infra capability, operations and features an entry needs
# (design/infra-drivers/spec.md section 12, ADR-0016 section 7).

#: What a ``rests_on`` item pins: capability, instance, features it needs, features it needs
#: absent. Operations are not pinned (the checker reads them from the kernel port).
Shape = tuple[str, str, frozenset[str], frozenset[str]]


def _shape(
    capability: str,
    instance: str = "",
    features: tuple[str, ...] = (),
    without: tuple[str, ...] = (),
) -> Shape:
    return (capability, instance, frozenset(features), frozenset(without))


_SECURITY = _shape("atomic_store", "security", ("shared", "never_evicts"))
_SECURITY_GET = _shape("atomic_store", "security")
_IDEM = _shape("atomic_store", "idempotency", ("shared",))
_IDEM_ANY = _shape("atomic_store", "idempotency")
_TASKQUEUE = _shape("taskqueue")
_BEAT = _shape("lease", "beat", ("shared",))
_CACHE = _shape("cache", "default")
_BUS = _shape("eventbus")
_PROFILE = {
    _SECURITY,
    _shape("atomic_store", "idempotency", ("shared", "never_evicts")),
    _CACHE,
}


def _pin(ids: tuple[str, ...], *shapes: Shape) -> dict[str, frozenset[Shape]]:
    return {i: frozenset(shapes) for i in ids}


def _ids(prefix: str, *numbers: int) -> tuple[str, ...]:
    return tuple(f"{prefix}-{n:02d}" for n in numbers)


#: Spec section 12, row by row: the capabilities each entry rests on. The ledger must say
#: exactly this; changing a row of the spec changes it here. An empty set is the only
#: ``rests_on: []``: the entry rests on no capability (kernel bus logic, the per-process
#: replay guard, settings validation).
PINNED_REST_ON: dict[str, frozenset[Shape]] = {
    **_pin(_ids("G-REVOKE", 1, 2, 3), _SECURITY),
    **_pin(_ids("G-REVOKE", 10, 12, 21), _SECURITY_GET),
    **_pin(_ids("G-REVOKE", 19), _SECURITY_GET, _IDEM_ANY),
    **_pin(_ids("G-BLOCK", 1, 2, 3, 6), _SECURITY),
    **_pin(_ids("G-IDEM", 1, 2, 3, 5), _IDEM),
    **_pin(_ids("G-IDEM", 8, 18), _IDEM_ANY),
    **_pin(_ids("G-IDEM", 16), _SECURITY_GET, _IDEM_ANY),
    **_pin(
        _ids("G-IDEM", 9, 10, 11, 12), _shape("database", "", ("http_idempotency",))
    ),
    **_pin(_ids("G-TASK", 1, 2, 3), _IDEM, _TASKQUEUE),
    **_pin(_ids("G-TASK", 4, 5) + _ids("C-WORKER", 1, 2, 3, 4, 5), _TASKQUEUE),
    **_pin(("G-INFRA-11",), _shape("taskqueue", "", ("periodic",))),
    **_pin(("G-OPS-01", "C-WORKER-06"), _BEAT, _TASKQUEUE),
    **_pin(("G-SAGA-08",), _BEAT),
    **_pin(("G-HTTP-02", "G-INBOX-03"), _CACHE),
    **_pin(("G-RPC-08",), _shape("atomic_store", "security", ("shared",))),
    **_pin(("G-RPC-07", "G-RPC-09")),
    **_pin(_ids("G-PROFILE", 1, 6, 7, 8), *_PROFILE),
    **_pin(_ids("G-EVENT", 1, 2, 3, 5, 6, 7, 12), _BUS),
    **_pin(_ids("G-EVENT", 9), _shape("eventbus", "", ("group_start_id",))),
    **_pin(_ids("G-EVENT", 10), _shape("eventbus", "", ("durable",))),
    **_pin(_ids("G-EVENT", 11), _shape("eventbus", "", ("dlq_ops",))),
    **_pin(_ids("G-EVENT", 4, 8, 13)),
    **_pin(
        ("G-EVENT-14", "G-RISK-04"),
        _shape("eventbus", "", (), ("durable",)),
        _shape("broadcast"),
    ),
    **_pin(("G-MIGRATE-07", "G-MIGRATE-08"), _BUS),
    **_pin(("G-INFRA-10",), _shape("*")),
}
REST_ON_IDS = tuple(PINNED_REST_ON)
NO_CAPABILITY_IDS = tuple(i for i, shapes in PINNED_REST_ON.items() if not shapes)


def _shape_of(rest: Any) -> Shape:
    return (
        rest.capability,
        rest.instance,
        frozenset(rest.features),
        frozenset(rest.without_features),
    )


def test_spec_section_12_entries_carry_rests_on() -> None:
    entries = ledger_mod.load().by_id()
    missing = [i for i in REST_ON_IDS if i not in entries]
    assert not missing, f"not in the ledger: {missing}"
    unstated = [i for i in REST_ON_IDS if not entries[i].rests_on_declared]
    assert not unstated, f"no rests_on (spec section 12): {unstated}"


def _pin_differences(raw: dict[str, Any]) -> dict[str, Any]:
    """Entries whose ``rests_on`` is not what spec section 12 pins (actual, expected)."""
    entries = ledger_mod.parse(raw).by_id()
    return {
        entry_id: (
            sorted(map(repr, (_shape_of(r) for r in entries[entry_id].rests_on))),
            sorted(map(repr, expected)),
        )
        for entry_id, expected in PINNED_REST_ON.items()
        if frozenset(_shape_of(r) for r in entries[entry_id].rests_on) != expected
    }


def test_rests_on_matches_spec_section_12(raw: dict[str, Any]) -> None:
    """Capability, instance and features are the spec's, entry by entry; ``[]`` only
    for the entries pinned as resting on no capability."""
    wrong = _pin_differences(raw)
    assert not wrong, (
        f"rests_on differs from spec section 12 (actual, expected): {wrong}"
    )
    entries = ledger_mod.parse(raw).by_id()
    assert set(NO_CAPABILITY_IDS) == {i for i in REST_ON_IDS if not entries[i].rests_on}


def test_rests_on_pin_catches_a_dropped_feature_or_swapped_capability(
    raw: dict[str, Any],
) -> None:
    """G-REVOKE-01 with ``never_evicts`` dropped, or its item replaced by the cache,
    or ``rests_on: []``, no longer matches the pin (and only that entry differs)."""

    def mutate(rests_on: Any) -> dict[str, Any]:
        mutated = copy.deepcopy(raw)
        entry = next(e for e in mutated["guarantees"] if e["id"] == "G-REVOKE-01")
        entry["rests_on"] = rests_on
        return mutated

    first = next(e for e in raw["guarantees"] if e["id"] == "G-REVOKE-01")["rests_on"]
    dropped = copy.deepcopy(first)
    dropped[0]["features"] = ["shared"]
    for rests_on in (
        dropped,
        [{"capability": "cache", "operations": ["get"]}],
        [],
    ):
        assert set(_pin_differences(mutate(rests_on))) == {"G-REVOKE-01"}


def test_detects_unknown_rests_on_instance(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    entry = next(e for e in mutated["guarantees"] if e.get("rests_on"))
    entry["rests_on"] = [
        {"capability": "atomic_store", "instance": "streams", "operations": ["get"]}
    ]
    assert any("is not an instance the kernel names" in p for p in _problems(mutated))


def test_detects_rests_on_operation_that_is_not_a_port_method(
    raw: dict[str, Any],
) -> None:
    mutated = copy.deepcopy(raw)
    entry = next(e for e in mutated["guarantees"] if e.get("rests_on"))
    entry["rests_on"] = [
        {"capability": "taskqueue", "operations": ["send(task_id=)", "WorkerBoot"]}
    ]
    problems = _problems(mutated)
    assert any(
        "'send(task_id=)' is not an operation of taskqueue" in p for p in problems
    )
    assert any("'WorkerBoot' is not an operation of taskqueue" in p for p in problems)


def test_detects_a_feature_needed_and_absent_in_two_items(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    entry = next(e for e in mutated["guarantees"] if e.get("rests_on"))
    entry["rests_on"] = [
        {"capability": "eventbus", "features": ["durable"], "note": "needs"},
        {"capability": "eventbus", "without_features": ["durable"], "note": "absent"},
    ]
    assert any("needed by one eventbus item" in p for p in _problems(mutated))


def test_rests_on_capabilities_are_the_kernel_registry() -> None:
    from sandloom.core.infra.capabilities import BUILTIN_CAPABILITIES

    declared = ledger_mod.capability_features()
    assert set(declared) == {spec.name for spec in BUILTIN_CAPABILITIES}
    used = {
        rest.capability
        for entry in ledger_mod.load().entries
        for rest in entry.rests_on
        if rest.capability != ledger_mod.ANY_CAPABILITY
    }
    assert used <= set(declared)


def test_detects_unknown_rests_on_capability(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    entry = next(e for e in mutated["guarantees"] if e.get("rests_on"))
    entry["rests_on"] = [{"capability": "redis", "operations": ["get"]}]
    assert any("is not a kernel capability" in p for p in _problems(mutated))


def test_detects_undeclared_rests_on_feature(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    entry = next(e for e in mutated["guarantees"] if e.get("rests_on"))
    entry["rests_on"] = [
        {"capability": "atomic_store", "operations": ["get"], "features": ["nope"]}
    ]
    assert any(
        "'nope' is not a feature of atomic_store" in p for p in _problems(mutated)
    )


def test_detects_malformed_rests_on(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    first, second = (e for e in mutated["guarantees"][:2])
    first["rests_on"] = [{"capability": "lease", "feature": ["shared"]}]
    second["rests_on"] = [{"capability": "lease"}]
    problems = _problems(mutated)
    assert any("unknown keys ['feature']" in p for p in problems)
    assert any("names no operation, feature or note" in p for p in problems)


# --------------------------------------------------------------------------- #
# Positive detection: the gate catches each failure it promises to catch.


def test_detects_vanished_enforcing_test(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    entry = next(e for e in mutated["guarantees"] if e.get("tests"))
    entry["tests"] = [
        entry["tests"][0].split("::")[0] + "::test_does_not_exist_anywhere"
    ]
    assert any("enforcing test vanished" in p for p in _problems(mutated))


def test_detects_vanished_test_file(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    entry = next(e for e in mutated["guarantees"] if e.get("tests"))
    entry["tests"] = ["tests/architecture/test_nope.py::test_x"]
    assert any("does not exist" in p for p in _problems(mutated))


def test_detects_unknown_rule(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    mutated["guarantees"][0]["rules"] = ["no-such-rule"]
    assert any("unknown sandloom check rule" in p for p in _problems(mutated))


def test_detects_deleted_entry_without_removed_record(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    victim = mutated["guarantees"].pop(0)
    problems = _problems(mutated)
    assert any(victim["id"] in p and "removed:" in p for p in problems), problems


def test_deleting_the_last_entry_of_a_prefix_is_caught(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    prefix = mutated["guarantees"][0]["id"].rsplit("-", 1)[0]
    last = max(
        (e for e in mutated["guarantees"] if e["id"].startswith(prefix + "-")),
        key=lambda e: int(e["id"].rsplit("-", 1)[1]),
    )
    mutated["guarantees"].remove(last)
    assert any(last["id"] in p for p in _problems(mutated))


def test_removed_record_needs_reason_and_owner_approval(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    victim = mutated["guarantees"].pop(0)
    mutated.setdefault("removed", [])
    mutated["removed"] = [*mutated["removed"], {"id": victim["id"], "statement": "x"}]
    problems = _problems(mutated)
    assert any("approved_by" in p for p in problems)
    assert any("'reason'" in p for p in problems)

    mutated["removed"][-1].update(
        reason="superseded by inference",
        approved_by="owner: ok in #123",
        date="2026-10-25",
    )
    assert not [p for p in _problems(mutated) if victim["id"] in p]


def test_entry_without_enforcement_must_be_a_gap(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    entry = mutated["guarantees"][0]
    for key in ("tests", "rules", "import_contracts", "gap", "gap_size"):
        entry.pop(key, None)
    assert any("record it as a gap" in p for p in _problems(mutated))


def test_new_adr_section_needs_an_entry(raw: dict[str, Any], tmp_path: Path) -> None:
    adr_dir = tmp_path / "adr"
    shutil.copytree(ledger_mod.ADR_DIR, adr_dir)
    existing = sorted(adr_dir.glob("0005-*.md"))[0]
    existing.write_text(
        existing.read_text(encoding="utf-8")
        + "\n### 9. A brand new promise\n\nText.\n",
        encoding="utf-8",
    )
    (adr_dir / "0099-new-decision.md").write_text(
        "# ADR-0099: New\n\n## Decision\n\nWe promise X.\n", encoding="utf-8"
    )
    problems = _problems(raw, adr_dir)
    assert any("9. A brand new promise" in p for p in problems), problems
    assert any("0099-new-decision.md is not mapped" in p for p in problems), problems


def test_headings_inside_code_fences_are_ignored(tmp_path: Path) -> None:
    doc = tmp_path / "x.md"
    doc.write_text(
        "## Real\n\n```bash\n# not a heading\n## nor this\n```\n### Also real\n"
    )
    assert ledger_mod.headings(doc) == ["Real", "Also real"]


def test_marker_matching() -> None:
    ids = ("src/a/test_x.py::test_y", "src/a/test_z.py::TestK")
    assert ledger_mod.item_matches("src/a/test_x.py::test_y[param-1]", ids)
    assert ledger_mod.item_matches("src/a/test_z.py::TestK::test_m", ids)
    assert not ledger_mod.item_matches("src/a/test_x.py::test_y2", ids)


def test_the_marker_run_collects_the_driver_packages_tests() -> None:
    """A row evidenced in ``packages/<package>/tests`` is part of the
    ``pytest -m guarantees`` selection and its directory is among the paths the
    run is given; a node id outside the repository's test roots is not."""
    ids = ledger_mod.ledger_nodeids_for_marker()
    in_packages = [n for n in ids if n.startswith("packages/sandloom-celery/tests/")]

    assert in_packages, "no celery package test reaches the guarantees marker run"
    assert all(n.startswith(ledger_mod.PYTEST_ROOTS) for n in ids)
    assert not any(n.startswith(("docs/", "design/")) for n in ids)
    paths = ledger_mod.pytest_paths(ledger_mod.load())
    assert paths[0] == "src"
    assert "packages/sandloom-celery/tests" in paths
    # sandloom-s3 runs in its own CI lane: its tests do not collect here.
    assert "packages/sandloom-s3/tests" not in paths
    covered = tuple(p + "/" for p in paths if p != "src")
    dev_env = [
        n
        for n in ids
        if n.startswith("packages/") and n.split("/")[1] in ledger_mod.DEV_ENV_PACKAGES
    ]
    assert dev_env
    assert all(n.startswith(covered) for n in dev_env)
