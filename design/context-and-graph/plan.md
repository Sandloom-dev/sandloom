# Implementation plan: module context and architecture graph

**Status: Proposed — implementation not started.** Date: 2026-10-30.
[Specification](spec.md) · [Research and source evidence](research.md).

The phases below are reviewable PRs with acceptance evidence, not a promise of
calendar duration. No paid agent benchmark or feature implementation was run
for this design. Keep website publishing separate from these tooling changes.

## Phase 0 — lock examples, schema and defaults

**Own:** design fixtures/protocol schemas and the collector contract.

- Turn the shared data model, agent response/error envelope, discovery catalog
  and command payloads into version-1 JSON schemas and complete golden examples.
  JSON is the default for direct agent calls; human formats stay explicit.
- Select a small fixture set from existing app/framework layouts: checkout-owned
  module; installed optional batteries; non-default shared package; DTO aliases,
  recursive `$defs` and copied DTO origins; event views; absent optional provider;
  a legitimate event feedback cycle; partially composed module set.
- Include hostile/path fixtures: duplicate logical names, symlink to a sibling,
  tracked secret file, changed instruction file, new/deleted source, inaccessible
  installed provenance, source changing during capture, oversized mandatory DTO.
- Decide and record the viewer renderer after an isolated footprint/layout/
  accessibility spike on 20-module/high-fan-out/cyclic examples. No backend
  dependency or CDN requirement. No claim of universal readability from one demo.
- Confirm the two-level freshness policy: default labelled interactive snapshots,
  optional `--require-verified`, unchanged read-only gates and no body-edit
  regeneration tax. Record source ownership/instruction mapping for framework
  modules and scaffolded applications, including non-ancestor role contracts.

**Done when:** schemas/examples, defaults, renderer decision and diagnostic
semantics are reviewable; every graph edge has actual declaration evidence.
Do not add a generic Python evaluator or whole-repository index to resolve gaps.

## Phase 1 — shared semantic collector and authoring integration

**Own:** `src/sandloom/core/cli/inspection/{model,collect,provenance}.py`,
`core/cli/generated.py`, its existing generation tests, scaffold ignore/template
integration, and the structured SDK catalog producer under `tools/docs/` (shipped resource:
`src/sandloom/core/cli/inspection/sdk-catalog.json`).
Names describe intended responsibilities, not existing modules.

- Collect from existing AppManifest, loaded ModuleDescriptors, contract describe
  data and generated stub/DTO/schema files. Reuse current module-name resolution,
  DTO origins and static event-view extraction. Keep loader/runtime behavior intact.
- Retain capabilities/features, optionality/reasons, ordering and source evidence
  without expanding frozen AppManifest solely for the tools.
- Use current planned wire bytes rather than old on-disk stubs/DTOs/schemas.
  Test a missing artifact and a compatible type change becoming coherent in one
  generation invocation and then passing immediate `--check`.
- Add the semantic inspection document to full generation and semantic stale
  checks. Preserve the existing compatibility refusal and no-write checks.
- Implement atomic publication of the index after referenced wire artifacts.
  Define partial-generation behavior exactly as in the specification; never
  overwrite the project inventory with a single-module inventory.
- Freeze input membership/content before application composition and verify again
  after planning. A concurrent source edit must withhold the new index/receipt,
  not associate new file hashes with old imported objects.
- Write a separate local ignored verification receipt after successful explicit
  generation. Capture membership+content, policy/layout and distribution/wire
  identities. Use per-module/closure semantic digests and a separate project digest;
  unrelated declaration regeneration must not invalidate focused verification.
  Do not put full implementation fingerprints into semantic output. Ignore only
  `.sandloom/cache/`; keep `.sandloom/inspection.json` eligible for version control.
- Generate/commit/check a public SDK signature/doc/tier catalog during authoring;
  ship its JSON resource without adding SDK imports or runtime execution to Hatch.

**Reuse tests:** `tests/sdk/test_sdk_generated.py`,
`tests/sdk/test_sdk_contract_dtos.py`, `core/tests/test_app_manifest.py`,
`tests/architecture/test_kernel_import_contracts.py`.

**Done when:** no-incompatibility-write behavior, partial generation, deterministic
semantic output, crash/interruption consistency and implementation-only edits
all pass. Existing generated files are unchanged unless declaration data changes.
SDK baseline changes are detected by catalog validation.

## Phase 2 — cold reader and policy/closure engine

**Own:** `core/cli/inspection/{read,files,closure,diagnostics}.py` and focused
fixtures/tests. The runtime collection dependency cannot leak into these imports.

- Read normalized layout from semantic JSON and verify the raw layout-file hash.
  Do not call optional-PyYAML `load_rules`; a layout mismatch fails before resolving
  source ownership. Test a real success path with `yaml` absent.
- Read the semantic document without loading application code,
  settings, dotted package parents or plugin entry points.
- Resolve owned source roots and allowed public artifacts; classify installed
  providers separately. Enforce hard exclusions before/after canonical resolution.
- Parse wire stubs/DTO imports/annotations and JSON Schema references as data.
  Resolve closure with recursion protection; never evaluate annotations/defaults.
- Validate identities/schema versions and calculate selected-scope receipt status.
  Resolve source evidence from current symbols when possible, flagging uncertainty.
- Implement deterministic diagnostics, byte accounting inputs and safe destination
  checks. No model calls, embeddings, network index or automatic secret values.

**Reuse tests:** static CLI subprocess patterns in
`core/tests/test_static_cli_no_env.py`; installed module metadata fixtures;
existing DTO-origin and generated schema fixtures.

**Done when:** import sentinels, empty environment and socket/subprocess traps
prove cold behavior on real success/error paths. Closure, stale/unknown states,
file addition/deletion, owner isolation and escaping fixtures pass. Source-only
changes do not silently mutate generated artifacts or claim provider compatibility.

## Phase 3 — context CLI and readable/structured exports

**Own:** `core/cli/context.py`, `core/cli/inspection/context.py`, static dispatch
in `src/sandloom/__init__.py`, context templates and command tests.

- Implement the proposed `context` command over Phase 2.
- Include applicable scoped/role rules, beginner SDK surface and referenced
  advanced names, complete required wire definitions, selected source and tests.
- Reserve mandatory material, then select optional whole sections by stable
  priority. Enforce the complete UTF-8 byte budget and omission ledger.
- Implement noninteractive JSON-first invocation, structured failure responses
  and output-file acknowledgements; test a subprocess consuming stdout as JSON
  without parsing human messages or assuming a TTY.
- Emit Markdown and JSON from the same selected content, with collision-safe
  fences, hashes, freshness, inclusion reasons and verification instructions.
- Handle missing cache/unsupported installed-only focus with concrete remedies.

**Done when:** a real `sandloom new` app and framework business module both
produce useful bounded context. Nested DTOs and rules are never silently dropped;
no sibling implementation/denied file appears. Repeated exports are identical.
Validate actual commands from a bare wheel, not only parser `--help`.

## Phase 4 — graph export and offline viewer

**Own:** `core/cli/graph.py`, graph projection/formatters and package-owned viewer
assets. No business module or admin-console code is required for a first version.

- Export typed nodes/edges, evidence, scope and unresolved facts as canonical JSON.
- Add bounded `graph --view modules` discovery and JSON size-limit diagnostics so
  an agent can select a module without reading the entire graph or using errors
  for discovery. Label a catalog as not requesting relationships.
- Add Mermaid interchange output with escaped labels/stable generated identifiers.
- Build the selected offline HTML renderer, module focus/hop/relation filters,
  incoming/outgoing inspection, explicit hidden counts and evidence selection.
- Keep detail expansion and the text table synchronized. Show authoring/snapshot
  freshness prominently; do not use a green “live” badge for cached metadata.
- Provide keyboard navigation/focus and a mobile table fallback. Escape embedded
  JSON/HTML/source labels and prohibit executable URL schemes.
- Report SCCs by relation type; avoid blanket event-cycle errors. Reference
  existing compatibility diagnostics instead of reimplementing evolution checks.

**Done when:** a small ordinary subprocess/JSON client can perform the complete
discover → context → focused graph → typed-error/recovery flow without human
assistance or model calls. Every fixture edge maps to real evidence, legitimate event cycles
remain inspectable, optional/missing providers are understandable, filters export
truthful scope and hostile labels cannot execute code.
Test reciprocal contract dependencies and one-hop focus preserving the provider
and intermediate contract; capability-sharing must not create module edges. Visual acceptance covers
20-module/high-fan-out graphs, both themes, keyboard and narrow screens.

## Phase 5 — package, documentation, gates and release readiness

**Own:** CLI/docs references, package smokes, source allowlist and reviewed workflow
fingerprints. No new optional backend or permanent service is introduced.

- Add every source/catalog/viewer/test asset to the reviewed source allowlist.
- Extend `tools/packaging/check_package_distributions.py` with actual artifact
  exports from clean bare/standard installations and sdist-rebuilt wheels.
  Prove standalone Hatch-only source builds still work and no source checkout
  or repository-only docs are required at runtime. Test the catalog resource from
  an sdist-rebuilt bare wheel with both optional YAML and repo tools/docs absent.
- Document cold/snapshot semantics, freshness modes, context exclusions, byte
  budgets, partial generation and the difference from live tracing/formal proof.
- Add error catalogue entries and verify their exact website anchors.
- Run required static, architecture, import, generated-artifact, typing and package
  gates. Run the framework's normal full validation for the generator/CLI change;
  retain all existing crash/transport/integration coverage.
- Trigger review of the documented DX evaluation policy for generator/API changes.
  Paid agent runs remain a separately authorized experiment; do not imply that
  offline fixtures establish actual token/cost/correctness improvements.

**Done when:** installed package exports and required CI pass; no runtime/SDK/
module isolation regression; user documentation matches the shipped CLI. Publish
only after owner review/merge and the normal release approval process.

## Measurements and decision gates

Use reproducible fixtures and report warm/cold conditions, source size, fixture
module/edge counts, output bytes, runtime, peak memory, full/omitted sections and
unresolved facts. Set performance ceilings after Phase-0 baselines; invented
universal millisecond/token targets are not acceptance evidence.

For later authorized human/agent comparisons, hold task, code baseline, framework
version and checks constant. Measure irrelevant/sibling file reads, missing
interface lookups, import/contract violations, task correctness and time/cost.
A smaller bundle alone is not success. No new claims are made until measured.

## Risks and stop conditions

- **Semantic drift:** if a collector would duplicate loader/generator rules,
  deepen the shared authoring collection seam instead of implementing a second rule set.
- **Freshness friction:** any implementation-only edit that causes an unrelated
  generated-file CI failure blocks rollout; repair the semantic/receipt separation.
- **Incomplete extraction:** unresolved dynamic metadata remains visible; do not
  widen source scope, execute an app or fabricate an edge to hide it.
- **Packaging:** missing catalog/assets or a Hatch-only sdist failure blocks release.
- **Graph scale:** if the viewer needs unreviewed remote code or cannot expose
  equivalent text/evidence, keep JSON/Mermaid available and repair the renderer
  before claiming the viewer acceptance complete.
- **Context safety:** denied-content output or cross-module implementation leakage
  blocks release, including through symlink/alias/Markdown boundary fixtures.

Implementation starts only after this proposed specification is reviewed. Design
artifacts do not add working commands or alter current generation/test behavior.
