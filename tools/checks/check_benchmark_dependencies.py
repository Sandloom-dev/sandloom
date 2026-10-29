"""Keep the Cachegrind image's dependencies identical to the benchmark lock.

Run from any directory with the repository's pinned uv on PATH. The default
checks only; --write refreshes the export after an intentional lock update.
"""

from __future__ import annotations

import argparse
import difflib
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "benchmarks/ladder/cg/requirements.txt"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="refresh the locked export"
    )
    args = parser.parse_args()
    uv = shutil.which("uv")
    if uv is None:
        print("uv is required; use the version pinned in CI.", file=sys.stderr)
        return 1
    command = [
        uv,
        "export",
        "--project",
        str(ROOT / "benchmarks"),
        "--locked",
        "--no-dev",
        "--no-emit-project",
        "--no-hashes",
        "--no-header",
        "--no-annotate",
    ]
    for manifest in sorted((ROOT / "packages").glob("*/pyproject.toml")):
        name = tomllib.loads(manifest.read_text())["project"]["name"]
        command.extend(("--no-emit-package", name))
    result = subprocess.run(  # noqa: S603 - trusted uv and checked-in metadata; no shell
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode:
        print(result.stderr, file=sys.stderr, end="")
        return result.returncode
    if args.write:
        TARGET.write_text(result.stdout)
        print(f"Updated {TARGET.relative_to(ROOT)} from benchmarks/uv.lock")
        return 0
    current = TARGET.read_text()
    if current != result.stdout:
        print(
            "Cachegrind requirements are stale. Run "
            "python tools/checks/check_benchmark_dependencies.py --write",
            file=sys.stderr,
        )
        sys.stderr.writelines(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                result.stdout.splitlines(keepends=True),
                fromfile=str(TARGET.relative_to(ROOT)),
                tofile="benchmarks/uv.lock export",
            )
        )
        return 1
    print("Benchmark lock and Cachegrind requirements match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
