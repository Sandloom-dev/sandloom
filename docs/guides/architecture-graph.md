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
uv run sandloom explore                  # generate, save HTML, open the browser
uv run sandloom graph                    # terminal tree; JSON in a pipe
uv run sandloom graph notes              # module and immediate neighbors
uv run sandloom graph impact stats.quota.v1
uv run sandloom graph diff main          # compare the committed model on main
```

`explore` explicitly runs `sandloom generate`: it loads the application in its
own process and writes managed artifacts after compatibility checks. It then
saves `.sandloom/cache/explore.html` and opens its local file URL. The HTML embeds
its renderer and data; no web server or external service is needed. Generation
failure stops the command before opening a stale page. The file URL is always
printed; a headless environment can use `--no-open`.

```bash
uv run sandloom explore notes --hops 0
uv run sandloom explore --no-generate    # inspect the saved model, without app loading
uv run sandloom explore --no-open        # generate and save only
uv run sandloom graph --format json      # stable choice for scripts/agents, including a TTY
uv run sandloom graph --format text      # readable output even through a pipe
uv run sandloom graph --view modules     # discover stable module IDs
uv run sandloom graph --format html --output architecture.html
```

`graph` reads the saved model and never opens a browser or regenerates files.
Its default depends only on stdout: a TTY gets a plain tree, while a pipe,
redirect or `--output` gets compact JSON. `--format` overrides that choice.
There is no agent identity detection. The existing `--module`, `--view`, `--item`
and `--before` forms remain supported. Modules named `impact` or `diff` can be
selected explicitly with `--module`.

Discovery lists stable module IDs without dumping relationships. HTML provides
module search across entities (with hints for matches hidden by another layer
or filters), semantic layers, directed neighborhoods, zoom/pan/fit, a persistent
inspector and equivalent entity/relationship lists.

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

Scoped HTML contains only the selected modules and their process declarations;
a specific process query includes only that process. Public interface references
can still identify related providers. Freshness covers the exported module scope.

The HTML **Processes** tab offers forward sequence, compensation alternative and
saga states, each with an equivalent step/transition list. The inspector explains
the selected step and source when known. A saved URL fragment restores filters,
representation and typed selection (including Changes/Impact); the same local
export must be available to its recipient. The mobile details button leads to the same inspector.

## Potential impact and architecture changes

```bash
uv run sandloom graph impact stats.quota.v1
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

`graph diff REV` reads this project's `.sandloom/inspection.json` from a local
Git revision, including the project prefix in a monorepo. The response records
the resolved commit ID. Commit the generated index on the baseline branch first;
a revision without it produces an explicit error, with no inferred replacement.
No fetch, checkout, old-code execution or historical source resolution occurs.
Git is required only for this explicit revision form. `--view diff --before PATH`
reads a project-relative saved index without Git. Both treat the baseline as JSON data. It lists additions,
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
Full detail adds provenance, module declarations and process policy; compact
topology includes process counts and defers their bodies to the sequence query.
`--max-bytes` limits complete JSON output (default
131072 UTF-8 bytes); overflow fails with a remedy to focus the graph or raise the
budget. Nodes/edges are never silently truncated. Scoped exports report hidden
counts. `--hops` requires a positional module or `--module` and accepts non-negative integers.

`--detail` and `--max-bytes` are JSON options; presentation formats reject them.
Mermaid is interchange output; third-party layout can vary. HTML embeds its data
and renderer locally, with no CDN, telemetry or external fonts. Save SVG exports
the current overview; Save JSON retains the original export and its evidence. Its JSON download
retains the exported scope; viewer filtering does not pretend to change the
underlying export inventory.

Freshness, explicit generation, output safety and structured errors follow the
[module context guide](module-context.md). Cold reads need no app environment,
PostgreSQL, Redis or optional PyYAML. For cold `graph` reads, a missing inspection index requires explicit
`sandloom generate`; it is never run automatically. `explore` is the separate
authoring command that generates by default.
