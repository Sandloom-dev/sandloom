"""E2E: an ``sandloom new`` application splits into microservices, with no code
change (``tools/smoke/split_check.py``, the CI job ``app-split``).

On a fresh ``sandloom new hub --with identity,tenancy,billing`` with two modules
(``orders`` calls ``catalog``'s contract and consumes its event), the
documented commands split it into identity, tenancy, billing, catalog and
orders services: ``sandloom add deploy`` writes the profile and the gateway, a
repeated run changes nothing, no Python file changes, ``docker compose
config`` accepts the profile, it passes the split invariants ``sandloom test``
checks (SANDLOOM-T014) and every service boots alone with its bindings
(``sandloom add deploy --check``).

With ``SANDLOOM_SPLIT_SMOKE=1`` the stack also runs for real (``docker compose
up``): through the gateway, login collects tenancy's claims over RPC, orders
calls catalog over RPC, and orders consumes catalog's event.

Opt-in (slow: ``uv sync`` of a fresh app, ~1 min; the smoke builds the image,
~3 min): ``SANDLOOM_SPLIT_E2E=1``. CI runs the same script in its own job.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "smoke" / "split_check.py"


def test_an_sandloom_new_app_splits_into_microservices(tmp_path: Path) -> None:
    if os.environ.get("SANDLOOM_SPLIT_E2E", "").strip() != "1":
        pytest.skip("opt-in: set SANDLOOM_SPLIT_E2E=1 (slow end-to-end scenario)")
    if not SCRIPT.is_file() or shutil.which("uv") is None:
        pytest.skip("needs the framework repository and uv")
    smoke = os.environ.get("SANDLOOM_SPLIT_SMOKE", "").strip() == "1"
    argv = [sys.executable, str(SCRIPT), "--workdir", str(tmp_path / "work")]
    if smoke:
        argv += ["--smoke", "--require-docker"]
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PROJECT"}
    }
    done = subprocess.run(
        argv, env=env, capture_output=True, text=True, timeout=1800, check=False
    )
    tail = (done.stdout + done.stderr)[-6000:]
    assert done.returncode == 0, tail
    assert "split: OK" in done.stdout, tail
    if smoke:
        assert '"catalog":true' in done.stdout.replace(" ", ""), tail
        assert "orders consumed catalog.item.created.v1" in done.stdout, tail
