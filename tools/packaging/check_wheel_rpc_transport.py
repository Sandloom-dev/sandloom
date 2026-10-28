"""Build the sandloom wheel, install it ALONE into a clean venv and run
the internal-RPC transport smoke (``tools/packaging/wheel_rpc_smoke.py``) there.

Proves the aiohttp transport dependency is packaged and that the probe + the
httpx fallback work from the installed distribution, not just the source
tree. Run from the repository root (CI ``backend-static`` step; locally also via
``tools/ci/ci_local.py``)::

    uv run python tools/packaging/check_wheel_rpc_transport.py [--python <venv python>]

``--python`` skips the build and reuses an existing venv that already has the
wheel installed (``check_package_distributions.py`` passes its lean venv).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
FRAMEWORK = BACKEND_ROOT / "packages" / "sandloom"
SMOKE = BACKEND_ROOT / "tools" / "packaging" / "wheel_rpc_smoke.py"


def _run(*command: str, cwd: Path | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True, cwd=cwd)


def _python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run_smoke(python: Path, cwd: Path) -> None:
    """Run the smoke with ``python`` in isolated mode (no cwd / script dir /
    user site on ``sys.path``) from ``cwd`` (outside the source tree)."""
    _run(str(python), "-I", str(SMOKE), cwd=cwd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--python", type=Path, help="reuse this venv interpreter")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="sandloom-wheel-rpc-") as temp:
        root = Path(temp)
        if args.python is not None:
            run_smoke(args.python, root)
            return 0
        dist = root / "dist"
        _run("uv", "build", "--wheel", "--out-dir", str(dist), str(FRAMEWORK))
        wheels = sorted(dist.glob("sandloom-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one sandloom wheel, got {wheels}")
        venv = root / "venv"
        _run("uv", "venv", str(venv))
        _run("uv", "pip", "install", "--python", str(_python(venv)), str(wheels[0]))
        run_smoke(_python(venv), root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
