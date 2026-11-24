---
title: Explore the architecture graph
type: how-to
---

# Explore the architecture graph

Inspect what an authoring composition declares: business modules, contracts,
event channels, consumers, projections and infrastructure requirements.
This is a declared map, not production traffic or active RPC routing.
The browser viewer, `sandloom explore`, is experimental: its view and
options may change between releases; `graph` and `context` are stable.

!!! note "Package availability"
    Available in `sandloom>=0.7.0a17,<0.8`. <!-- sandloom-release -->

```bash
uv run sandloom explore                  # generate, save HTML, open the browser
uv run sandloom graph                    # compact text project map
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
uv run sandloom graph --format json      # structured fields and stable IDs
uv run sandloom graph --format text      # readable output even through a pipe
uv run sandloom graph --format html > architecture.html
```

`graph` reads the saved model and never opens a browser or regenerates files.
It defaults to text in terminals, pipes and redirects; request JSON explicitly
with `--format json`. Without a module selector, each module gets its recorded
package purpose and a compact facts line: routes, tables, provided contracts,
published event count, called modules and listened-to producer modules. Infrastructure
requirements remain in detailed module views. `generate` records the first line of
each module package's docstring, including installed batteries without owned source.
The cold reader never imports those packages.

Every action uses one positional grammar: `graph [MODULE]`, `graph impact ITEM`,
`graph diff REV_OR_FILE`, `graph sequence [ID]` and `graph state ID`.
Use `module:NAME` for a module named `impact`, `diff`, `sequence` or `state`;
`modules` is an ordinary module name. Import-path module selectors also work.
JSON includes the module list with stable IDs and recorded summaries.

HTML provides
search results across all layers (opening an entity also selects its layer), semantic layers, directed neighborhoods, zoom/pan/fit, a persistent
inspector and equivalent entity/relationship lists. Disconnected modules are placed
separately; arrows route around unrelated blocks. Large graphs open at readable
scale inside a scrollable canvas. Fit-all is explicit and can require zooming
back in to read labels. Module purposes appear on blocks and in the inspector;
route counts open recorded routes and their framework lifecycles.

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
imports do not invent cross-module calls. The HTML module layer draws one labelled
line per direction of a module pair; the list below it keeps every declaration.
In the HTML interface layer, a module filter shows that module's contracts and events
with the modules on their far side, with consumers and projections collapsed into
their owner. In the infrastructure layer it shows only that owner's declarations;
direction and distance controls apply to the module-relationship layer. Missing providers and optional contracts
remain explicit. Available authoring metadata is not evidence of deployed services.

Required contracts, optional contracts, ordering and event-impact cycles are
reported separately. An event feedback loop may be intentional; a declared cycle
is not proof of deadlock. The graph never claims an event causes an RPC merely
because both touch one module.

## Synchronous coupling

A contract call is synchronous: the caller waits for the provider. A provider
that is slow or down hurts every caller. The graph counts these dependencies
from the declared contract requires, both required and optional. The count is an
upper bound. A declared require may never be called on a given path, and the
graph measures no calls.

```bash
uv run sandloom graph --runtime-risks          # table after the module map
uv run sandloom graph notes --runtime-risks    # the module and its neighbors
uv run sandloom explore                        # the Coupling tab
uv run sandloom graph --format json            # data.sync_coupling
```

The flag is text only. JSON always carries `data.sync_coupling` and the HTML
viewer always has the **Coupling** tab. So `--runtime-risks` with `--format json`
or `html`, or with `impact`, `diff`, `sequence` or `state`, is a usage error
(exit 2). The table has one row per module in scope:

```text
Module    Synchronous dependencies                Depth  Fan-in
tickets   billing (optional), tenancy (optional)  2      0
billing   tenancy (optional)                      1      1
tenancy   -                                       0      2
identity  -                                       0      0
Longest chain (depth 2): tickets → billing → tenancy
Most depended on: tenancy (fan-in 2), billing (fan-in 1)
```

- **Synchronous dependencies** are the modules this one requires a contract
  from. `(optional)` marks a dependency the caller can lose and still run.
- **Depth** is the longest chain of contract hops this module can start: a
  request entering at `tickets` may wait on `billing`, which waits on `tenancy`.
  Each hop adds latency and one more provider whose outage reaches the caller.
- **Fan-in** is how many modules require this one. A high fan-in marks a module
  whose outage reaches many callers.
- **cycle** in the depth column: the module sits in a contract cycle, so it has
  no finite depth. `2 + cycle` means the longest chain is two hops and at least
  one branch reaches a contract cycle (the hop into the cycle is counted). Rows
  are ordered cycles first, then by depth, then by fan-in. A cycle is declared,
  not proof of deadlock, but a call that returns to its caller is the first thing
  to remove.
- With a focus (`graph MODULE --runtime-risks`), depth and fan-in are computed
  over the selected neighbourhood only, not the whole application.

What to change, in this order:

1. **A cycle.** Break it where one side only needs to be told something
   happened: replace that require with an [event](../modules/between/events.md)
   the other side consumes.
2. **A deep chain** (depth 3 or more, or a required hop at the end of a long
   one). Ask what the first module needs from the last. If it only reads data
   to answer a request, keep a [projection](../modules/between/projections.md)
   of it in the caller. The provider's events feed it, and the contract call
   goes. If the call starts work whose result the caller does not wait for,
   publish an event instead.
3. **A high fan-in** on a module in the middle of chains. Keep its contracts
   small and its optional callers optional, and look at its callers for reads a
   projection can serve. A leaf with a high fan-in, such as an identity or
   tenancy module, is a normal shape: do not rewrite it for the count alone.

An event or a projection trades the immediate answer for eventual consistency.
Replace a call only where the caller can live with data that is briefly behind.
A multi-step operation that must be undone as a whole is a
[saga](../modules/between/sagas.md), not an event chain.

The Coupling tab lists the same rows. Selecting a module name opens its
inspector, which shows that module's depth, fan-in and longest chain.

## Processes: sequence and state

```bash
uv run sandloom graph sequence
uv run sandloom graph sequence SAGA_OR_ROUTE_ID
uv run sandloom graph sequence SAGA_OR_ROUTE_ID --format mermaid
uv run sandloom graph state SAGA_ID --format html > states.html
```

Text catalogs show process names, owning modules and saga versions. Select a
unique name, quoting names containing spaces, for example
`uv run sandloom graph sequence "POST /api/v1/enrollments"`. For ambiguous names,
use `graph sequence --format json` to obtain an exact stable ID. Saga IDs include their
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
uv run sandloom graph diff architecture-before.json
uv run sandloom graph diff architecture-before.json --format html > changes.html
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
Git is required only for revision baselines. `graph diff old.json` reads a
project-relative saved index without Git. Existing files take precedence over
revision names; `.json` names and explicit `./` paths select files even when
missing. Both forms treat the baseline as JSON data. It lists additions,
removals and changes to declarations/interfaces; implementation bodies are not
compared. Contract compatibility uses the framework's rolling-upgrade rules;
event changes use its same-version schema rules. Missing interface evidence is
`unknown`, never automatically compatible. Other changes remain `not_assessed`.
The comparison basis and counts are included in JSON and the HTML Changes tab.

## Exports and limits

```bash
uv run sandloom graph module:notes --hops 0
uv run sandloom graph notes --format json --detail full
uv run sandloom graph notes --format mermaid
uv run sandloom graph --require-verified
```

Compact JSON preserves relation types, endpoints, requiredness and witness IDs.
Full detail adds provenance, module declarations and process policy; compact
topology includes process counts and defers their bodies to the sequence query.
`--max-bytes` limits complete JSON output (default
131072 UTF-8 bytes); overflow fails with a remedy to focus the graph or raise the
budget. Nodes/edges are never silently truncated. Scoped exports report hidden
counts. `--hops` requires a module selector and accepts non-negative integers.

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
