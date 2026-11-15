---
name: sandloom-ci-map
description: "Which CI jobs and commands a changed path runs in the Sandloom repository (ci, docs, admin, packages and architecture workflows, tools/ci/required_ci.py). Load before a push to pick the checks of the areas you touched, or before opening a workflow file."
allowed-tools: Bash(uv run --quiet python tools/agent_reference.py:*)
---

# CI by path

A changed path runs the jobs of its area; run the same commands locally before a
push. Rendered from the workflows of this checkout when the skill loads, in the
project's environment (reading the workflows needs PyYAML).

!`uv run --quiet python tools/agent_reference.py ci`

If you see the command above instead of its output, run it from the repository root.
