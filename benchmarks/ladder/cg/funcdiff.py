"""Per-C-function instruction diff between two rungs' cg_annotate outputs.

    python ladder/cg/funcdiff.py <out>/L6.annot <out>/L6B.annot [K] [top]

Each ``.annot`` is ``cg_annotate`` of (K ops − 0 ops), so values / K are
instructions per op attributed to the C function that executed them
(CPython's eval loop, asyncpg's Cython protocol, the kernel-facing libc
calls, ...). Prints functions sorted by |B − A|.
"""

from __future__ import annotations

import re
import sys

LINE = re.compile(r"^\s*(-?[\d,]+)\s+\([^)]*\)\s+(.+?)\s*$")


def load(path: str) -> dict[str, int]:
    out: dict[str, int] = {}
    section = False
    for line in open(path):
        if (
            "file:function" in line
            or "function:file" in line
            or "-- Function summary" in line
        ):
            section = True
            continue
        if section and line.startswith("--") and "Function" not in line and out:
            break
        m = LINE.match(line)
        if section and m:
            name = m.group(2)
            out[name] = out.get(name, 0) + int(m.group(1).replace(",", ""))
    return out


def main() -> None:
    a, b = load(sys.argv[1]), load(sys.argv[2])
    k = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    top = int(sys.argv[4]) if len(sys.argv) > 4 else 30
    keys = set(a) | set(b)
    rows = sorted(keys, key=lambda n: -abs(b.get(n, 0) - a.get(n, 0)))
    ta, tb = sum(a.values()) / k, sum(b.values()) / k
    print(f"total instr/op: A {ta:,.0f}  B {tb:,.0f}  B-A {tb - ta:+,.0f}")
    print(f"{'A/op':>10} {'B/op':>10} {'B-A':>10}  function")
    for n in rows[:top]:
        va, vb = a.get(n, 0) / k, b.get(n, 0) / k
        print(f"{va:10,.0f} {vb:10,.0f} {vb - va:+10,.0f}  {n[:110]}")


if __name__ == "__main__":
    main()
