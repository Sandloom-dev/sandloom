---
name: sandloom-kernel-map
description: "File and symbol of the Sandloom kernel entry points: create_app, the plugin loader, define_module, the contract and snapshot generators, the error catalogue. Load before opening src/sandloom/core/app_factory, core/loader, core/cli/generated.py or core/error_codes.py."
allowed-tools: Bash(python3 tools/agent_reference.py:*)
---

# Kernel map

Entry points as file and symbol, with no line numbers: find the line with
`grep -nE '^(async def |def |class )?<symbol>\b' <file>` (a function, class or constant). Read from the AST of this checkout when the skill loads.

!`python3 tools/agent_reference.py kernel`

If you see the command above instead of its output, run it from the repository root.
