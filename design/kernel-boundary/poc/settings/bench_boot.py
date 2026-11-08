"""KB-49 P13: ``create_app`` wall time, base against candidate.

Each sample is a fresh interpreter that times ``import sandloom.main`` (the
standard root: ``create_app`` with the bundled modules), in an empty cwd with
the G-CONFIG-07 dev env. Rounds alternate: base A, candidate, base B,
candidate + fixture. Base A against base B is the noise floor.

    python bench_boot.py <base python> <candidate python> [rounds]
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = (
    "import time; t0 = time.perf_counter(); import sandloom.main; "
    "print(round((time.perf_counter() - t0) * 1000, 1))"
)
ENV = {
    "ENVIRONMENT": "dev",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/boot_checks_test",
    "CACHE_URL": "redis://localhost:6379/3",
    "BROKER_URL": "redis://localhost:6379/4",
    "JWT_SECRET": "boot-checks-not-a-real-secret-0123456789",
    "EVENT_BUS_BACKEND": "memory",
    "LOG_LEVEL": "error",
}
BUNDLED = ["sandloom_identity", "sandloom_tenancy", "sandloom_billing"]


def sample(py: str, modules: list[str]) -> float:
    import os

    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    env.update(ENV)
    env["SANDLOOM_MODULES"] = json.dumps(modules)
    env["PYTHONPATH"] = str(HERE / "fixture")
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run(
            [py, "-c", CODE], cwd=tmp, env=env, capture_output=True, text=True, check=True
        )
    return float(out.stdout.strip().splitlines()[-1])


def main() -> int:
    base, cand = sys.argv[1], sys.argv[2]
    rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    series: dict[str, list[float]] = {"base A": [], "candidate": [], "base B": [], "candidate + fixture": []}
    sample(base, BUNDLED), sample(cand, BUNDLED)  # warm the file cache
    for _ in range(rounds):
        series["base A"].append(sample(base, BUNDLED))
        series["candidate"].append(sample(cand, BUNDLED))
        series["base B"].append(sample(base, BUNDLED))
        series["candidate + fixture"].append(sample(cand, [*BUNDLED, "inventory"]))
    med = {k: statistics.median(v) for k, v in series.items()}
    for k, v in series.items():
        print(f"{k:22s} median {med[k]:7.1f} ms  min {min(v):7.1f}  max {max(v):7.1f}  runs {v}")
    noise = abs(med["base A"] - med["base B"])
    delta = med["candidate"] - (med["base A"] + med["base B"]) / 2
    print(f"noise |base A - base B| = {noise:.1f} ms; candidate - base = {delta:+.1f} ms; "
          f"fixture adds {med['candidate + fixture'] - med['candidate']:+.1f} ms")
    print("within noise" if abs(delta) <= max(noise, 0.02 * med["base A"]) else "BEYOND NOISE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
