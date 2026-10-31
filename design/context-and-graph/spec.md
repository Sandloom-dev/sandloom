# Module maps and declared architecture inspection

**Status: implemented in 0.7.0a4.**
Updated 2026-10-30 following the compact-map review. This specification replaces
the initial source-bundle proposal: context is a navigation map, and the agent
reads implementation files itself. Historical research/acceptance records describe
the earlier prototype; this file and the public guides describe current behavior.

[Module map guide](../../docs/guides/module-context.md) ·
[Graph guide](../../docs/guides/architecture-graph.md) ·
[CLI reference](../../docs/reference/cli.md) ·
[Research](research.md) · [Acceptance records](evolution.md).

## Outcome

A module author or agent can discover an isolated module, understand its public
interfaces and composition-local connections, and locate the files needed for a
task. The tool supplies structural evidence and navigation rather than selecting
and copying implementation code into the caller's context. A human can generate
and open the same declared architecture as an offline Explorer in one command.
No extra application declarations, hosted service or LLM calls are required.

This is an authoring model. It does not observe traffic, infer handler control
flow, prove distributed transactions or provide production snapshots/replay.
Normal architecture, compatibility and quality gates remain authoritative.

## CLI

```bash
sandloom context MODULE
sandloom context MODULE --format json
sandloom context MODULE --require-verified
sandloom graph
sandloom graph MODULE --hops 1
sandloom graph impact INTERFACE_ID_OR_NAME
sandloom graph diff LOCAL_REVISION
sandloom graph diff saved-index.json
sandloom explore
```

Context defaults to Markdown in terminals and pipes. Explicit JSON exposes the
same map through a versioned envelope. It has no source export, byte-budget or
full-evidence mode. Graph JSON retains full
provenance and a configurable byte budget.
Graph defaults to text in terminals and pipes; JSON requires `--format json`.
Modes are positional: `impact ITEM`, `diff REV_OR_FILE`, `sequence [ID]`,
`state ID` or a module selector. `modules` is an ordinary module name.
Reserved module names use `module:NAME`. Shell redirection saves stdout;
Explorer alone writes an internal, fixed cache artifact atomically.

Both commands accept a project root and verification requirement. Graph JSON
has a default 131072-byte budget; overflow fails without a partial success.
Context emits its complete map without size accounting. Whole signatures and route methods remain intact.

`explore` explicitly invokes normal authoring generation in a child process,
saves `.sandloom/cache/explore.html`, prints its local URL and opens the browser.
`--no-generate` reads the saved model; `--no-open` only saves. Generation failure
stops before opening stale HTML. An unavailable desktop browser leaves a usable
file and a clear explanation. The cold inspection commands never do these steps.

## Module map

JSON uses `data.representation = module_map`. Markdown is a readable projection
of the same facts. The map includes:

- Module identity and owned root; paths to applicable agent/rule documents.
- Provided/used contracts: available declared method signatures, read/command
  kind, optionality and callers/providers within this composition. Missing method
  descriptions are explicitly unavailable; provider bodies are never inspected.
- Produced events and declared listeners, incoming events and handlers.
- Route methods/paths, tables, versioned sagas and infrastructure
  requirements. Available authoring metadata is not deployed configuration.
- Owned file paths (sizes in JSON), first-line purpose from module docstrings and
  a few top-level public symbols with line numbers. Additional symbols are counted.
  Schemas/models have directory inventories; tests and migrations have summaries.
  Migration heads come from literal revision/down-revision links.
- Generated public interface paths, freshness and scope-specific unresolved facts.
  Implementation bodies, full files, SDK reference entries and instruction bodies
  are not embedded. Descriptions are source metadata, not inferred behavior.

File metadata is read through contained stable readers and parsed without imports
or execution. Syntax errors, oversized files and unsupported text encoding retain
a navigable path and an explicit per-file metadata warning. Test bodies need no
decoding or AST parsing; raw hashes still track source changes. Source changes
invalidate the freshness receipt. Escaping ownership or traversing symlinks fails
closed. Actual exported metadata remains secret-checked. `.sandloomignore`
excludes owned file metadata, while rule/public-interface references remain available.

Exact DTO fields, defaults, validation constraints and aliases remain in the
linked generated interface files. Agents open those and implementation helpers/tests
using their ordinary repository tools. Context returns the complete module map;
it has no interface-slicing flag. Contract commands expose declared risk as
`[money]`; idempotency key parameters appear in signatures without duplicate metadata.

A context target requires checkout-owned source. Installed-only dependencies
supply public interfaces and logical identities rather than editable inventories.
Cross-module imports/edit authority are not expanded by inspecting a relation.

The map contains facts about its selected scope. Follow-up commands and general
limits belong in the module-context skill. Markdown suppresses default auth tags,
redundant route groups, small-file sizes and inert package initializers, groups
tables by tenant scope, and shows readable process labels. Structured output
retains stable IDs. Markdown explains freshness in words, omits exclusion accounting
and uses single blank lines between sections. JSON retains status enums and
`context_ignore`/`package_scaffolding` counters for automation.

The default graph text answers a project-level question: one purpose line and one
facts line per module. Counts include routes, tables and produced events; provided
contracts retain names, while calls and listeners name their peer modules.
Infrastructure stays in detailed module views. Average full stdout size, including
the header, must not exceed 300 UTF-8 bytes per module in each maintained example
and the root composition. JSON keeps stable IDs and `summary` for each module.

## Shared model and freshness

Explicit `sandloom generate` builds `.sandloom/inspection.json` from the existing
module manifest, descriptors, resolved route policy, contract descriptions, event
schemas and registered saga definitions. It records normalized project-relative
layout and source locators. Each module card includes `summary`, the first line
of its package docstring, collected from the loaded package even when it is an
installed battery without checkout-owned source. Cold readers use this recorded
text without imports. It writes the index after compatibility checks and
compares a local verification basis before publishing a receipt. Inspection never
invokes callbacks, argument functions or application imports.

The semantic index belongs in Git. The receipt under `.sandloom/cache/` is local
and per module. Basis inputs include owned source membership/content, scoped rule
files, generated interface closure, declarations and distributions.
Optional inspection failures withhold new verification, while a detectable source
race aborts before publishing generated output. Existing schema-1 indexes without
new optional metadata remain readable with explicit unavailability.

Reader validates schema variants, unique identities, endpoints, package/source
ownership and raw layout-file identity. Cold reads do not parse YAML, load `.env`,
construct settings/SDK or contact backends. Freshness is evidence of these inputs,
not runtime health. Missing receipts label uncertainty; malformed/future receipts
produce typed errors. `--require-verified` exits 3 when selected verification is
unavailable. Missing mandatory interfaces and invalid ownership fail independently.

## Graph, processes, changes and impact

Raw edges retain contract/event/capability evidence. Caller-to-provider and
producer-to-consumer module relations retain raw witness IDs. Required/optional
contracts, ordering and event cycles are separate families. Iterative SCC
traversal avoids repeated reachability scans and includes self-loops; a cycle
is not proof of a deadlock. Directed hops cannot cross invented DTO/shared-infra
relations. Filtering reports hidden counts without changing the underlying model.

Sequence/state views use versioned stable process IDs. Saga forward paths mark
pivot and local/provider unit boundaries; compensation is a separate conditional
reverse path before pivot. State transitions own both full and short labels.
Route sequences describe conditional framework phases from resolved policy and
keep handler internals opaque. Mermaid preserves these meanings with its native
escaping rather than HTML entities. These views are declarations, never traces.

Impact starts with direct owners/interface users and retains their witnesses,
then labels transitive module reachability as an overapproximation that may pass
through other interfaces. It is not call-site or test coverage and cannot justify
skipping quality gates. Diff compares saved semantic declarations and reuses the
runtime's contract/event compatibility rules. Unknown evidence stays unknown.

Explicit revision diff resolves a local revision to an immutable commit and reads
this project's bounded committed index, including a monorepo prefix. It never
fetches, checks out code or executes historical Python. Git lazy fetching and
terminal authentication prompts are disabled. A missing committed index is an
error; file-based comparison works without Git. The response records its basis.

## Artifact and browser safety

Inspection writes only stdout. Shell redirection saves external artifacts.
Explorer and generation atomically replace only their framework-owned cache/index
paths, retaining containment and symlink checks. There is no public destination,
force flag, artifact acknowledgement or export-recognition layer.

HTML embeds renderer/data with safe JSON encoding and no external asset service,
telemetry or font request. It contains only the selected module/process inventory
and corresponding freshness. Typed URL state preserves entities, process steps,
Changes/Impact rows and interactive impact previews across reload/tab changes.
Search identifies matches hidden by another layer/filter. Lists, keyboard actions
and the mobile details control provide alternatives to two-dimensional dragging.
Runtime accessibility and participant comprehension claims require separate study.

## Response and verification contract

JSON emits one `sandloom.tool_response/1` success/error envelope, with command,
kind, selection, freshness, data and typed diagnostics. Text errors leave stdout
empty and explain on stderr. Exit codes: 0 usable,
1 invalid/missing/unsafe input or budget/I/O failure, 2 usage, 3 unavailable strict
verification. SANDLOOM-T015 routes to the existing explanation machinery.

Tests cross the CLI/model interface: compact map completeness, absence of bodies
in Markdown and JSON, graph byte limits, interface/type
and peer evidence, file navigation without execution, ignore counts,
route preservation, stale/cold reads, source races, managed atomic writes and
HTML scope/selection. Packaging checks exercise lean installed cold tools;
application smoke covers all-battery generation and Explorer. Normal full quality
gates, Linux CI and source-export checks apply. Paid AI-agent DX remains deferred.
