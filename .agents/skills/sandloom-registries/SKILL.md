---
name: sandloom-registries
description: "Entry format and the command that adds or checks an entry of a shared registry file of the Sandloom repository: guarantees.yml, kernel_edges.yml, design/kernel-boundary/edges.yml, error_codes.py, public-source.json, public_api_baseline.json, CHANGELOG.md (changelog.d/ fragments). Load before opening or editing one of them."
allowed-tools: Bash(python3 tools/agent_reference.py:*)
---

# Registries

Shared files are never opened whole and others' lines are never reordered: add
your own entry with the command below, then run the gate it names.

!`python3 tools/agent_reference.py registries`

If you see the command above instead of its output, run it from the repository root.
