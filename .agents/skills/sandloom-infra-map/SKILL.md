---
name: sandloom-infra-map
description: "Where a capability's port, drivers (tier, package), conformance kit and tests live in the Sandloom repository. Load before opening core/infra, core/drivers, driverkit or packages/sandloom-*, or before grepping for a provider, driver or kit class."
allowed-tools: Bash(python3 tools/agent_reference.py:*)
---

# Infrastructure map

One line per capability, read from the code of this checkout when the skill loads
(ports, kits, built-in driver rows and tiers, driver packages' entry points, test
files). Open the named file, not the directory.

!`python3 tools/agent_reference.py infra`

If you see the command above instead of its output, run it from the repository root.
