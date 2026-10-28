"""Which locked packages have NO prebuilt Linux x86_64 wheel for a Python tag.

Used by the workflow to explain an install failure ("what blocks 3.14 /
3.14t"): a package listed here must build from source on that interpreter.
Stdlib only, so it runs before the environment is installed.

    python benchmarks/benchsuite/wheels.py cp314t
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

LOCK = Path(__file__).resolve().parents[1] / "uv.lock"


def _compatible(filename: str, tag: str) -> bool:
    py, abi, plat = filename[:-4].split("-")[-3:]
    if "x86_64" not in plat or "linux" not in plat or "musllinux" in plat:
        return False
    if tag.endswith("t"):  # free-threaded: abi3 wheels do not apply
        return abi == tag
    if abi == tag:
        return True
    if abi == "abi3":
        lowest = int(py.split(".")[0][2:])
        return lowest <= int(tag[2:])
    return False


def missing_wheels(tag: str, lock: Path = LOCK) -> list[str]:
    data = tomllib.loads(lock.read_text())
    out = []
    for pkg in data.get("package", []):
        wheels = [w["url"].rsplit("/", 1)[1] for w in pkg.get("wheels", [])]
        if not wheels or any(w.endswith("-none-any.whl") for w in wheels):
            continue
        if not any(_compatible(w, tag) for w in wheels):
            out.append(f"{pkg['name']}=={pkg['version']}")
    return out


def main(argv: list[str]) -> int:
    tag = argv[1] if len(argv) > 1 else f"cp{sys.version_info[0]}{sys.version_info[1]}"
    missing = missing_wheels(tag)
    print(", ".join(missing) if missing else "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
