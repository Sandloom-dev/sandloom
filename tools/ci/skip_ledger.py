#!/usr/bin/env python3
"""Merge the skip ledgers of the CI lanes; fail on a test that runs nowhere.

Each test lane writes a ledger (``-p sandloom.core.testing.skip_ledger
--sandloom-skip-ledger=<file>``): the tests it ran and the tests it skipped,
with the reasons. PostgreSQL is the mandatory lane and SQLite the second one
(``second-db-test-lane``); a test may skip on one lane because its database
lacks a feature (``skip_unless_db_feature``), but a test skipped on every lane
that collected it runs nowhere, and this command fails with its name and each
lane's reason. Sharded runs of one lane are simply more ledgers: a test is
judged by every ledger that holds it.

    python3 tools/ci/skip_ledger.py ledgers/*.json \\
        --allowlist tools/ci/skip_ledger_allowlist.json \\
        --require-lanes postgresql,sqlite

The allowlist names the tests that are skipped everywhere on purpose (they
need something CI does not have), each with a reason: ``{"entries": [{"test":
"<key, * matching any text>", "reason": "..."}]}``. An entry without a reason,
or one whose test ran on a lane, fails too, so the list shrinks with the tests
it excuses. An entry whose tests no lane collected (a slow suite the PR
profile deselects) is not judged, unless ``--strict-allowlist`` (full
validation, where every suite is collected).

Standard library only: the CI job runs it without a virtualenv.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

LEDGER_VERSION = 1


@dataclass
class Merged:
    lanes: dict[str, int] = field(default_factory=dict)
    ran: set[str] = field(default_factory=set)
    #: key -> {lane: reason}
    skipped: dict[str, dict[str, str]] = field(default_factory=dict)

    def nowhere(self) -> dict[str, dict[str, str]]:
        """Tests skipped on some lane and run on none. A module skipped at
        collection (a key without ``::``) ran when any of its tests ran."""
        modules = {k.split("::", 1)[0] for k in self.ran}
        return {
            k: v
            for k, v in sorted(self.skipped.items())
            if k not in self.ran and ("::" in k or k not in modules)
        }


def _matches(key: str, pattern: str) -> bool:
    """``*`` matches any text; everything else, brackets included (node ids
    carry parameters), is literal."""
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, key) is not None


def load(paths: list[Path]) -> Merged:
    merged = Merged()
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
            raise ValueError(f"{path}: not a version {LEDGER_VERSION} skip ledger")
        lane = str(data.get("lane") or "default")
        merged.lanes[lane] = merged.lanes.get(lane, 0) + 1
        merged.ran.update(str(k) for k in data.get("ran", []))
        for key, reason in dict(data.get("skipped", {})).items():
            merged.skipped.setdefault(str(key), {})[lane] = str(reason)
    return merged


def load_allowlist(path: Path | None) -> tuple[list[tuple[str, str]], list[str]]:
    """``([(pattern, reason)], problems)``."""
    if path is None:
        return [], []
    data = json.loads(path.read_text(encoding="utf-8"))
    entries: list[tuple[str, str]] = []
    problems: list[str] = []
    for entry in data.get("entries", []) if isinstance(data, dict) else []:
        test = str(entry.get("test", "")).strip() if isinstance(entry, dict) else ""
        reason = str(entry.get("reason", "")).strip() if isinstance(entry, dict) else ""
        if not test or not reason:
            problems.append(f"allowlist entry without a test or a reason: {entry!r}")
            continue
        entries.append((test, reason))
    return entries, problems


def judge(
    merged: Merged,
    allowlist: list[tuple[str, str]],
    *,
    required_lanes: list[str],
    strict: bool = False,
) -> tuple[list[str], list[str]]:
    """``(failures, notes)``: the lines to print. ``strict``: an allowlist
    entry no lane collected fails too (a full validation collects every
    suite, so such an entry names a test that is gone)."""
    failures = [
        f"no ledger from the {lane} lane"
        for lane in required_lanes
        if lane not in merged.lanes
    ]
    notes: list[str] = []
    used: set[str] = set()
    for key, reasons in merged.nowhere().items():
        excused = next(
            (
                (pattern, why)
                for pattern, why in allowlist
                if key == pattern or _matches(key, pattern)
            ),
            None,
        )
        by_lane = "; ".join(f"{lane}: {why}" for lane, why in sorted(reasons.items()))
        if excused is not None:
            used.add(excused[0])
            notes.append(f"allowed: {key} ({excused[1]}) [{by_lane}]")
        else:
            failures.append(f"skipped on every lane: {key} [{by_lane}]")
    for pattern, why in allowlist:
        if pattern in used:
            continue
        ran = [k for k in merged.ran if k == pattern or _matches(k, pattern)]
        if ran:
            failures.append(
                f"allowlist entry names a test that ran (remove it): "
                f"{pattern} ({why}) [ran: {ran[0]}]"
            )
        elif strict:
            failures.append(
                f"allowlist entry matches no test any lane collected "
                f"(--strict-allowlist; remove it): {pattern} ({why})"
            )
    return failures, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ledgers", nargs="+", type=Path)
    parser.add_argument("--allowlist", type=Path)
    parser.add_argument(
        "--require-lanes",
        default="",
        help="comma-separated lanes that must have a ledger (e.g. postgresql,sqlite)",
    )
    parser.add_argument(
        "--strict-allowlist",
        action="store_true",
        help="an allowlist entry no lane collected fails (full validation, "
        "where every suite is collected)",
    )
    args = parser.parse_args(argv)
    try:
        merged = load(args.ledgers)
        allowlist, problems = load_allowlist(args.allowlist)
    except (OSError, ValueError) as exc:
        print(f"skip ledger: {exc}", file=sys.stderr)
        return 2
    required = [lane for lane in args.require_lanes.split(",") if lane.strip()]
    failures, notes = judge(
        merged, allowlist, required_lanes=required, strict=args.strict_allowlist
    )
    failures = problems + failures
    lanes = ", ".join(f"{lane} ({n})" for lane, n in sorted(merged.lanes.items()))
    print(
        f"skip ledger: {len(args.ledgers)} ledger(s), lanes {lanes}; "
        f"{len(merged.ran)} tests ran somewhere, {len(merged.skipped)} skipped "
        f"somewhere, {len(merged.nowhere())} run nowhere"
    )
    for line in notes:
        print(f"  {line}")
    for line in failures:
        print(f"  FAIL {line}")
    if failures:
        print(
            "skip ledger: FAILED. A test skipped on every lane runs nowhere: "
            "make it run on one lane (PostgreSQL runs everything production "
            "has), or add it to the allowlist with the reason CI cannot run it."
        )
        return 1
    print("skip ledger: OK (every skipped test runs on another lane)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
