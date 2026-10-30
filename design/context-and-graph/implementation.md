# Implementation and validation

Implemented 2026-10-30 on `codex/context-graph-design`; unreleased. This is the
implementation record for the [specification](spec.md), not a release announcement.
The current public package version remains 0.7.0a3. Public guides explicitly mark
these commands as additions after that version.

## Delivered

- `sandloom context`: compact Markdown by default, explicit JSON, scoped rules,
  beginner SDK and referenced advanced names/aliases with usage evidence,
  whole-file wire/DTO closure, event schemas/views, module declarations and
  optional owned source/tests under an exact UTF-8 byte budget.
- `sandloom graph`: bounded module discovery, typed JSON, witnessed module hops,
  relation-specific cycles, Mermaid and an offline HTML viewer with search,
  focus, filters, evidence, raw-edge/entity tables and JSON download.
- Trusted generation collects manifest/descriptors and planned wire bytes into
  committed `.sandloom/inspection.json`. The index is published atomically after
  wire outputs; incompatibility writes nothing. Partial generation retains the
  previous project index when unselected artifacts are stale.
- Local ignored receipts verify module scopes independently. Incomplete optional
  provider interfaces do not prevent another module from verifying. Ordinary
  body/comment edits do not create semantic generated-file failures; read-only
  checks leave receipts and generated files untouched.
- Cold exports use standard-library readers, never import the app/settings/SDK,
  parse optional YAML, contact backends, prompt or silently regenerate. Structured
  errors use typed kinds, exit codes and catalogue entry SANDLOOM-T015.
- SDK catalog is generated/checked through the existing reference pipeline and
  shipped as package data. The viewer is also a package resource; standalone
  Hatch-only sdist builds retain both.
- Framework skill mirrors, application template, `new` / `add skills`, agent maps,
  CLI help/reference, two public guides, navigation and llms index entries.

## Validation evidence

`uv run sandloom test --full --ci` completed successfully:

- 5,082 passed, 7 skipped, 1 expected failure in the full suite;
- 1,261 passed, 6 skipped in architecture checks;
- all generated, schema/deploy, Ruff, formatting, mypy, architecture/import,
  snapshot, migration-drift, public API and typing gates passed.

New acceptance tests cover context closure/isolation, inherited instructions,
JSON/Markdown protocols, budgets, aliases, ignore policy, stale/missing inputs,
unsafe paths and canonical output aliases, read-only checks, planned wire bytes,
partial generation, independent receipts, graph witnesses/cycles and hostile labels.
A subprocess success test blocks app/settings/SDK/YAML imports and network sockets.

All four distributions passed wheel/sdist builds, rebuilt-wheel comparison,
license metadata, import layering and clean bare/standard installation checks.
The installed cold-tool smoke exercises successful context, discovery and HTML
export with YAML/app imports and sockets blocked, outside the source checkout.

Five real applications created from the reviewed local wheel set passed:
no batteries; identity; tenancy; billing; all three. Each ran generation and its
read-only check, verified the owned notes context, discovered modules, exported
HTML and received matching skill mirrors. Notes JSON context was 24,785 bytes
in every variant: installed battery implementation source was excluded.

All five checked-in examples carry their generated inspection index; generation
and its read-only check passed for each. Linux fallback CI on the implementation
commit passed `backend-static` and `docs`. Unified website build passed. Browser checks confirmed search/focus and absence
of JavaScript errors in the standalone graph. A declared 20-module high-fan-out/
event-cycle fixture exported 80 nodes, 140 raw edges and 80 module relations:
67,058 JSON bytes / 75,744 standalone HTML bytes; in-process export took 16.66 ms.
This synthetic fixture tests export scale, not runtime traffic or universal readability.

## Renderer and limits

V1 uses package-owned inline SVG and DOM tables: no external library, CDN,
renderer licence or backend dependency. The overview shows module relations;
filters and the raw-edge/entity tables expose contract/channel details. Dense
maps should use focus and relation filters; advanced automatic layout is a later
viewer improvement, independent of the semantic reader/protocol.

Installed-only modules are not source-editing context targets. Unknown external
DTO libraries and external JSON Schema references fail with an explicit diagnostic;
V1 resolves local generated wire closure instead of executing imports or fetching
URLs. Dynamic/unresolved event-view bases fail context capture rather than
claiming complete fields. Hashes verify enumerated authoring inputs, not arbitrary
Python/environment-dependent behavior or live topology.

Warm-filesystem fresh-process medians from three local CPython 3.12/macOS runs
were approximately 264 ms for module discovery, 172 ms for focused graph JSON,
573 ms for billing JSON context and 648 ms for billing Markdown context. Other
gates were active; these are local observations, not performance guarantees.
No token-savings, AI correctness or cost improvement is claimed. No paid agent
DX experiment ran; ordinary deterministic protocol/app fixtures provide the
validation above.

Owner review/merge, package versioning/release and website deployment remain
separate actions. This work does not publish packages or the site.
