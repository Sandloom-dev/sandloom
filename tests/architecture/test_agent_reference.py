"""``.claude/agent-reference.md`` is what ``sandloom generate`` writes today.

The file (registries, CI by path, kernel map, plan index) is rendered by
``tools/agent_reference.py`` from the workflows, ``tools/ci/required_ci.py``, the
kernel's AST and ``design/kernel-boundary/migration-plan.md``. A change to one of
them (a moved kernel entry point, a new CI job, a new KB section) fails here until
``uv run sandloom generate`` rewrites the file.
"""

from __future__ import annotations

from pathlib import Path

import sandloom.core.cli.generated as generated

REPO = Path(__file__).resolve().parents[2]
REFERENCE = REPO / ".claude" / "agent-reference.md"


def test_the_agent_reference_is_current() -> None:
    (item,) = generated._agent_reference(REPO)
    assert item.path == REFERENCE
    assert not item.stale, (
        "stale .claude/agent-reference.md: run `uv run sandloom generate`"
    )


def test_the_agent_reference_stays_small() -> None:
    size = len(REFERENCE.read_bytes())
    assert size <= 8_192, f"{size} bytes: an agent reads it whole, trim a section"


def test_an_application_gets_no_agent_reference(tmp_path: Path) -> None:
    assert generated._agent_reference(tmp_path) == []
