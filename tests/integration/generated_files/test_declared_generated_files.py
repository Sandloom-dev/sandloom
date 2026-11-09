"""The console's manifest is a file the console declares (ADR-0012 decision 4).

``sandloom generate`` writes every file a loaded module declares, rendered from
the composed app's data. Each composition boots in its own process (no
lifespan, no connection) and reports what ``generate`` would write:

- AD-1: without the console there is no manifest, the platform API stays;
- AD-2: the console and identity out of the box: identity's screens;
- AD-4: the platform operations API is the kernel's, console or not;
- AD-5: the third-party ``inventory`` (``thirdparty/``, outside ``src/``)
  declares screens and its section reaches the manifest with no kernel or
  console change.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.integration]

BACKEND = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent

_PROBE = """
import json, tempfile
from pathlib import Path
from sandloom.core.cli import generated
from sandloom.core.composition import load_composition_app
app = load_composition_app()
planned = [f for f in generated.plan(app, Path(tempfile.mkdtemp()), None)
           if f.code == "SANDLOOM-T016"]
print(json.dumps({
    "files": {f.path.name: {"content": f.content, "problems": f.problems}
              for f in planned},
    "paths": sorted({getattr(r, "path", "") for r in app.routes}),
}))
"""


def _console_importable() -> bool:
    return importlib.util.find_spec("sandloom_admin") is not None


def _compose(*modules: str, root: str | None = None) -> dict[str, Any]:
    """Boot the composition in a child; ``root`` names a composition root (an
    issuer such as identity needs one with a trust map, SANDLOOM-B035)."""
    env = {
        **os.environ,
        "SANDLOOM_MODULES": json.dumps(list(modules)),
        "ENVIRONMENT": "dev",
        "BILLING_PROVIDER": "fake",
        "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:9/never",
        "CACHE_URL": "redis://127.0.0.1:9/0",
        "BROKER_URL": "redis://127.0.0.1:9/0",
        "JWT_SECRET": "generated-files-test-only-" + "x" * 32,
        "PYTHONPATH": os.pathsep.join(
            [str(HERE / "thirdparty"), *(p for p in sys.path if p)]
        ),
    }
    env.pop("SANDLOOM_COMPOSITION_ROOT", None)
    if root:
        env["SANDLOOM_COMPOSITION_ROOT"] = root
    done = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_without_the_console_no_manifest_and_the_platform_api_stays() -> None:
    """AD-1 and AD-4: no ``/admin/``, no manifest; the kernel's platform API."""
    report = _compose("inventory")
    assert report["files"] == {}
    assert not any(p.startswith("/admin") for p in report["paths"])
    assert "/api/v1/platform/inventory/audit" in report["paths"]


@pytest.mark.skipif(not _console_importable(), reason="no console package")
def test_a_third_party_module_s_screens_reach_the_manifest() -> None:
    """AD-5: ``inventory`` declares screens; generate adds its section."""
    report = _compose("sandloom_admin", "inventory")
    [(name, item)] = report["files"].items()
    assert name == "admin-manifest.json" and item["problems"] == []
    manifest = json.loads(item["content"])
    assert manifest["modules"][0] == "inventory"
    [stock] = [r for r in manifest["resources"] if r["name"] == "stock"]
    assert stock["module"] == "inventory"
    assert [c["name"] for c in stock["columns"]] == ["sku", "quantity"]


@pytest.mark.skipif(
    not (
        _console_importable()
        and importlib.util.find_spec("sandloom_identity")
        and importlib.util.find_spec("sandloom_standard")
    ),
    reason="no console, identity or standard-composition package",
)
def test_the_console_and_identity_out_of_the_box() -> None:
    """AD-2: the console declares its manifest; identity's screens are in it."""
    report = _compose(
        "sandloom_identity", "sandloom_admin", root="sandloom_standard.main"
    )
    item = report["files"]["admin-manifest.json"]
    assert item["problems"] == []
    manifest = json.loads(item["content"])
    assert "identity" in manifest["modules"]
    assert any(r["module"] == "identity" for r in manifest["resources"])
    assert "/admin" in report["paths"]
