"""E2E: the ``sandloom docs`` recipes run as written in an ``sandloom new`` application
(``tools/smoke/recipes_check.py``, the CI job ``app-recipes``).

On a fresh ``sandloom new hub --with identity,tenancy,billing``, every recipe
with ``# file:`` blocks is applied in its own module (its ``uv run sandloom
add|migrate new`` lines, then its files), ``sandloom test`` passes on the whole
app and ``sandloom docs`` answers contracts, events and recipes from the
installed packages.

Opt-in (slow: ``uv sync`` of a fresh app and its full gate, ~2 min):
``SANDLOOM_RECIPES_E2E=1``; it uses the test server of ``SANDLOOM_TEST_DATABASE_URL``.
CI runs the same script in its own job.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "smoke" / "recipes_check.py"


def test_every_recipe_runs_in_a_generated_app(tmp_path: Path) -> None:
    if os.environ.get("SANDLOOM_RECIPES_E2E", "").strip() != "1":
        pytest.skip("opt-in: set SANDLOOM_RECIPES_E2E=1 (slow end-to-end scenario)")
    if not SCRIPT.is_file() or shutil.which("uv") is None:
        pytest.skip("needs the framework repository and uv")
    argv = [sys.executable, str(SCRIPT), "--workdir", str(tmp_path / "work")]
    database = os.environ.get("SANDLOOM_TEST_DATABASE_URL", "")
    if database:
        server = urlsplit(database.replace("+asyncpg", ""))
        argv += ["--postgres-url", f"{server.scheme}://{server.netloc}"]
        argv += ["--db-prefix", f"{Path(server.path).name}_recipes"]
    cache = os.environ.get("SANDLOOM_TEST_CACHE_URL", "")
    if cache:
        redis = urlsplit(cache)
        argv += ["--redis-url", f"{redis.scheme}://{redis.netloc}"]
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PROJECT"}
        and not k.startswith("SANDLOOM_TEST_")
    }
    done = subprocess.run(
        argv, env=env, capture_output=True, text=True, timeout=1800, check=False
    )
    tail = (done.stdout + done.stderr)[-6000:]
    assert done.returncode == 0, tail
    assert "recipes: OK" in done.stdout, tail
