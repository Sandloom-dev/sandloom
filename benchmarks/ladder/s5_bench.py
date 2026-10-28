"""Measure the S5 secure-read variants of ANOTHER checkout (read-only use).

    S5_DIR=<checkout>/benchmarks uv run python ladder/s5_bench.py [rates]

For each variant (uow|ro|fast|fn) it takes the server env and a tenant
token from ``python -m benchsuite.s5 env|token`` of that checkout, starts its
Sandloom app on uvicorn (uvloop + httptools, 1 worker) with that checkout's venv,
and drives it with ``server_bench.py`` (conc 64 closed loop; then open loop at
the given rates). Results append to ``ladder/results/s5.jsonl`` and
``s5_latency.jsonl``. Nothing in the other checkout is modified.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
S5 = Path(os.environ["S5_DIR"])
VARIANTS = {
    "uow": "/s5/",
    "ro": "/s5ro/",
    "fast": "/s5fast/",
    "fn": "/s5fn/",
}


def s5(*args: str) -> str:
    return subprocess.run(
        ["uv", "--directory", str(S5), "run", "python", "-m", "benchsuite.s5", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def run(variant: str, extra: list[str], out: str) -> None:
    env_file = Path(tempfile.gettempdir()) / f"s5-{variant}.env"
    env_file.write_text(s5("env", variant))
    token = s5("token").strip()
    cmd = [
        sys.executable,
        str(HERE / "server_bench.py"),
        "--target",
        "benchsuite.apps.sandloom_app:app",
        "--cwd",
        str(S5),
        "--python",
        str(S5 / ".venv/bin/python"),
        "--env-file",
        str(env_file),
        "--header",
        f"Authorization: Bearer {token}",
        "--ids",
        "5000",
        "--path",
        VARIANTS[variant],
        *extra,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.startswith("{"):
            rec = {"variant": variant, **json.loads(line)}
            with (HERE / "results" / out).open("a") as f:
                f.write(json.dumps(rec) + "\n")
            print(variant, {k: rec[k] for k in list(rec)[:16]}, flush=True)
    if r.returncode:
        print(r.stderr[-1500:], file=sys.stderr)


def main() -> None:
    rates = sys.argv[1] if len(sys.argv) > 1 else ""
    for v in VARIANTS:
        if not rates:
            run(v, ["--conc", "64", "--n", "12800", "--reps", "3"], "s5.jsonl")
        else:
            run(v, ["--capacity", rates, "--seconds", "20"], "s5_latency.jsonl")


if __name__ == "__main__":
    main()
