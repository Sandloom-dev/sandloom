"""``sandloom check`` rule ``best-effort-effects`` (0.7).

``publish_event`` / ``record_audit`` are guaranteed by default: outside a Unit
of Work they raise. ``required=False`` is the explicit best-effort form (a
direct bus publish / a skipped audit row); a business module may use it only
with a justified ``# sandloom: best-effort <reason>`` marker on the call.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from sandloom.core.archcheck import load_rules, run_checks
from sandloom.core.archcheck.analyzers import find_unmarked_best_effort_effects
from sandloom.core.archcheck.runner import RULES

FIXTURE = (
    Path(__file__).parent
    / "_fixtures"
    / "broken_module"
    / "services"
    / "best_effort_effects.py.txt"
)


ALIASES_FIXTURE = FIXTURE.with_name("best_effort_aliases.py.txt")


def test_rule_is_registered() -> None:
    assert "best-effort-effects" in RULES


def test_fixture_violations_are_flagged_and_marked_calls_pass() -> None:
    found = find_unmarked_best_effort_effects(FIXTURE)
    assert len(found) == 5, found
    joined = "\n".join(found)
    for shape in (
        "publish_event(..., required=False)",
        "record_audit(..., required=False)",
        "publish_event(..., required=flag)",
        "publish_event_versions(..., required=False)",
    ):
        assert shape in joined, (shape, found)
    # Every hit is in the `violations` function (the compliant half is clean).
    source = FIXTURE.read_text().splitlines()
    compliant_start = next(
        i for i, line in enumerate(source, 1) if "async def compliant" in line
    )
    assert all(int(f.split(":")[0].split()[1]) < compliant_start for f in found)


def test_aliased_effects_and_kwargs_splat_are_flagged() -> None:
    """``publish_event as emit``, a re-bound name and ``**kwargs`` count."""
    found = find_unmarked_best_effort_effects(ALIASES_FIXTURE)
    joined = "\n".join(found)
    for shape in (
        "emit (= publish_event)(..., required=False)",
        "audit (= record_audit)(..., required=False)",
        "emit (= publish_event)(..., **opts)",
        "send (= publish_event)(..., required=False)",
        "record_audit(..., **opts)",
    ):
        assert shape in joined, (shape, found)
    assert len(found) == 5, found
    source = ALIASES_FIXTURE.read_text().splitlines()
    compliant_start = next(
        i for i, line in enumerate(source, 1) if "async def compliant" in line
    )
    assert all(int(f.split(":")[0].split()[1]) < compliant_start for f in found)


def test_guaranteed_calls_are_never_flagged(tmp_path: Path) -> None:
    sample = tmp_path / "svc.py"
    sample.write_text(
        textwrap.dedent(
            """
            from sandloom.core.di import publish_event, record_audit
            async def f(db):
                await publish_event("a.b.v1", {})
                await publish_event("a.b.v1", {}, required=True)
                record_audit("a.b", actor_id=None)
                record_audit("a.b", required=True)
            """
        )
    )
    assert find_unmarked_best_effort_effects(sample) == []


def test_rule_runs_in_sandloom_check(tmp_path: Path) -> None:
    """The same rule fires through run_checks (what ``sandloom check`` executes)."""
    pkg = tmp_path / "src" / "acme" / "modules"
    (pkg / "orders").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "orders" / "__init__.py").write_text(
        "from sandloom.core.di import record_audit\n"
        "def f():\n    record_audit('orders.x', required=False)\n"
    )
    manifest = tmp_path / "architecture_rules.yml"
    manifest.write_text(
        "modules_path: src/acme/modules\n"
        "modules_package: acme.modules\n"
        "public_api:\n  - sandloom.core.di\n"
    )
    violations = run_checks(load_rules(manifest), only=["best-effort-effects"])
    assert [v.rule for v in violations] == ["best-effort-effects"]
    assert "required=False" in violations[0].detail
