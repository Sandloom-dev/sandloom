#!/usr/bin/env python3
"""The model of a fleet agent comes from its agent type, never from the call.

PreToolUse hook on Agent/Task (.claude/settings.json). The fleet has four
types, each with its model fixed in its frontmatter (the person, 2026-11-21):
the implementer (`*-porter`, `*-implementer`) always Sonnet, the reviewer (`*-reviewer`)
always Opus, the light reviewer (`*-light-reviewer`) Sonnet, the helper
(`*-waiter`) Haiku.

- a `model` passed on the call is refused: the type decides it;
- general-purpose, claude and Plan are refused: they inherit the
  orchestrator's Opus and its whole tool list (Explore stays allowed for a
  read-only search);
- `effort` stays free (xhigh for complex slices).
Same script in rtwk, CodexCLI and sandloom.
"""
import json
import sys
from typing import Any
from pathlib import Path

AGENTS = Path(__file__).resolve().parent.parent / "agents"
PLAIN = {"general-purpose", "claude", "Plan", ""}


def decide(call: dict[str, Any]) -> str | None:
    if call.get("tool_name") not in ("Agent", "Task"):
        return None
    inp = call.get("tool_input") or {}
    kind = inp.get("subagent_type") or ""
    if kind == "fork":
        return None
    types = ", ".join(sorted(p.stem for p in AGENTS.glob("*.md")))
    if inp.get("model"):
        return (f"No `model` on an Agent call: the agent type fixes it (implementer Sonnet, reviewer Opus, "
                f"light reviewer Sonnet, waiter Haiku). Drop `model` and pick the type: {types}. `effort` stays free.")
    if kind in PLAIN:
        return (f"`{kind or 'general-purpose'}` inherits the orchestrator's Opus. Use a fleet type ({types}) "
                f"or Explore for a read-only search.")
    return None


def main() -> None:
    reason = decide(json.load(sys.stdin))
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}))


if __name__ == "__main__":
    main()
