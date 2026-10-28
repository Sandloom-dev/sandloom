"""Report an interpreter whose environment could not be installed (stdlib only).

Writes a "blocked" section to ``$GITHUB_STEP_SUMMARY`` and prints a BENCHJSON
document so the report job lists the blocker next to the results.

    python3 benchmarks/ci/blocked.py <label> <python> <install-log> [missing-wheels]
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

CHUNK = 6000
_INTERESTING = re.compile(
    r"(error|failed|Failed|Caused by|hint:|help:|unsupported|not supported|"
    r"PyO3|Cython|requires)",
)


def main(argv: list[str]) -> int:
    label, python, log_path = argv[1], argv[2], Path(argv[3])
    missing = argv[4] if len(argv) > 4 else ""
    log = log_path.read_text(errors="replace") if log_path.exists() else ""
    lines = log.splitlines()
    key_lines = [ln for ln in lines if _INTERESTING.search(ln)][:25] or lines[-25:]
    reason = (
        (f"no prebuilt wheel for: {missing}. " if missing else "")
        + "install failed: "
        + " ⏎ ".join(ln.strip() for ln in key_lines)[:1500]
    )
    doc = {
        "meta": {"label": label, "python": python},
        "results": [],
        "failures": [],
        "blocked": reason,
    }
    raw = json.dumps(doc, separators=(",", ":"))
    chunks = [raw[i : i + CHUNK] for i in range(0, len(raw), CHUNK)]
    for i, chunk in enumerate(chunks, 1):
        print(f"BENCHJSON {label} {i}/{len(chunks)} {chunk}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(f"### {label}: environment install BLOCKED\n\n")
            if missing:
                fh.write(f"Packages without a prebuilt wheel: `{missing}`\n\n")
            fh.write("```text\n" + "\n".join(lines[-60:]) + "\n```\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
