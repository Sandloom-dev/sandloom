---
title: Explore the architecture graph
---

# Explore the architecture graph

Inspect what an authoring composition declares: business modules, contracts,
event channels, consumers, projections and infrastructure requirements.
This is a declared map, not production traffic or active RPC routing.

!!! note "Package availability"
    Added after 0.7.0a3. Use the current source checkout until the next PyPI release.

```bash
uv run sandloom graph --view modules
uv run sandloom graph --module notes --hops 1
uv run sandloom graph --format html --output architecture.html
```

Discovery lists stable module IDs without dumping relationships. The graph
returns compact JSON by default; the HTML file is an offline viewer with module
search, relation filters, focus/hops, evidence, keyboard controls and text tables.
Open it yourself; the command never opens a browser or starts an app server.

## What an arrow means

| Raw relationship | Direction |
| --- | --- |
| Provided / required contract | Provider / caller module → versioned contract |
| Produced event | Producer module → event channel |
| Consumed event | Event channel → consumer |
| Consumer ownership | Owning module → consumer |
| Projection input / ownership | Channel → projection; module → projection |
| Capability requirement | Module → capability/instance/version |
| Ordering | Dependent module → prerequisite module |

The overview derives caller → provider and producer → consumer-owner relations,
retaining the raw contract/channel witness edges. A hop crosses one such module
relation, including incoming and outgoing neighbors. Shared capabilities or DTO
imports do not invent cross-module calls. Missing providers and optional contracts
remain explicit. Available authoring metadata is not evidence of deployed services.

Required contracts, optional contracts, ordering and event-impact cycles are
reported separately. An event feedback loop may be intentional; a declared cycle
is not proof of deadlock. The graph never claims an event causes an RPC merely
because both touch one module.

## Exports and limits

```bash
uv run sandloom graph --module module:notes --hops 0
uv run sandloom graph --module notes --format json --detail full
uv run sandloom graph --module notes --format mermaid
uv run sandloom graph --require-verified
```

Compact JSON preserves relation types, endpoints, requiredness and witness IDs.
Full detail adds provenance. `--max-bytes` limits complete JSON output (default
131072 UTF-8 bytes); overflow fails with a remedy to focus the graph or raise the
budget. Nodes/edges are never silently truncated. Scoped exports report hidden
counts. `--hops` requires a module and accepts non-negative integers.

`--detail` and `--max-bytes` are JSON options; presentation formats reject them.
Mermaid is interchange output; third-party layout can vary. HTML embeds its data
and renderer locally, with no CDN, telemetry or external fonts. Its JSON download
retains the exported scope; viewer filtering does not pretend to change the
underlying export inventory.

Freshness, explicit generation, output safety and structured errors follow the
[module context guide](module-context.md). Cold reads need no app environment,
PostgreSQL, Redis or optional PyYAML. A missing inspection index requires explicit
`sandloom generate`; this may load the app and is not run automatically.
