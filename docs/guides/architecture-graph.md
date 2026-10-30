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
search across entities, semantic layers, directed neighborhoods, zoom/pan/fit,
a persistent inspector and equivalent entity/relationship lists.
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
relation. `--direction incoming|outgoing|both` controls expansion (default: both). Shared capabilities or DTO
imports do not invent cross-module calls. Missing providers and optional contracts
remain explicit. Available authoring metadata is not evidence of deployed services.

Required contracts, optional contracts, ordering and event-impact cycles are
reported separately. An event feedback loop may be intentional; a declared cycle
is not proof of deadlock. The graph never claims an event causes an RPC merely
because both touch one module.

## Processes: sequence and state

```bash
uv run sandloom graph --view sequence
uv run sandloom graph --view sequence --process SAGA_OR_ROUTE_ID
uv run sandloom graph --view sequence --process SAGA_OR_ROUTE_ID --format mermaid
uv run sandloom graph --view state --process SAGA_ID --format html --output states.html
```

Select an actual stable ID from the process catalog. Saga IDs include their
registered version so parallel definitions remain distinct. A saga sequence
shows declared forward steps, the pivot and unit boundaries. Compensation is a
separate alternative: only completed compensatable steps are undone, in reverse
order, when the runtime enters compensation before the pivot. The state view
shows conditional framework transitions, including parking; it is not an attempt
history. Callbacks and their argument functions are never executed by inspection.

Route sequences use resolved generated route policy, not a guessed call graph.
They show session checks, idempotency, conditional unit/commit phases or the
operation-intent release rule. Handler internals stay opaque. A declaration does
not establish that an event was published, a request succeeded, a consumer ran,
or multiple module commits were atomic. Kernel routes without a module policy
snapshot are not added as invented business-process declarations.

The HTML **Processes** tab offers forward sequence, compensation alternative and
saga states, each with an equivalent step/transition list. The inspector explains
the selected step and source when known. A saved URL fragment restores filters,
representation and selection; the same local export must be available to its
recipient. The mobile details button leads to the same inspector.

## Potential impact and architecture changes

```bash
uv run sandloom graph --view impact --item contract:stats.quota.v1
cp .sandloom/inspection.json architecture-before.json
# Change declarations, then explicitly regenerate.
uv run sandloom generate
uv run sandloom graph --view diff --before architecture-before.json
uv run sandloom graph --view diff --before architecture-before.json --format html --output changes.html
```

Replace the item ID with one from your graph. Impact preserves the direct
interface/owner witnesses, then follows potential module reachability. Transitive
results can involve other interfaces: this is an overapproximation of declared
impact, not complete call-site or test coverage. Optional dependencies remain
optional. Suggested module test commands never replace normal quality gates.

Diff reads a project-relative historical index as JSON data. It does not check
out Git revisions, import old code or resolve old source paths. It lists additions,
removals and changes to declarations/interfaces; implementation bodies are not
compared. Contract compatibility uses the framework's rolling-upgrade rules;
event changes use its same-version schema rules. Missing interface evidence is
`unknown`, never automatically compatible. Other changes remain `not_assessed`.
The comparison basis and counts are included in JSON and the HTML Changes tab.

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
and renderer locally, with no CDN, telemetry or external fonts. Save SVG exports
the current overview; Save JSON retains the original export and its evidence. Its JSON download
retains the exported scope; viewer filtering does not pretend to change the
underlying export inventory.

Freshness, explicit generation, output safety and structured errors follow the
[module context guide](module-context.md). Cold reads need no app environment,
PostgreSQL, Redis or optional PyYAML. A missing inspection index requires explicit
`sandloom generate`; this may load the app and is not run automatically.
