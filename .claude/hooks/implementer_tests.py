#!/usr/bin/env python3
"""Implementers write first and run the tests once at the end (the owner, 2026-11-23).

PreToolUse hook on Bash (.claude/settings.json). Inside a `sandloom-implementer`
subagent it refuses the runs that the agent file forbids, so the rule holds even
where a prompt says otherwise:

- `sandloom test --full` (the orchestrator runs it once per wave);
- a pytest with no test path (the whole suite, serially).

A command carrying `SANDLOOM_RUN_OK=1` passes: the one declared exception, which
the agent justifies in its report. Outside implementers the hook does nothing.
"""

import json
import re
import shlex
import sys

AGENT = "sandloom-implementer"
ESCAPE = "SANDLOOM_RUN_OK=1"
WHY = (
    " — implementers write the whole task first, then run the tests of what"
    " they touched once (.claude/agents/sandloom-implementer.md, 'Checks')."
    f" A whole-suite run you truly need: put {ESCAPE} in the command and say"
    " why in your report."
)


def _pytest_has_path(cmd: str) -> bool:
    try:
        words = shlex.split(cmd)
    except ValueError:
        return True  # unparsable: do not guess, let it run
    for i, word in enumerate(words):
        if word == "pytest" or word.endswith("/pytest"):
            rest = words[i + 1 :]
            break
    else:
        return True
    skip_next = {
        "-k",
        "-m",
        "-n",
        "-p",
        "-c",
        "-o",
        "--maxfail",
        "--tb",
        "--dist",
        "--durations",
        "--rootdir",
    }
    expect_value = False
    for word in rest:
        if word in {"|", "&&", ";", "||"}:
            break
        if expect_value:
            expect_value = False
            continue
        if word in skip_next or word.endswith((">", "<")):
            expect_value = True  # its value, or the redirection's target
            continue
        if word.startswith("-") or ">" in word or "<" in word:
            continue  # an option or a redirection
        return True  # a positional argument: a path or node id
    return False


def decide(call: dict) -> str | None:
    if (call.get("agent_type") or "") != AGENT:
        return None
    cmd = (call.get("tool_input") or {}).get("command") or ""
    if ESCAPE in cmd:
        return None
    if re.search(r"\bsandloom\s+test\b[^|;&]*--full\b", cmd):
        return "`sandloom test --full` refused" + WHY
    if re.search(r"(^|[\s/])pytest(\s|$)", cmd) and not _pytest_has_path(cmd):
        return "pytest without test paths refused" + WHY
    return None


def main() -> None:
    reason = decide(json.load(sys.stdin))
    if reason:
        print(reason, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
