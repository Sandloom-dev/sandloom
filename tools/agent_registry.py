#!/usr/bin/env python3
"""Make the routine edits to the shared registries without opening them.

An agent opens the big shared files mostly for three mechanical jobs. Each one
is a command here, so the agent runs it and does not have to read the file:

``classify PATH... [--exclude-file REASON]``
    Put new tracked files into ``tools/packaging/public-source.json``. Each path
    goes beside its nearest sibling (the classified path with the longest common
    prefix), in that sibling's ``include`` group. With ``--exclude-file`` the paths
    go to ``exclude_files`` with the reason instead. A path that is already
    classified, directly or under ``exclude_trees``, is left alone.
``next-id PREFIX [--bump]``
    Print the next free id. ``SANDLOOM-C`` gives the next ``SANDLOOM-C0NN`` in
    ``src/sandloom/core/error_codes.py``. ``G-AUTHZ`` or ``C-AUTH`` gives the next
    id from ``issued:`` in ``guarantees.yml``, and ``--bump`` raises ``issued:`` to
    it.
``edges-summary [--check]``
    Recount ``summary:`` of ``design/kernel-boundary/edges.yml`` (total, kind,
    current_status, target, by_adr) after a row was flipped, as
    ``tests/architecture/test_kernel_edges.py`` counts it.

Standard library and PyYAML. ``.claude/agent-reference.md`` names each command.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
PUBLIC_SOURCE = "tools/packaging/public-source.json"
ERROR_CODES = "src/sandloom/core/error_codes.py"
GUARANTEES = "guarantees.yml"
EDGES = "design/kernel-boundary/edges.yml"


# --------------------------------------------------------------- classify


def _common(a: str, b: str) -> tuple[int, int]:
    """How close two paths are: shared directory parts, then shared characters."""
    parts = 0
    for x, y in zip(a.split("/"), b.split("/"), strict=False):
        if x != y:
            break
        parts += 1
    chars = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        chars += 1
    return parts, chars


def _classified(policy: dict[str, Any], path: str) -> str | None:
    """Where ``path`` is already classified, or None."""
    for group in policy["include"]:
        if path in group["paths"]:
            return f"include ({group['reason']})"
    if path in policy["exclude_files"]:
        return "exclude_files"
    for tree in policy["exclude_trees"]:
        if path == tree or path.startswith(tree.rstrip("/") + "/"):
            return f"exclude_trees {tree}"
    return None


def classify(root: Path, paths: list[str], exclude_reason: str | None) -> list[str]:
    """Add ``paths`` to the policy file at ``root``; one report line per path."""
    file = root / PUBLIC_SOURCE
    policy = json.loads(file.read_text(encoding="utf-8"))
    report = []
    for path in paths:
        path = path.removeprefix("./")
        where = _classified(policy, path)
        if where is not None:
            report.append(f"{path}: already classified ({where})")
            continue
        if exclude_reason is not None:
            policy["exclude_files"][path] = exclude_reason
            policy["exclude_files"] = dict(sorted(policy["exclude_files"].items()))
            report.append(f"{path}: exclude_files")
            continue
        best: tuple[tuple[int, int], int, int] | None = None
        for g, group in enumerate(policy["include"]):
            for i, sibling in enumerate(group["paths"]):
                score = _common(path, sibling)
                if best is None or score > best[0]:
                    best = (score, g, i)
        if best is None:
            raise SystemExit("agent_registry: public-source.json has no include group")
        _, g, i = best
        group = policy["include"][g]
        siblings = group["paths"]
        # Beside the sibling, on the side the name sorts to.
        at = i + 1 if path > siblings[i] else i
        siblings.insert(at, path)
        report.append(
            f"{path}: include, beside {siblings[i if at > i else i + 1]}"
            f" ({group['reason']})"
        )
    file.write_text(
        json.dumps(policy, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report


# ---------------------------------------------------------------- next-id


def next_error_code(root: Path, prefix: str) -> str:
    """The next free ``SANDLOOM-<K>NNN`` after the highest one of that kind."""
    text = (root / ERROR_CODES).read_text(encoding="utf-8")
    numbers = [int(n) for n in re.findall(rf'"{re.escape(prefix)}(\d{{3}})"', text)]
    if not numbers:
        raise SystemExit(f"agent_registry: no {prefix}NNN code in {ERROR_CODES}")
    return f"{prefix}{max(numbers) + 1:03d}"


def next_guarantee(root: Path, prefix: str, bump: bool) -> tuple[str, str]:
    """The next ``G-``/``C-<AREA>-NN`` id and what to do with ``issued:``."""
    file = root / GUARANTEES
    text = file.read_text(encoding="utf-8")
    issued = yaml.safe_load(text).get("issued") or {}
    current = issued.get(prefix)
    new = (current or 0) + 1
    ident = f"{prefix}-{new:02d}"
    if current is None:
        note = f"issued: has no {prefix}; add `  {prefix}: {new}` under `issued:`"
        if bump:
            raise SystemExit(f"agent_registry: {note} by hand (a new area)")
        return ident, note
    line = re.compile(rf"^(  {re.escape(prefix)}: ){current}$", re.MULTILINE)
    if bump:
        updated, count = line.subn(rf"\g<1>{new}", text)
        if count != 1:
            raise SystemExit(
                f"agent_registry: expected one `  {prefix}: {current}` line"
            )
        file.write_text(updated, encoding="utf-8")
        return ident, f"issued: {prefix} {current} -> {new} (written)"
    return (
        ident,
        f"issued: {prefix} {current} -> {new} (--bump writes it)",
    )


# ---------------------------------------------------------- edges-summary


def _flow(counts: dict[str, int]) -> str:
    return "{" + ", ".join(f"{k}: {v}" for k, v in counts.items()) + "}"


def edges_summary(root: Path, write: bool) -> list[str]:
    """Recount ``summary:``; return the lines that differ (written when ``write``)."""
    file = root / EDGES
    text = file.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    edges = data["edges"]
    old = data["summary"]
    counted = {
        "kind": Counter(e.get("kind", "runtime") for e in edges),
        "current_status": Counter(e["current_status"] for e in edges),
        "target": Counter(e["target"] for e in edges),
        "by_adr": Counter(e["adr"] for e in edges),
    }
    changes = []
    lines = text.splitlines(keepends=True)
    start = next(i for i, ln in enumerate(lines) if ln.startswith("summary:"))
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if not ln.startswith("  "):
            break
        key = ln.strip().split(":", 1)[0]
        if key == "total":
            new = len(edges)
            if old["total"] != new:
                comment = ln[ln.index("#") :].rstrip("\n") if "#" in ln else ""
                lines[i] = (
                    f"  total: {new}" + (f"  {comment}" if comment else "") + "\n"
                )
                changes.append(f"total: {old['total']} -> {new}")
        elif key in counted:
            # Keep the file's order; new values go last; a count of 0 drops out.
            order = [k for k in old[key] if counted[key][k]]
            order += sorted(k for k in counted[key] if k not in order)
            new_counts = {k: counted[key][k] for k in order}
            if new_counts != old[key]:
                lines[i] = f"  {key}: {_flow(new_counts)}\n"
                changes.append(f"{key}: {_flow(old[key])} -> {_flow(new_counts)}")
    if write and changes:
        file.write_text("".join(lines), encoding="utf-8")
    return changes


# ------------------------------------------------------------------- main


def main(argv: list[str] | None = None, root: Path = REPO) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("classify", help=f"add tracked files to {PUBLIC_SOURCE}")
    c.add_argument("paths", nargs="+")
    c.add_argument("--exclude-file", metavar="REASON")
    n = sub.add_parser("next-id", help="next SANDLOOM-<K> code or G-/C-<AREA> id")
    n.add_argument("prefix", help="SANDLOOM-C, SANDLOOM-B, G-AUTHZ, C-AUTH, ...")
    n.add_argument("--bump", action="store_true", help="write the new issued: number")
    e = sub.add_parser("edges-summary", help=f"recount summary: of {EDGES}")
    e.add_argument("--check", action="store_true", help="exit 1 when it is stale")
    args = parser.parse_args(argv)

    if args.command == "classify":
        print("\n".join(classify(root, args.paths, args.exclude_file)))
        print("check: python3 tools/packaging/export_repository.py --check")
        return 0
    if args.command == "next-id":
        prefix = args.prefix.rstrip("-")
        if prefix.startswith("SANDLOOM-"):
            if args.bump:
                raise SystemExit("agent_registry: --bump is for G-/C- ids")
            print(next_error_code(root, prefix))
            return 0
        if re.fullmatch(r"[GC]-[A-Z]+", prefix):
            ident, note = next_guarantee(root, prefix, args.bump)
            print(ident)
            print(note)
            return 0
        raise SystemExit(f"agent_registry: unknown prefix {args.prefix!r}")
    changes = edges_summary(root, write=not args.check)
    for line in changes:
        print(line)
    if args.check:
        return 1 if changes else 0
    print("summary: written" if changes else "summary: current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
