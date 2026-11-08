"""M-boot: ``create_app`` and CLI wall time, base against candidate.

The migration plan's boot measurement (``design/kernel-boundary/migration-plan.md``,
M-boot; ADR-0015 P13). Each sample is a fresh interpreter in an empty working
directory with an explicit environment, timed from spawn to exit:

- ``create_app``: ``import sandloom.main`` (the standard composition root
  builds the app at import) with identity, tenancy and billing;
- ``cli``: ``sandloom config dump --json`` (the kernel settings load and the
  CLI's import path).

Rounds alternate base A, candidate, base B, candidate + the settings fixture
module (``tests/fixtures/settings_modules/kb_inventory``), so load drift hits
every series alike. The base runs the source tree given by ``--base-src``
first on ``PYTHONPATH`` (an export of the base commit's ``src/``), on the same
interpreter and dependencies as the candidate. The verdict compares the
candidate's median with base-against-base noise (``|median A - median B|``).

Usage (repository root; nothing else heavy running)::

    git archive --format=tar --output=/tmp/base.tar <base-ref> src
    mkdir /tmp/base && tar -xf /tmp/base.tar -C /tmp/base
    uv run python benchmarks/boot/m_boot.py --base-src /tmp/base/src --runs 10
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "settings_modules"

BASE_ENV: dict[str, str] = {
    "ENVIRONMENT": "dev",
    "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:9/m_boot",
    "CACHE_URL": "redis://127.0.0.1:9/0",
    "BROKER_URL": "redis://127.0.0.1:9/1",
    "JWT_SECRET": "m-boot-not-a-real-secret-0123456789abcdef",
    "TWOFA_ENCRYPTION_KEY": "m-boot-not-a-real-key-0123456789abcdef0123",
    "EVENT_BUS_BACKEND": "memory",
    "LOG_LEVEL": "ERROR",
    "SANDLOOM_MODULES": json.dumps(
        ["sandloom_identity", "sandloom_tenancy", "sandloom_billing"]
    ),
}

COMMANDS: dict[str, list[str]] = {
    "create_app": [sys.executable, "-c", "import sandloom.main"],
    "cli": [
        sys.executable,
        "-c",
        "import sys; sys.argv = ['sandloom', 'config', 'dump', '--json']; "
        "import sandloom; sandloom.main()",
    ],
}

SERIES = ("base A", "candidate", "base B", "candidate + fixture")


def _env(series: str, base_src: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR") if k in os.environ}
    env.update(BASE_ENV)
    paths = []
    if series.startswith("base"):
        paths.append(str(base_src))
    if series.endswith("fixture"):
        paths.append(str(FIXTURES))
        modules = [*json.loads(BASE_ENV["SANDLOOM_MODULES"]), "kb_inventory"]
        env["SANDLOOM_MODULES"] = json.dumps(modules)
    if paths:
        env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def _sample(command: list[str], env: dict[str, str]) -> float:
    with tempfile.TemporaryDirectory() as cwd:
        start = time.perf_counter()
        proc = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True, check=False
        )
        elapsed = (time.perf_counter() - start) * 1000
    if proc.returncode != 0:
        raise SystemExit(f"{command[-1]!r} failed:\n{proc.stderr[-2000:]}")
    return elapsed


def measure(base_src: Path, runs: int) -> dict[str, dict[str, list[float]]]:
    out: dict[str, dict[str, list[float]]] = {
        name: {s: [] for s in SERIES} for name in COMMANDS
    }
    for _ in range(2):  # warm the file cache for every series, not counted
        for command in COMMANDS.values():
            for series in SERIES:
                _sample(command, _env(series, base_src))
    for _ in range(runs):
        for name, command in COMMANDS.items():
            for series in SERIES:
                out[name][series].append(_sample(command, _env(series, base_src)))
    return out


def report(results: dict[str, dict[str, list[float]]]) -> str:
    lines = []
    for name, by_series in results.items():
        lines.append(f"{name} (ms, n={len(by_series['base A'])} per series)")
        medians = {s: statistics.median(v) for s, v in by_series.items()}
        for series, values in by_series.items():
            lines.append(
                f"  {series:<20} median {medians[series]:8.1f}  "
                f"min {min(values):8.1f}  max {max(values):8.1f}"
            )
        noise = abs(medians["base A"] - medians["base B"])
        base = statistics.median(by_series["base A"] + by_series["base B"])
        delta = medians["candidate"] - base
        fixture = medians["candidate + fixture"] - medians["candidate"]
        verdict = "within noise" if abs(delta) <= noise else "BEYOND noise"
        lines.append(
            f"  noise |A-B| {noise:.1f}; candidate - base {delta:+.1f}; "
            f"fixture adds {fixture:+.1f}: {verdict}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base-src", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--json", type=Path, help="also write the samples here")
    args = parser.parse_args(argv)
    results = measure(args.base_src.resolve(), args.runs)
    if args.json:
        args.json.write_text(json.dumps(results, indent=2))
    print(report(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
