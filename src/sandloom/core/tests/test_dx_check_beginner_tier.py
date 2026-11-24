"""``sandloom check`` reports SANDLOOM-T009 (an advanced ``sandloom.sdk`` import
with no ``# sandloom: advanced <reason>`` marker), not only the ``schema &
deploy`` step of ``sandloom test``.

Runs on the no-Redis lane (``src/sandloom/core/tests``): ``sandloom check``
builds no application and reads source.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.core.cli.check import main as check_main
from sandloom.core.cli.project import generate_project

_UNMARKED = "\nfrom sandloom.sdk.web import DbSession\n"


@pytest.fixture
def app(tmp_path: Path) -> tuple[Path, Path]:
    """A fresh project: its manifest and the sample module's ``services.py``."""
    generate_project("probe", tmp_path, sample_module="gadgets")
    root = tmp_path / "probe"
    return root / "architecture_rules.yml", (
        root / "probe" / "modules" / "gadgets" / "services.py"
    )


def test_a_fresh_project_has_no_unmarked_advanced_import(
    app: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    rules, _ = app
    assert check_main(["--rules", str(rules)]) == 0
    assert "SANDLOOM-T009" not in capsys.readouterr().out


def test_check_reports_an_unmarked_advanced_import(
    app: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    rules, services = app
    services.write_text(services.read_text() + _UNMARKED)
    assert check_main(["--rules", str(rules)]) == 1
    out = capsys.readouterr().out
    assert "SANDLOOM-T009" in out
    assert "DbSession" in out
    assert "probe/modules/gadgets/services.py:" in out


def test_check_accepts_a_marked_advanced_import(
    app: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    rules, services = app
    services.write_text(
        services.read_text()
        + _UNMARKED.replace(
            "DbSession", "DbSession  # sandloom: advanced a typed dependency"
        )
    )
    assert check_main(["--rules", str(rules)]) == 0
    assert "SANDLOOM-T009" not in capsys.readouterr().out


def test_check_reports_it_beside_a_rule_violation(
    app: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Both reports reach the output when a rule fails too (the other branch of
    the report): a violation does not hide the tier problem."""
    rules, services = app
    services.write_text(
        services.read_text()
        + _UNMARKED
        + "\nasync def leak(db):\n    await db.rollback()\n"
    )
    assert check_main(["--rules", str(rules)]) == 1
    out = capsys.readouterr().out
    assert "SANDLOOM-T009" in out and "DbSession" in out
    assert "uow-boundary" in out


def test_a_subset_of_rules_does_not_run_the_tier_check(
    app: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """``--only`` is a rule subset: the gates that are not rules stay out of it."""
    rules, services = app
    services.write_text(services.read_text() + _UNMARKED)
    assert check_main(["--rules", str(rules), "--only", "module-boundaries"]) == 0
    assert "SANDLOOM-T009" not in capsys.readouterr().out


def test_check_reads_the_manifest_it_was_given_not_one_beside_the_root(
    app: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """``--rules`` names the manifest: the tier check scans ITS modules, also
    when it is not the ``architecture_rules.yml`` at the project root (it used
    to load that file and report nothing for any other)."""
    rules, services = app
    services.write_text(services.read_text() + _UNMARKED)
    elsewhere = rules.parent / "ci" / "rules.yml"
    elsewhere.parent.mkdir()
    elsewhere.write_text(
        rules.read_text()
        .replace("modules_path: probe/modules", "modules_path: ../probe/modules")
        .replace("source_root: .", "source_root: ..")
    )
    assert not (elsewhere.parent / "architecture_rules.yml").exists()
    assert check_main(["--rules", str(elsewhere)]) == 1
    out = capsys.readouterr().out
    assert "SANDLOOM-T009" in out and "DbSession" in out
    assert "gadgets/services.py:" in out


_SAGA_IMPORT = "from sandloom.sdk.sagas import Saga, step\n"


def test_check_accepts_a_tier_2_import_in_the_file_made_for_it(
    app: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """``sagas.py`` is what ``sandloom add saga`` writes: its sdk names need no
    marker there (tier 2)."""
    rules, services = app
    services.with_name("sagas.py").write_text(_SAGA_IMPORT)
    assert check_main(["--rules", str(rules)]) == 0
    assert "SANDLOOM-T009" not in capsys.readouterr().out


def test_check_reports_a_tier_2_import_in_any_other_file(
    app: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The bite of the file rule: the same import in ``services.py`` is
    reported, naming the file it is free in; a marker waives it."""
    rules, services = app
    original = services.read_text()
    services.write_text(original + "\n" + _SAGA_IMPORT)
    assert check_main(["--rules", str(rules)]) == 1
    out = capsys.readouterr().out
    assert "SANDLOOM-T009" in out and "Saga, step" in out
    assert "tier 2" in out and "sagas.py" in out
    services.write_text(
        original
        + "\n"
        + _SAGA_IMPORT.rstrip("\n")
        + "  # sandloom: advanced a reason\n"
    )
    assert check_main(["--rules", str(rules)]) == 0
    assert "SANDLOOM-T009" not in capsys.readouterr().out
