"""Build the benchmark job matrix from the workflow_dispatch inputs (stdlib only).

One job per (python, server) when every locked package has a prebuilt wheel
for that interpreter — implementations are compared WITHIN a job (same VM).
A Python whose lock has packages without a wheel (typically the newest
CPython / free-threaded builds) gets ONE ``probe`` job instead: it attempts
the real install (source builds included), reports exactly what blocks, and —
if the install works — runs a reduced server set.

    python benchmarks/ci/plan.py --python 3.12,3.13,3.14,3.14t --servers ...

Writes ``matrix``, ``scaling_python`` and ``summary`` to ``$GITHUB_OUTPUT``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchsuite"))
from wheels import missing_wheels  # noqa: E402

PROBE_SERVERS = "uvicorn-uvloop,granian-uvloop"


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def tag_for(python: str) -> str:
    free = python.endswith("t")
    major, minor = python.rstrip("t").split(".")[:2]
    return f"cp{major}{minor}{'t' if free else ''}"


def plan(pythons: list[str], servers: list[str]) -> dict[str, object]:
    include: list[dict[str, str]] = []
    full: list[str] = []
    notes: list[str] = []
    for py in pythons:
        missing = missing_wheels(tag_for(py))
        if not missing:
            full.append(py)
            include += [
                {"python": py, "server": s, "label": f"py{py}/{s}", "probe": "false"}
                for s in servers
            ]
        else:
            probe = (
                ",".join(s for s in _csv(PROBE_SERVERS) if s in servers) or servers[0]
            )
            include.append(
                {
                    "python": py,
                    "server": probe,
                    "label": f"py{py}/probe",
                    "probe": "true",
                    "missing": ", ".join(missing),
                }
            )
            notes.append(
                f"- py{py}: no prebuilt wheel for {', '.join(missing)} "
                f"→ one probe job (source builds attempted; servers {probe})"
            )
    scaling = next((p for p in reversed(full) if not p.endswith("t")), "")
    return {"matrix": {"include": include}, "scaling_python": scaling, "notes": notes}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--python", required=True)
    p.add_argument("--servers", required=True)
    args = p.parse_args()
    result = plan(_csv(args.python), _csv(args.servers))
    print(json.dumps(result, indent=2))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"matrix={json.dumps(result['matrix'])}\n")
            fh.write(f"scaling_python={result['scaling_python']}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and result["notes"]:
        with open(summary, "a") as fh:
            fh.write("### Benchmark plan\n\n" + "\n".join(result["notes"]) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
