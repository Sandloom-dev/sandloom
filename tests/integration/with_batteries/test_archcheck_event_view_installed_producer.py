"""SANDLOOM-C025 reads an installed producer's ``module.snapshot.json``.

Moved from ``tests/architecture/test_api10_checks.py``: the view names a
channel of the bundled tenancy module, so the check needs ``sandloom_tenancy``
installed (the architecture suite runs without the batteries). The analyzer's
other cases stay there.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from sandloom.core.archcheck import api10_rules as a10


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def test_c025_reads_an_installed_producers_module_snapshot(tmp_path: Path) -> None:
    """The kernel ships no exported schema (ADR-0013): a view of a foreign
    channel with no producer class in the tree and no exported file is checked
    against the ``module.snapshot.json`` of the installed producer, and is
    unverified only when no installed producer lists the channel."""
    views = _write(
        tmp_path,
        "consumers.py",
        """
        class Added(Event, channel="tenancy.member_added.v2"):
            organization_id: UUID
            version: int

        class Wrong(Event, channel="tenancy.member_added.v2"):
            version: str

        class Nobody(Event, channel="nobody.thing.v1"):
            x: int
        """,
    )
    found = a10.find_event_view_problems({"stats": [views]}, None)
    levels = sorted((level, detail) for level, _file, detail in found)
    assert [level for level, _ in levels].count("warning") == 1
    assert any("nobody.thing.v1" in d for level, d in levels if level == "warning")
    errors = [d for level, d in levels if level == "error"]
    assert any("'version' is str here but integer" in d for d in errors), errors
    assert not any("view Added" in d for d in errors), errors
