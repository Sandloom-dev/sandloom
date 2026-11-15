#!/usr/bin/env python3
"""PreToolUse guard on Agent: a sandloom-implementer runs on the model its agent file sets.

On 2026-11-14 Opus implementers took 202M of 347M tokens: 6 of 11 launches passed
`model: opus` over the agent file's `model: sonnet`. This refuses that override.
"""

import json
import sys

GUARDED = {"sandloom-implementer"}


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    if event.get("tool_name") not in ("Agent", "Task"):
        return
    tool_input = event.get("tool_input") or {}
    agent = tool_input.get("subagent_type")
    model = tool_input.get("model")
    if agent not in GUARDED or not model:
        return
    reason = (
        f"{agent} runs on the model its agent file sets (.claude/agents/{agent}.md); "
        f"launch it again without `model` (got {model!r}). Opus is for the security "
        "review only, through sandloom-reviewer."
    )
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
