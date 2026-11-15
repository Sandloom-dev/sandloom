---
name: sandloom-plan
description: "Index of the headings of the active wave's plan or spec in the Sandloom repository: every design doc marked with an agent-reference index comment in its first lines. Load before opening a design plan or spec, to find the one section your task names and read only that."
allowed-tools: Bash(python3 tools/agent_reference.py:*)
---

# Plan index

One section per marked design doc: its path and its `## ` headings. A wave's
orchestrator adds the marker `<!-- agent-reference: index -->` to its plan or spec
when the wave starts and removes it when the wave ends.

!`python3 tools/agent_reference.py plan`

If you see the command above instead of its output, run it from the repository root.
