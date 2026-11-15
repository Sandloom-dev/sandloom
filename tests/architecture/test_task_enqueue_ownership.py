"""SANDLOOM-C035: a module enqueues only its own tasks by name.

The kernel checks that an enqueued task name is declared somewhere in the
process, never that the caller owns it (PR #200 security review, S1). The
cases of ``_fixtures/c035/cases.py.txt`` are one module file each: every
``bad`` one must be reported, no ``ok`` one. The same review (S3) keeps
``declare_task`` off the modules and refuses an aliased ``task`` / ``periodic``
import (SANDLOOM-C033), which would hide the decorator from the derivation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sandloom.core.archcheck import module_rules as mr
from sandloom.core.archcheck.analyzers import find_legacy_infra_imports
from sandloom.core.error_codes import CATALOGUE

_TEXT = (Path(__file__).parent / "_fixtures" / "c035" / "cases.py.txt").read_text(
    encoding="utf-8"
)
_PARTS = re.split(r"^# === (bad|ok): (\w+)\n", _TEXT, flags=re.MULTILINE)
_CASES = [(_PARTS[i], _PARTS[i + 1], _PARTS[i + 2]) for i in range(1, len(_PARTS), 3)]


def _write(tmp_path: Path, name: str, source: str) -> list[Path]:
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return [path]


@pytest.mark.parametrize(("verdict", "name", "source"), _CASES)
def test_the_case_is_judged(
    tmp_path: Path, verdict: str, name: str, source: str
) -> None:
    hits = mr.find_foreign_task_enqueues(_write(tmp_path, name, source), "c035app")
    assert bool(hits) is (verdict == "bad"), hits


def test_the_fixture_has_both_kinds() -> None:
    assert {v for v, _, _ in _CASES} == {"bad", "ok"}
    assert len(_CASES) >= 8


def test_the_code_is_in_the_catalogue_with_its_rule() -> None:
    entry = next(e for e in CATALOGUE if e.code == "SANDLOOM-C035")
    assert entry.slug == "task-enqueue-ownership" and entry.kind == "check"


def test_a_module_cannot_import_declare_task_from_di(tmp_path: Path) -> None:
    path = tmp_path / "m.py"
    path.write_text("from sandloom.core.di import declare_task\n", encoding="utf-8")
    assert find_legacy_infra_imports(path)


def test_an_aliased_task_decorator_import_is_refused(tmp_path: Path) -> None:
    module = tmp_path / "mod"
    module.mkdir()
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "tasks.py").write_text(
        "from sandloom.sdk.tasks import periodic as every\n\n"
        '@every(cron="*/5 * * * *", id="mod.job")\n'
        "def job():\n    return None\n",
        encoding="utf-8",
    )
    hits = mr.find_undeclared_capabilities(module, [module / "__init__.py"])
    assert hits and "periodic as every" in hits[0][1]
