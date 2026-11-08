"""Every entry point of the shipped image builds ONE composition (KB-08).

ADR-0012 decision 3 (owner decision #1): the standard composition lives in
the ``sandloom-standard`` distribution, and the image, the compose profiles
and the generated framework profile name it with
``SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main``. The process matrix here
is the G-CONFIG-07 harness (``test_boot_configuration_checks``): the API's
import of ``<root>:app``, the Celery worker and beat, ``sandloom migrate`` and
the composed operator commands, each a real process in an empty directory
with an explicit environment. A production module refusal stops each one
after it built the composition; a ``sitecustomize`` probe on ``PYTHONPATH``
records, at exit, which composition root the process imported and the
module set it composed. Every process of the standard root reports the same
root and the same set, and none imports the lean ``sandloom.main``; without
the variable, the same processes compose the lean root instead.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.config.common import INVALID_CONFIGURATION
from sandloom.core.descriptors.module_names import installed_modules
from sandloom.core.tests.test_boot_configuration_checks import (
    _MODULE_BASE,
    COMPOSED_ENTRY_POINTS,
    _run,
)

pytestmark = [pytest.mark.slow, pytest.mark.subprocess_probe]

STANDARD_ROOT = "sandloom_standard.main"
LEAN_ROOT = "sandloom.main"
ROOTS = (LEAN_ROOT, STANDARD_ROOT)

#: Records the composition root(s) the process imported (a ``sys.meta_path``
#: finder sees each import attempt: a refused root's import fails and leaves
#: ``sys.modules``) and the module set the composition resolved, when the
#: interpreter exits (a refusal included).
PROBE = """\
import atexit, json, os, sys

_imported = set()


class _Recorder:
    @staticmethod
    def find_spec(name, path=None, target=None):
        if name in {roots!r}:
            _imported.add(name)
        return None  # the regular finders import it


sys.meta_path.insert(0, _Recorder)


def _report():
    roots = sorted(_imported)
    try:
        from sandloom.core.config.settings import settings
        modules = sorted(settings.sandloom_modules)
    except Exception as exc:  # the report must reach the test
        modules = repr(exc)
    with open(os.environ["SANDLOOM_COMPOSITION_PROBE"], "w") as out:
        json.dump({{"roots": roots, "modules": modules}}, out)


atexit.register(_report)
"""


def _api(root: str) -> list[str]:
    """The API process's import of ``<root>:app``, as gunicorn's uvicorn worker does."""
    code = (
        "from uvicorn.importer import import_from_string; "
        f"import_from_string({root + ':app'!r})"
    )
    return [sys.executable, "-c", code]


def _entry_points(root: str) -> dict[str, list[str]]:
    """Every process of the shipped image: the API, worker, beat, migrate, CLIs."""
    return {**COMPOSED_ENTRY_POINTS, "app": _api(root)}


def _probe(tmp_path: Path) -> Path:
    site = tmp_path / "probe"
    site.mkdir()
    (site / "sitecustomize.py").write_text(PROBE.format(roots=ROOTS))
    return site


def _composition(argv: list[str], root: str | None, tmp_path: Path) -> dict[str, Any]:
    """Run ``argv`` under the module refusal; return the probe's report."""
    report = tmp_path / "report.json"
    values: dict[str, Any] = {
        **_MODULE_BASE,
        "PYTHONPATH": str(_probe(tmp_path)),
        "SANDLOOM_COMPOSITION_PROBE": str(report),
    }
    if root is not None:
        values["SANDLOOM_COMPOSITION_ROOT"] = root
    work = tmp_path / "work"
    work.mkdir()
    proc = _run(argv, values, work)
    assert proc.returncode == 1, (proc.returncode, proc.stderr[-2000:])
    last = proc.stderr.strip().splitlines()[-1]
    assert last.startswith(INVALID_CONFIGURATION), proc.stderr[-2000:]
    assert report.is_file(), proc.stderr[-2000:]
    return dict(json.loads(report.read_text()))


def _installed() -> list[str]:
    return sorted({module.name for module in installed_modules()})


@pytest.mark.parametrize("entry", sorted(COMPOSED_ENTRY_POINTS))
def test_every_entry_point_builds_the_standard_composition(
    entry: str, tmp_path: Path
) -> None:
    """With ``SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main`` (the image's
    and the profiles' setting) each process imports that root, never the lean
    one, and composes every installed module."""
    argv = _entry_points(STANDARD_ROOT)[entry]
    report = _composition(argv, STANDARD_ROOT, tmp_path)
    assert report == {"roots": [STANDARD_ROOT], "modules": _installed()}


@pytest.mark.parametrize("entry", sorted(COMPOSED_ENTRY_POINTS))
def test_without_the_variable_every_entry_point_builds_the_lean_root(
    entry: str, tmp_path: Path
) -> None:
    """Control: the kernel's default stays ``sandloom.main``, so the root the
    variable names is what makes the processes above agree."""
    argv = _entry_points(LEAN_ROOT)[entry]
    report = _composition(argv, None, tmp_path)
    assert report == {"roots": [LEAN_ROOT], "modules": _installed()}
