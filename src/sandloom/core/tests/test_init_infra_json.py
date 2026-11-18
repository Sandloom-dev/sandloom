"""The README's SQLite ``INFRA`` line names every Redis instance of ``DEFAULT_INFRA``.

``stack_files.infra_json`` writes it with a capability list of its own, next to the
``DEFAULT_INFRA`` of the generated ``__init__.py``; ``.env.test`` and the
compose file are held to the selection by ``test_project.py``, this line by
this test: an instance missing from it would make the app refuse to start
(a Redis instance with no url).
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

from sandloom.core.cli.project import generate_project


def test_the_readme_sqlite_infra_names_every_redis_instance_of_default_infra(
    tmp_path: Path,
) -> None:
    generate_project("probe", tmp_path)
    root = tmp_path / "probe"
    spec = importlib.util.spec_from_file_location(
        "probe_selection_readme", root / "probe" / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    selection = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selection)
    selected = {
        (capability, name)
        for capability, instances in selection.DEFAULT_INFRA.items()
        for name in instances
    } - {("database", "default"), ("eventbus", "default")}  # the in-process bus

    found = re.findall(r"^INFRA='(.+)'$", (root / "README.md").read_text(), re.M)
    assert len(found) == 1, "the README states one INFRA line for SQLite"
    infra = json.loads(found[0])
    named = {(c, n) for c, instances in infra.items() for n in instances}
    assert named == selected
