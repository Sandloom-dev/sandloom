#!/usr/bin/env python3
"""Implementers write first and run the tests once at the end (the owner, 2026-11-23).

PreToolUse hook on Bash (.claude/settings.json). Inside a `sandloom-implementer`
subagent it refuses the runs that the agent file forbids, so the rule holds even
where a prompt says otherwise:

- `sandloom test --full` (the orchestrator runs it once per wave);
- a pytest with no test selection (the whole suite, serially). A selection is a
  path or node id (`tests/x.py`, `a/b::test_c`, an existing path) or a rerun of
  the last failures (`--lf`, `--ff`, `--nf`, `--sw`).

Each segment of a command (split on `&&`, `||`, `;`, `|`) is judged on its own,
and only a segment that RUNS pytest or `sandloom test` (after `env`, `VAR=value`,
`uv run`, `python -m`): a `grep` for the words passes. A command carrying
`SANDLOOM_RUN_OK=1` passes: the one declared exception, which the agent justifies
in its report. Outside implementers the hook does nothing.
"""

import json
import os
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
SEPARATORS = {"&&", "||", ";", "|", "&"}
RERUN = {"--lf", "--last-failed", "--ff", "--failed-first", "--nf", "--sw"}
# options whose value is the next word (the `--opt=value` form is one word)
VALUED = {
    "-k", "-m", "-n", "-p", "-c", "-o", "-W", "-r",
    "--maxfail", "--tb", "--dist", "--durations", "--rootdir", "--ignore",
    "--ignore-glob", "--deselect", "--junitxml", "--junit-xml", "--basetemp",
    "--confcutdir", "--cov", "--cov-report", "--log-level", "--timeout",
    "--import-mode", "--capture", "--color", "--override-ini", "--maxprocesses",
}  # fmt: skip


def _segments(cmd: str) -> list[list[str]]:
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    out: list[list[str]] = [[]]
    for word in lexer:
        if word in SEPARATORS or set(word) <= set(";&|"):
            out.append([])
        else:
            out[-1].append(word)
    return [s for s in out if s]


def _program(words: list[str]) -> list[str]:
    """The words from the program actually run: past env, VAR=x, uv run, python -m."""
    i = 0
    while i < len(words):
        w = words[i]
        if w == "env" or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w):
            i += 1
        elif w == "uv" and i + 1 < len(words) and words[i + 1] == "run":
            i += 2
            while i < len(words) and words[i].startswith("-"):
                i += 1  # uv run's own flags (--locked, --quiet)
        elif re.fullmatch(r"python3?(\.\d+)?", w) and words[i + 1 : i + 2] == ["-m"]:
            i += 2
        else:
            break
    return words[i:]


def _is_selection(word: str) -> bool:
    path = word.split("::", 1)[0]
    return "::" in word or path.endswith(".py") or os.path.exists(path)


def _pytest_selects(args: list[str]) -> bool:
    expect_value = False
    for word in args:
        if expect_value:
            expect_value = False
            continue
        if word in RERUN:
            return True
        if word in VALUED or word.endswith((">", "<")):
            expect_value = True  # its value, or the redirection's target
            continue
        if word.startswith("-") or ">" in word or "<" in word:
            continue
        if _is_selection(word):
            return True
    return False


def decide(call: dict) -> str | None:
    if (call.get("agent_type") or "") != AGENT:
        return None
    tool_input = call.get("tool_input")
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(cmd, str) or ESCAPE in cmd:
        return None
    try:
        segments = _segments(cmd)
    except ValueError:
        return None  # unparsable: do not guess, let it run
    for words in segments:
        prog = _program(words)
        if not prog:
            continue
        name = os.path.basename(prog[0])
        if name == "sandloom" and prog[1:2] == ["test"] and "--full" in prog:
            return "`sandloom test --full` refused" + WHY
        if name == "pytest" and not _pytest_selects(prog[1:]):
            return "pytest without test paths refused" + WHY
    return None


def main() -> None:
    try:
        call = json.load(sys.stdin)
    except ValueError:
        return
    if isinstance(call, dict):
        reason = decide(call)
        if reason:
            print(reason, file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
