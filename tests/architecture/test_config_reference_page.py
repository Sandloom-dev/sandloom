"""The configuration reference page is generated whole and files every setting.

It once carried ~100 settings under "Other", an ``<enum 'Environment'>`` type, a
migration tier that does not exist and no ``WORKER_CONCURRENCY``. The generator
runs in a child process: importing it seeds environment variables.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# The page lists the settings of the bundled modules (the admin console among
# them), so its generator imports every battery. The kernel-without-batteries
# lane hides them: the file skips there and runs on every lane that installs
# them (backend-tests, the docs job's reference check).
pytest.importorskip(
    "sandloom_admin",
    reason="the configuration reference lists the bundled modules' settings",
)

REPO = Path(__file__).resolve().parents[2]
PAGE = "reference/configuration.md"

_RENDER = (
    "import sys; sys.path.insert(0, 'tools/docs'); "
    "import gen_config_reference as g; "
    f"sys.stdout.write(g.render_pages()[{PAGE!r}])"
)
_CATEGORY = (
    "import sys; sys.path.insert(0, 'tools/docs'); "
    "import gen_config_reference as g; "
    "sys.stdout.write(g._category(sys.argv[1]))"
)


def _run(code: str, *args: str) -> str:
    done = subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


@pytest.fixture(scope="module")
def page() -> str:
    return _run(_RENDER)


def test_the_committed_page_is_what_the_generator_writes(page: str) -> None:
    committed = (REPO / "docs" / PAGE).read_text(encoding="utf-8")
    assert committed.strip() == page.strip(), (
        "run `uv run python tools/docs/docs_reference.py` and commit the page"
    )


def test_no_setting_is_left_under_other(page: str) -> None:
    assert "\n## Other\n" not in page, "place the new setting in gen_config_reference"


def test_a_setting_no_rule_places_stays_under_other() -> None:
    """The check above bites: an unplaced name does land in ``Other``."""
    assert _run(_CATEGORY, "NO_RULE_PLACES_THIS") == "Other"
    assert _run(_CATEGORY, "RPC_TRANSPORT") == "Service Registry & RPC"
    assert _run(_CATEGORY, "CONTRACT_DB_POOL_SIZE") == "Database"


def test_types_read_as_values_not_as_python_reprs(page: str) -> None:
    assert "<enum" not in page
    assert "Optional[" not in page
    assert "`'dev' | 'test' | 'prod'`" in page


def test_the_page_names_no_tier_that_does_not_exist(page: str) -> None:
    assert "**migration**" not in page
    assert "there is no transitional tier" in page


def test_the_worker_variables_are_listed(page: str) -> None:
    assert "| `WORKER_CONCURRENCY` |" in page
    assert "| `SANDLOOM_WORKER_TASKQUEUE` |" in page
