# Context bundles and architecture graphs — research

Status: **design research; no implementation**. Reviewed 2026-10-30.
Runtime baseline: `2fa0bc19c06b6c541664b80e781117f1b8d08591` (0.7.0a3).
Website-only merge `1ed3562a35d9e0894ee293b5c69ce37bc106d70b` does not alter these runtime findings.

Read [the proposed specification](spec.md) and [implementation plan](plan.md).
The primary-source survey and repository inspection below are independent inputs.
Recommendations in them are not assertions that Sandloom already provides these features.

## Decisions from the evidence

1. Treat direct agent CLI invocation as a primary workflow: JSON-first, noninteractive, versioned data/errors, bounded discovery and typed recovery, with explicit human renderings.
2. Use existing declaration/generation semantics, not a second hand-authored dependency map or a speculative full Python static interpreter.
3. Add a generated semantic inspection document because format-1 module snapshots omit capabilities, source evidence, event views and freshness provenance.
4. Keep exported context/graph readers cold: no business module imports, settings loading, evaluated annotations, entry-point loading or network calls.
5. Separate semantic generated content from a local full-content verification receipt. A receipt becoming stale must not force `generate` for an implementation-only edit or make `generate --check` write files. Interactive exports show their evidence/freshness; strict callers can require a verified basis.
6. Context and graph use the same inspected facts, but context adds explicitly scoped current source content. An exact current-file hash is not proof that a provider implementation agrees with a generated wire interface.
7. Deliver machine-readable JSON and readable exports together. Context is budgeted and reports omissions; graph edges have explicit types, evidence and scope.
8. Keep framework runtime, public SDK tiers, import isolation, transaction ownership and infrastructure configuration unchanged. These are tooling features, not another application runtime.

## Draft fit-review corrections

A second focused check against code and the primary-source guidance identified
and corrected these design gaps before implementation:

- Bare framework installations lack optional PyYAML: cold readers must use
  generated normalized layout JSON plus a raw layout-file identity check.
- Focused provenance needs per-module/closure digests, not a whole-inventory hash
  that invalidates unrelated contexts on every declaration change.
- The generator must feed planned wire bytes to the collector; reading old files
  would create an internally stale index in the same generation invocation.
- Pin the SDK catalog beneath the actual shipped source map, with a Hatch-only
  sdist/bare-wheel resource test.
- Explicit edge directions and witnessed module-level projections are necessary
  for correct dependency hops and SCCs; raw module→contract edges are insufficient.

The receipt design is a Sandloom-specific adaptation to the existing read-only gates and declaration-based generation workflow; it is not a vendor prescription. Format/schema names and CLI flags below are proposals, not shipped commands.

## Primary-source survey

# Context bundles and architecture graph: primary-source practices

Research date and access date for every link: **2026-10-30**. Documentation is living unless a version is stated. RFC 8785 was published June 2020; the Git ignore manual reports its latest change in Git 2.55.0, 2026-06-29. This note does not inspect Sandloom code. **Evidence** records primary-source behavior; **recommendation** proposes design choices, not verified Sandloom capabilities. No model benchmarks or implementation were run.

## Evidence worth adopting

**Aider: compact navigation before whole-repository source.** Its repository map contains important definitions, types and signatures; dependency-graph ranking selects relevant portions. Its token target is configurable but may expand with chat state. Refresh modes include automatic and manual. This supports a budgeted map, not a universal optimal context size. [Repository map](https://aider.chat/docs/repomap.html), [options](https://aider.chat/docs/config/options.html).

**Copilot: scope instructions; do not assume portable precedence.** GitHub distinguishes repository-wide, path-specific and agent instructions. Most clients combine applicable instructions without a general precedence order; GitHub.com Chat documents its own precedence. GitHub recommends short, self-contained instructions and warns that natural-language instructions do not guarantee compliance. [Custom instructions](https://docs.github.com/en/copilot/concepts/prompting-and-context/custom-instructions).

**Continue: content identity and separate exclusion purposes.** Its indexing source describes paths plus content hashes and a Merkle tree for updates; tags distinguish workspace/branch/provider. Separate source constants cover security exclusions and ordinary indexing exclusions. Treat these as design precedents, not proof that an ignore list finds all secrets. [Index synchronization design](https://github.com/continuedev/continue/blob/main/sync/src/README.md), [ignore implementation](https://github.com/continuedev/continue/blob/main/core/indexing/ignore.ts).

**Git ignore is not a confidentiality boundary.** Git says tracked files are unaffected by `.gitignore`; nested patterns have precedence and negation has limitations. A security policy cannot simply equate “tracked” with “safe.” [Git ignore manual](https://git-scm.com/docs/gitignore).

**Static collection needs an explicit execution boundary.** Python warns that `get_type_hints` can execute arbitrary code in annotations. Avoiding application startup is insufficient if extraction evaluates annotations. [Python 3.14 typing](https://docs.python.org/3.14/library/typing.html#typing.get_type_hints).

**Preserve reference semantics when bundling types.** JSON Schema supports recursive references and compound schema documents; bundling must preserve schema identifiers, reference resolution and dialects. URI identifiers do not inherently require network retrieval. [JSON Schema structuring and bundling](https://json-schema.org/understanding-json-schema/structuring).

**Declared communication is not an inferred opposite endpoint.** AsyncAPI 3.1.0 separates operations, channels and messages; send/receive is from the described application's perspective. It explicitly discourages deriving receiver documents by reversing sender declarations, since channels and intermediary infrastructure can differ. [AsyncAPI 3.1.0](https://www.asyncapi.com/docs/reference/specification/v3.1.0).

**Observed graphs depend on observation completeness.** OpenTelemetry's service graph connector pairs client/server or producer/consumer spans. Dropped/unpaired spans are counted, and distributing a trace across connector instances prevents reliable pairing. Thus runtime graphs are evidence over a time window, not exhaustive dependency declarations. [Service graph connector](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/connector/servicegraphconnector).

**Schema compatibility has direction and history.** Confluent distinguishes backward, forward, full and transitive checks; behavior depends on format and configuration. “No schema diff” and “business compatible” are different claims. [Schema evolution](https://docs.confluent.io/platform/current/schema-registry/fundamentals/schema-evolution.html).

## Recommended shared foundation

Build one offline, versioned inventory consumed by both commands. Default extraction should read source, project configuration and generated artifacts without importing business modules, evaluating decorators/annotations, connecting to databases, or making network requests. Unsupported dynamic expressions become explicit unresolved diagnostics with locations. Runtime discovery, if added later, must be a separately named mode with a different provenance kind.

Use stable semantic IDs for modules, contract versions, event channels and DTO symbols; retain repository-relative path, symbol and line span as evidence, not identity. Record producer/parser version, output schema version, extraction mode, options, selected profile and a manifest of input paths plus SHA-256 digests. Commit SHA alone misses dirty edits and untracked selected files. Track generated-artifact identity separately from source identity.

Sort records and diagnostics by documented keys; normalize path separators and text serialization. Keep local absolute paths, current wall-clock timestamps and usernames outside canonical output. RFC 8785 provides a defined canonical JSON scheme; use it if interoperable digesting is required, otherwise specify a narrower deterministic encoding. Hash canonical data separately from display metadata. [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785.html).

Freshness checking should recompute the candidate inventory and digests, detecting additions and deletions as well as modifications. Include selection-policy, ignore-file and configuration changes. Report changed inputs and whether regeneration is needed; do not silently regenerate authoritative snapshots. A derived artifact can be reproducible yet already stale.

## Recommended context-bundle contract

Make module selection mandatory initially; allow explicit task paths/symbols as refinement. Define inclusion in this order: applicable instructions and module contract; required public SDK signatures/invariants; selected contract/event wire definitions; their complete DTO dependency closure; target-module files; bounded relevant tests/examples; optional navigation map. Show inclusion reasons and exclusions. Never silently replace a required definition with a token-truncated fragment.

Keep instruction files verbatim with path, scope and precedence metadata. For Sandloom, apply its documented nearest-contract rule rather than importing Copilot precedence. Retain root rules plus applicable local rules; identify conflicts instead of pretending arbitrary natural-language conflicts can be solved mechanically. Other providers' instruction files can be listed as references, but should not silently become authoritative.

Default source bodies to the selected module and its tests. Permit cross-module information through approved generated contracts/DTO copies, event schemas and public declarations. Do not recursively include sibling implementations merely because AST references mention them. Public SDK context should expose signatures, tier markers, examples and lifecycle restrictions; kernel internals are a separate explicit scope.

Compute DTO closure through nested annotations, aliases, enums, bases and referenced schema definitions, using stable IDs and a visited set for recursive models. Preserve defaults, optionality, serialization aliases and relevant declared constraints. Missing or dynamically computed dependencies must produce a diagnostic, not a fake complete bundle. External schema references resolve only through an explicit local allowlist/catalog; no automatic URL fetching.

Have distinct policy classes: hard sensitive-path exclusions, user/project context ignores, and ordinary low-value exclusions. Apply the first even to tracked files and explicit selections; report a blocked selection without printing its contents. Exclude credentials, environment files, private keys, data dumps, logs and binaries by default. Reject path traversal and symlink escape. Prefer explicit fixture exceptions over broad “include ignored” switches. This reduces accidental disclosure; it is not a complete secret detector.

Offer a deterministic byte budget first; token counts need an identified bundled tokenizer/version or an explicit estimate label. Reserve mandatory content, then select optional whole sections by stable priority and tie-breaker. If mandatory material exceeds budget, fail with required size and remedies. Include content/size accounting and an omission manifest. Emit structured JSON plus readable Markdown from the same inventory; omit checkout-specific absolute paths from shareable output.

## Recommended graph contract and UX

Represent separate typed edges: declares dependency, provides/requires contract, produces/consumes event, and configured service binding. Keep routes and saga/projection memberships as nodes or annotations only when declaration evidence exists. Do not describe an event producer-to-consumer relationship as proof of a particular route-to-handler execution chain. Distinguish available modules, selected deployment profile and unresolved external providers.

Default to **declared** topology. A source-reference layer may show syntactic references, explicitly marked as such; a future observed layer should carry environment, interval, instrumentation coverage and counts. Never merge these silently. Every node/edge needs evidence kind and source location; selecting it should explain why it exists.

Analyze strongly connected components per meaningful edge class. Synchronous declared dependency cycles may violate a framework rule; asynchronous event feedback can be intentional. Report an SCC as a cycle in the declared relation, not demonstrated deadlock or an infinite runtime loop. Unknown dynamic relationships remain unknown. Reuse existing authoritative schema/version checks; represent versioned channels separately, with comparison base and policy in each diagnostic.

Start with deterministic JSON and text/DOT or Mermaid exports; build optional HTML from the same data. Pin renderer/assets, use stable IDs/order and test canonical data independently from layout bytes. Default to no CDN, telemetry or external fonts. Escape labels and embedded JSON; prohibit script/URL injection and unsafe source links. Mermaid's strict mode encodes HTML and disables diagram click actions; prefer application-controlled source navigation. [Mermaid security level](https://mermaid.js.org/config/schema-docs/config-properties-securitylevel.html).

Provide module search, edge-type/profile filters, incoming/outgoing focus, hop limits, reset and visible hidden-node/edge counts. Selection opens source evidence and relevant schemas; exports identify applied filters. Supply a synchronized textual node/edge table. Keyboard-operable controls, visible focus, textual legends and line styles complement color; diagrams need equivalent text information. These follow W3C guidance on [keyboard](https://www.w3.org/WAI/WCAG22/Understanding/keyboard.html), [color](https://www.w3.org/WAI/WCAG22/Understanding/use-of-color.html) and [text alternatives](https://www.w3.org/WAI/WCAG22/Understanding/non-text-content.html).

Acceptance should establish repeatability, closure completeness, honest unresolved states, no application execution/network access, sensitive-path exclusion, freshness detection and evidence-preserving exports. Those are testable product guarantees. Claims that smaller bundles ensure correctness or declared graphs prove dynamic execution should remain outside the specification.


## Repository-fit investigation

# Sandloom context / graph tooling: repository fit

Repository inspection at `98f25be` (the website branch; runtime identical to the baseline above). Paths below are repository relative; numbers are source lines. Shorthand `core/…` and `sdk/…` means `src/sandloom/core/…` and `src/sandloom/sdk/…`; unprefixed filenames refer to the last named directory. This is implementation evidence and a proposed seam, not implemented functionality.

## Existing data and its limits

`core/descriptors/__init__.py:55–96` defines `CapabilityRequirement(capability, instance, version, features, required)` and `ContractRequirement(name, required, reason, methods)`. `ModuleDescriptor` (`:99–147`) retains version, module optionality, migration path / database / chain, `depends_on`, capability requirements, consumed contracts and `issues_tokens`. These are the best existing semantic inputs. Its `database_instances()` (`:158–201`) is the authoritative migration-target inference; do not substitute graph heuristics based on the first database capability.

`core/loader/manifest.py:37–165` defines the frozen manifest entries. `ModuleManifest` carries version / optionality, routes and resolved policies, producer events and schemas, consumers and delivery options, contracts provided/consumed, saga names/versions, projection names/source channels/storage, table names/tenant scoping, migration chain and deviations. `AppManifest` aggregates loaded entries (`:168–235`). It is constructed from a registration draft and loaded descriptor (`:396–427`), not an independent static parser. “Built without I/O” describes this conversion, not an assurance that obtaining a manifest avoids importing an application.

Gaps even in the live manifest: no capabilities, `depends_on`, `issues_tokens`, contract-use reason, DTO ownership/index, declaration file/line, installed distribution identity, or deployment binding. Models are only table + tenant-scoped, not class/source/schema. Projection descriptors have consistency/rebuildable/version (`core/projections/descriptors.py:50–80`), but manifest conversion drops those (`loader/manifest.py:358–370`). Its chain preserves raw descriptor fields, not necessarily a fully inferred database/chain.

`core/cli/generated.py:985–1022` is the exact `module.snapshot.json` format-1 serializer. It retains only module import path, producer schemas, consumer delivery options, consumed-contract required/methods, provided-contract database/methods, saga name/version, projection name/source channels. It drops **even manifest version, module optionality, models, chain, deviations, projection storage and routes**. `ContractUse` preserves optionality, but reason is already lost in `loader/manifest.py:328–344`. Existing snapshots have no source fingerprint, generator identity or declared module-set scope.

Separate route snapshots include resolved route policy (example `sandloom_identity/routes.snapshot.json:1–35`, format 3). Separate contract snapshots include structural method metadata, command/read kind, argument requiredness, returns and idempotency information (example `sandloom_billing/contracts.snapshot.json:1–78`). These are compatibility artifacts, not complete architecture manifests. Neither producer JSON nor contract snapshots alone prove that a provider is installed, selected, or deployed.

`ModuleLoadSummary` has loaded/skipped/failed/explicit lists and consumed-contract local/remote/missing resolution (`descriptors/__init__.py:222–252`). `_preflight_consumes` derives resolution against the live registry and `settings.service_bindings` (`loader/plugin_loader.py:110–157`). Keep that runtime/deployment distinction: semantic graph edges can say “consumes optional X”; a cold graph cannot claim “currently bound remotely” without an explicitly supplied deployment artifact. Installed does not mean loaded; loaded in authoring composition does not mean deployed.

## Cold discovery and output-root seam

The top-level entry point is `sandloom:main` (`packages/sandloom/pyproject.toml:95–96`). Add new static dispatch beside `check` using `_static_import` (`src/sandloom/__init__.py:64–90,273–289`), outside `_CONFIGURED_COMMANDS` and without `_load_test_env_for_authoring`. Existing static tests explicitly launch with `env={}` and no `.env`, and assert `sandloom.core.config.settings` is absent (`core/tests/test_static_cli_no_env.py:23–83`). Extend them with real successful graph/context requests, not only help.

Do **not** call `generated.main()` as a cold reader: top-level `generate` loads `.env.test` (`sandloom/__init__.py:93–104,283–285`); `generated.main` adds source root to `sys.path` and calls `load_composition_app()` (`generated.py:1294–1329`). Importing `generated` also imports composition and runtime schema machinery (`:53–61`). “Generate connects to none” still permits app imports/settings. `load_module_topology` is likewise runtime loading, not static discovery.

Today path authority is **`architecture_rules.yml`, not TOML**. `ArchRules` supplies root, modules path/package, source root, shared package and `shared_dir` (`archcheck/manifest.py:79–160`); `load_rules` resolves paths against the manifest parent (`:183–213`). `cli/add.layout(root)` is a small existing consumer (`cli/add.py:81–118`). `generate --root` defaults to cwd; it reads exactly `<args.root>/architecture_rules.yml` and writes shared files into `rules.shared_dir` (`generated.py:1307–1327`). A new project-wide generated cache can therefore live at `<rules.root>/.sandloom/inspection.json` (proposed path), outside import packages, using project-relative sources; installed sources need distinct distribution-relative locators. No existing project TOML module-path schema was found. If supporting explicit TOML paths, that is a new schema, not existing configuration reuse.

`descriptors/module_names.py:43–81` supplies `InstalledModule(name,path,distribution)` and reads `sandloom.modules` entry-point metadata without loading the entry points. Extend the tooling collector to resolve files using distribution `files`/`locate_file`, recording package version and relevant file hashes; never `ep.load()`. Explicitly report missing source/metadata and editable/namespace ambiguity. The existing `module_name` resolution/ambiguity rules (`:95–163`) should remain authoritative. `sdk/_prim/app.py:17–56` statically detects `register`, but `app_modules()` uses `find_spec` on a dotted app package, which can import parent packages. Similarly `sdk/_prim/docs_wire.py:34–65` infers `<normalized project.name>.shared` from TOML then uses `find_spec`. Reuse their narrow parsing ideas, not their package-resolution path for a strict no-app-import command.

## Generated closure and provenance

Stubs encode `ContractRef("contract.name.vN", Protocol)` plus typed method signatures and docstrings; the module docstring records provider Python module and qualified name (`generated.py:266–311`). This is a useful provenance hint, but not a machine-readable source file/line or hash. Parse stubs as AST, do not import them.

`dto_copies.Resolver` (`cli/dto_copies.py:85–143`) identifies provider-owned DTOs by Python package prefix; already copied DTOs and allowed shared DTO packages remain references, while own DTOs are copied. It traverses nested nominal annotations. Rendering discovers field/default dependencies to a fixed point, topologically orders them and marks each class `@dto_copy("module:qualname")` (`:574–619`). `descriptors/contract_refs.py:23–77` specifies this origin marker. Thus the cold closure is stub imports → DTO copies → their nested annotations/defaults/imports, with origin markers mapping back to owner types. This captures **wire dependencies**, not provider implementation dependencies or DTO validators/methods (deliberately absent, `dto_copies.py:11–17`). Do not infer cross-module contract calls merely from DTO imports.

Producer schema JSON contains nested `$defs`/refs and descriptions but no Python source locator. Event views are not exported: L1 discovery explicitly discards them (`sdk/_decl/module.py:254–269`), and registration only emits producer schemas (`:463–471`). Static `archcheck.api10_rules.event_classes()` returns file, class AST, channel, role and inherited same-file fields (`:620–690`); useful to attach source lines and views during generation. It cannot fully resolve arbitrary aliasing, dynamic classes or cross-file inheritance. The context/graph cache must mark unresolvable cases, not invent edges. Consumer snapshots supply actual channels; projection snapshots supply channel lists.

## Recommended collector / freshness boundary

Extend `generated.plan()` (`:1145–1209`) with one **generated semantic inspection artifact**, built from `app.state.app_manifest`, `app.state.module_summary.loaded` descriptors (`core/app_factory.py:227–230`), structural contract describe data, generated DTO/stub AST, and source locators. The existing plan writes module snapshots only for owned source paths but generates shared stubs/DTO/schema files for installed providers too (`:1063–1071,1154–1162,1173–1207`); mirror this distinction in cache provenance. Keep the collector and cold reader under `core.cli` (e.g. `cli/inspection/`), with a stdlib-only schema/reader. This avoids adding a new core child/layer or kernel→SDK import. Recover views through the existing static extractor at generation time; changing L1 discovery to retain them would cross more interfaces than needed. Add descriptor capability/ordering/source metadata to this cache directly, without forcing all missing fields into the frozen public `AppManifest`.

Fingerprint a canonical **file membership inventory plus content hashes**, not only files already present: add/delete of a module package, declaration, conventional subpackage, tests or relevant agent instructions must invalidate the appropriate bundle/index. Include `architecture_rules.yml`, app composition source, relevant pyproject/lock/distribution metadata, selected module-set identity, framework/cache schema version and provider artifact identities. Exclude secrets and generated output itself from recursive input hashing; model generated dependencies explicitly to avoid circular fingerprints. Verify hashes cold; generate/test can recompute the semantic cache from the same authorized booted authoring composition and use `Generated`/`apply` (`generated.py:1260–1289`) for stale checks and no-writing-on-incompatibility. Do not reuse the AST analyzer's mtime+size cache as persistent freshness proof (`archcheck/analyzers.py:36–58`).

Legacy snapshots support a useful **unverified snapshot graph**, never an attestably current source graph. A content fingerprint proves the enumerated inputs match the generation run, not that arbitrary runtime/env-dependent registration is reproduced. Record the authoring composition's explicit selected modules and unresolved dynamic inputs; never present cached authoring binding resolutions as deployed truth. Partial `generate module` must not silently replace a whole-project cache with a partial module set: either regenerate the complete index or explicitly scope/cache separately.

## Gates and packaging

Nearest contract is `src/sandloom/core/AGENTS.md`. Tooling layer already includes `cli : testing : contract_testing : archcheck`; exhaustive layering and acyclic siblings remain enforced (`.importlinter:21–45`, `tests/architecture/test_kernel_import_contracts.py:24–37`). Kernel never imports SDK (`.importlinter:118–123`); collector must not import the SDK's event discovery implementation merely to obtain views. Kernel-local imports require optional/cold reasons; lint suppression ratchet applies.

New files must be explicitly listed in public-export allowlist `tools/packaging/public-source.json` (existing CLI list `:1096–1135`); `tests/architecture/test_source_export.py:68–87` rejects unreviewed tracked paths. Package source map already ships `src/sandloom` (`packages/sandloom/pyproject.toml:113–124`); `tools/packaging/package_build.py:33–57` recursively includes runtime assets but strips tests, and `:85–106` builds self-contained sdists. Therefore code/templates placed inside `core.cli` ship automatically; repository-only files outside the package do not. `check_package_distributions.py:254–305` discovers all CLI commands and checks lean-wheel help; `:216–236` checks sdist round trips. Add actual cold artifact commands to lean smoke coverage, since help cannot prove data files shipped.

Reuse/extend `test_app_manifest.py:84–144`, `tests/sdk/test_sdk_generated.py:773–889` (stale/atomic generation), `tests/sdk/test_sdk_contract_dtos.py:272–294,632–658` (copy origins/collisions), static CLI subprocess tests, architecture tests and installed distribution smoke. Add fixtures for absent/stale cache, old formats, module add/delete, installed/provider absent, dynamic declarations, and partial module-set scope. No repo files changed and no backend checks were run for this research-only task.
