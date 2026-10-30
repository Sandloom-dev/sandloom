# Context bundles and declared architecture graph

**Status: Implemented on this branch — unreleased.** Date: 2026-10-30.
Targets the existing 0.7.0a3 architecture. This document records the design for module authors and agents.
See [implementation and validation](implementation.md) for delivered behavior and limits.
[Evidence](research.md) · [Implementation plan](plan.md) · [Draft usage guide](usage.md) ·
[Draft agent skill](skills/sandloom-module-context/SKILL.md).

## 1. Outcome and limits

An agent can invoke the CLI itself to discover modules, obtain bounded,
traceable context for one business module and inspect its connections. A person
can read the same facts as Markdown or an offline diagram. Manual copy/paste or
a person preparing the bundle is not a prerequisite.
Both views come from the same framework declarations and wire artifacts.
No extra declarations are required from application authors.

The first release includes:

- a shared, versioned inspection model;
- compact Markdown context for direct LLM consumption and structured JSON for integrations;
- a declared module/contract/event/projection graph as JSON, Mermaid and an
  offline HTML viewer with an equivalent text table;
- source evidence, omission reporting, compatibility with existing generated
  DTOs and explicit freshness/coverage information.

It does not include runtime tracing, production snapshots/replay, embeddings,
LLM calls, a hosted service, IDE/MCP integrations, a workflow engine or
architecture enforcement based on a graph viewer. No new infrastructure is needed.
A context bundle is navigation and input preparation, not a correctness proof.

## 2. Small public interface

```bash
sandloom graph --view modules
sandloom context notes
sandloom context notes --format json --output notes-context.json
sandloom context notes --format json --detail full
sandloom context notes --file services.py --file tests/test_notes.py --max-bytes 131072
sandloom context notes --require-verified

sandloom graph --format html --output architecture.html
sandloom graph --module notes --hops 1
sandloom graph --format json --detail full
sandloom graph --format mermaid --module notes --hops 1
sandloom graph --require-verified --format json
```

`context` requires a logical module name, unambiguous import path or a stable
`module:<name>` ID returned by discovery, resolved with existing module naming
rules. Graph focus accepts the same selectors. `context` defaults to compact
Markdown on stdout in terminals and pipes alike: a short status header, scoped
rules, interfaces and code blocks. It is intended for direct agent consumption
as well as people. Use `--format json` when a program needs structured fields.
Code is not JSON-escaped merely because its reader is an AI agent.
`--file` is repeatable and makes selected **module-local** files mandatory;
paths are relative to that module. It never permits sibling source access.
`--max-bytes` defaults to 131072 UTF-8 bytes, counting the entire serialized
output, metadata and omissions included. It is not called a token limit.

`graph` defaults to plain text on stdout TTY and compact JSON in a pipe,
redirect or `--output`; explicit `--format` wins. `--output` writes the
chosen format atomically. Its formats are `text`, `html`, `json`, `mermaid`; a filename
extension never silently selects the format. `--view modules` is a bounded
JSON discovery view containing module IDs/import paths, source availability,
summary counts and freshness. It does not require a deliberately failing
command or return the entire relationship graph. Its response identifies the
view and says relations were not requested; absent relations are not empty
relationships. Other views use the complete/focused graph contract below. `--module` focuses a
module; `--hops` is a non-negative integer, defaults to 1 with a focus, and
is rejected without a focus. Filtering retains evidence and reports hidden
node/edge counts. The complete underlying inventory remains distinguishable
from the selected view.

Both commands accept `--root PATH`, default cwd, and read the inspection document
under that project root. The document carries normalized, root-relative layout
from the existing ArchRules at generation time, plus the raw layout-file hash.
Cold readers do not call `load_rules` or parse YAML: PyYAML is currently optional
and absent from a bare framework install. They validate containment and compare
the raw `architecture_rules.yml` identity; a layout mismatch is a hard error
requiring generation before resolving ownership. No parent-package `find_spec`,
application import or `.env` loading is allowed. Unknown/ambiguous
modules fail with known candidates and a concrete remedy.

Without `--output`, stdout contains exactly one response/artifact in the requested
format. JSON responses follow the agent protocol below, including errors. Human
logs belong on stderr; diagnostics remain machine-readable in the JSON response. All references,
identities and input paths in a shareable artifact are project-relative or
logical distribution-relative locators, never absolute checkout/home paths.
Existing output files require `--force`; a source, policy or managed generated
input can never be an output destination. Failure leaves the previous output
intact. Output targets cannot be symlinks.

Exit codes: 0 = usable artifact, possibly with explicit warnings; 1 = invalid
inputs/artifacts, missing mandatory information, unsafe selection or I/O
failure; 2 = CLI usage error; 3 = verification required but unavailable.
No partial successful artifact is emitted on a nonzero exit; JSON mode emits
one complete structured error response instead. Implementation must register
public diagnostics with the existing error/explanation machinery; this proposal
does not reserve unverified numeric error-code slots.

### 2.1 Agent protocol: first-class, not a human-output parser

Defaults depend on the information: compact Markdown for code/rules context,
compact JSON for graph/discovery relations in pipes and plain text on stdout
TTY. Output mode uses `isatty`, never agent identity or an environment switch. An agent can read Markdown directly; a program chooses
explicit JSON instead of scraping Markdown. The cold `context`/`graph` commands are noninteractive: no confirmation prompt,
browser opening, pager, progress animation or fallback to app imports/network/generation.
`graph MODULE`, `graph impact ITEM` and `graph diff REV` are positional forms;
explicit revision diff reads the committed inspection index without checkout or
fetch, while saved-file diff still works without Git. A separate `explore`
authoring command explicitly generates, saves an offline viewer in the ignored
`.sandloom/cache/` directory and opens its local file URL. `--no-generate` and
`--no-open` expose those steps independently. Generation failure never opens
a stale artifact. A caller decides whether to execute
a proposed recovery action according to its own permissions.

Every JSON response uses one versioned envelope:

- `schema`: `sandloom.tool_response`; `schema_version`: 1;
- `command`: `context` or `graph`; `ok`: boolean;
- `kind`: `context`, `graph`, `modules`, `artifact` or `error`;
- `selection`: normalized requested scope; resolved stable IDs when available;
- `freshness`: structured state/evidence, or null when not established;
- `data`: the appropriate structured result, or null on failure;
- `diagnostics`: typed records, never a requirement to parse English messages.

Context `data` exposes sections/files/definitions individually with IDs, content,
source references, byte accounting and omissions. Graph `data` exposes typed
nodes/edges and witness IDs directly. Definitions are referenced by ID rather
than repeated in every section. `--detail compact` is the default in both context
formats and graph JSON: omit per-file hashes and repetitive provenance records,
not business facts. Keep schema/version, scope/coverage, freshness state and
closure digest, uncertainty, diagnostics, mandatory content and omission ledger.
`--detail full` adds file hashes, inclusion reasons and complete provenance/
evidence records; it changes metadata depth, not selected business scope or
freshness. Graph compact records retain relation type, endpoints, requiredness
and witness references, plus one shared minimal evidence/source dictionary.
Full adds evidence details without inventing new relationships. Empty optional
diagnostic fields can be omitted under the documented versioned schema.
JSON uses deterministic compact separators without display indentation. Plain
meaningful field names remain; no custom compressed syntax or one-letter keys.
Select content using the compact representation for the requested format and
budget. Full detail enriches that selection; if its metadata exceeds the budget,
fail with the required size rather than evicting business content.
Display prose/HTML is not the only way to retrieve a fact. `--view modules` returns `kind: modules`
with a catalog and `relations_included: false`, not a fake edge-free graph.

Diagnostics have stable `kind`, `severity`, human `message`, source locations,
related IDs, and typed `next_actions`. Public explanation codes are included
when assigned by the existing catalogue; clients branch on the documented
machine kind and exit status, not translated wording. A next action contains a
known action kind, argument array and declared side effects, never an opaque
shell string from a project document. For example, regeneration is explicitly
labelled as writing generated files. There is no automatic permission grant.

Illustrative error envelope (not an implemented command):

```json
{
  "schema": "sandloom.tool_response",
  "schema_version": 1,
  "command": "context",
  "ok": false,
  "kind": "error",
  "selection": {"requested_module": "unknown", "module_id": null},
  "freshness": null,
  "data": null,
  "diagnostics": [{
    "kind": "unknown_module",
    "severity": "error",
    "message": "No module named unknown",
    "locations": [],
    "related_ids": [],
    "next_actions": [{
      "action": "list_modules",
      "argv": ["sandloom", "graph", "--view", "modules"],
      "side_effects": []
    }]
  }]
}
```

For JSON-mode usage, freshness, budget, closure and path failures, stdout is
still exactly one parseable error envelope and the exit code is nonzero. Explicit
`--help` remains conventional help. A failed command cannot overwrite an earlier
output file. With `--output`, successful stdout is a small `kind: artifact`
acknowledgement with requested output locator, format, byte count and SHA-256;
the target holds the complete selected artifact. This avoids dumping a large
bundle a second time. This acknowledgement is JSON regardless of target format;
the explicit file-writing mode has its own machine contract, including JSON
errors. Portable artifact content does not gain absolute home paths from the
local write acknowledgement.

Graph JSON and module discovery accept the same `--max-bytes` limit as context;
exceeding it returns a structured remedy to focus by module/hops or raise the
budget. No silent node/edge truncation. HTML/Mermaid remain explicit presentation
exports; `--max-bytes` is rejected for non-JSON graph formats rather than silently
ignored or pretending to limit embedded renderer assets. `--detail` on graph
HTML/Mermaid is rejected; those renderers have their own evidence presentation.
In Markdown mode without `--output`, failures leave stdout empty, print an actionable
diagnostic on stderr and return the same exit codes. Callers needing structured
failures explicitly select JSON. Metadata reduction never conceals a stale basis,
missing mandatory definitions or omissions.

The expected agent loop is: discover modules → choose a stable module ID →
request its context and a focused graph → inspect freshness/omissions/diagnostics
→ request additional module-local files or an explicit authorized refresh when
necessary → modify the selected module → run the existing gates. No stage needs
a person to copy a diagram, interpret terminal colors or paste a bundle manually.

## 3. One semantic model, two presentations

```text
Existing trusted authoring generation
  descriptors + AppManifest + contract describe data + generated wire artifacts
                            |
                 semantic inspection document
                            |
           cold reader + current scoped files + provenance
                       /                  \
             context Markdown/JSON     graph JSON/Mermaid/HTML
```

The shared implementation lives under `sandloom.core.cli.inspection`, within
the existing tooling layer. Public CLI handlers remain small. The cold reader
and its data types do not import the generator, composition root, settings,
business modules, SDK facades or driver factories.

### 3.1 Semantic artifact

Proposed path: `<rules.root>/.sandloom/inspection.json`. This generated semantic
artifact is versioned/committed like other declaration artifacts. It contains
no full implementation-source hashes, source file bodies, current timestamps,
secrets, endpoint URLs, evaluated settings or transient runtime health.

Its schema is `sandloom.inspection`, version 1. Required sections:

| Section | Content |
| --- | --- |
| `layout` | Normalized root-relative source/module/shared paths, module ownership locators and raw layout-policy identity; no YAML dependency in readers |
| `scope` | Authoring composition identity, exact selected logical module IDs/import paths, and coverage; never an implicit claim of all installed/deployed modules |
| `modules` | Logical/import/distribution identity; module version/optionality; declared ordering; capabilities/features/requiredness; tables/tenant flags and supported migration metadata |
| `nodes`, `edges` | Typed graph facts with stable semantic IDs and evidence locators |
| `wire_documents` | Contract describe/stub/DTO and event schema/view references; schema IDs/dialects; artifact identity; provider ownership and DTO origins |
| `source_locators` | Relative source path plus symbol/declaration identity; unavailable installed-source locations remain explicit |
| `unresolved` | Dynamic/unsupported declarations and metadata gaps, including the affected scope and collection reason |
| `producer_protocol` | Model/collector protocol version; no build timestamp or checkout-specific details |

Readers reject unsupported schema versions and unknown required variants;
there is no silent format upgrade. Optional additive metadata can be retained
without inventing semantics. Breaking field/identity/edge semantics require a
new schema version and an explicit migration/regeneration path.

Use `ModuleDescriptor` plus loaded descriptors for information missing from
`AppManifest`. Do not expand the frozen public manifest solely for this UI.
Preserve contract optionality/reasons, producer/consumer direction, projection
sources and event versions. Collect event views through existing static
architecture extraction during generation; do not add a forbidden core→SDK
import or assume views were already retained by L1 discovery.

Do not invent source locations or capability sets. Absent data is unavailable,
not an empty dependency list. Producer schemas describe wire shape; copied DTOs
omit provider validators/implementation by design and must be labelled accordingly.

Generation uses the same compatibility preflight and normalized registrations
as existing snapshots. The collector consumes **planned `Generated.content` bytes**
for stubs/DTOs/schemas from the current generation plan, not their old on-disk
versions. Missing wire files and compatible changes must become consistent in
one invocation, followed immediately by a passing `generate --check`. A rejected breaking change writes no inspection artifact.
Establish source membership/content identity before application composition and
compare again after collecting/planning. An edit during generation must not
produce a falsely verified receipt; detect it and withhold the new index/receipt
with a concrete retry diagnostic. Fingerprinting only after app import is
insufficient because registered objects may reflect older bytes.

Commit the new semantic artifact after its dependent generated wire files;
use an atomic replace for the semantic file. An interrupted write must be
absent, previously valid or detectably inconsistent, never silently current.

Partial `generate notes` cannot replace the project document with a notes-only
graph. Recompute the full semantic inventory from the already composed app,
while preserving the existing selected-module behavior of other generated files;
verify selected and unselected wire dependencies before committing the index.
If unselected wire artifacts would need changes, preserve the successful selected-file
generation and its existing exit behavior, retain the prior project index and emit
a specific warning to run full `generate`. Do not issue a fresh receipt or report
the project graph as current. A selected-module success is not full-index verification.

### 3.2 Identity, evidence and deterministic serialization

IDs identify logical entities, not paths or translated labels: e.g.
`module:notes`, `contract:stats.quota.v1`, `event:notes.created.v1`.
Consumer/projection IDs use their full registered identity and owner. Encode
reserved characters unambiguously. Detect conflicting identities before export.
Renderer identifiers are derived/escaped IDs; no label becomes executable syntax.

Each fact includes `evidence_kind` and a locator. V1 kinds are
`registered_declaration`, `generated_wire`, and `source_reference`; no
`observed_runtime` facts are produced. A source reference is not promoted to a
registered consumer or a demonstrated runtime call. Resolve current line spans
from source symbols/AST when possible; line numbers are evidence, not identity.
Unresolvable/ambiguous locations remain so labelled.

Sort nodes/edges/diagnostics/files by documented stable keys. Normalize path
separators and serialization; reject non-finite JSON numbers. Specify one
canonical UTF-8 JSON encoding and digest its bytes. Do not claim RFC 8785
compliance without implementing it. HTML layout bytes are not the semantic
identity; JSON model reproducibility is tested separately from layout.

## 4. Freshness without a generate-after-every-edit tax

A full-content hash embedded in a byte-compared generated file would make an
ordinary service-function edit fail `generate --check`. That is a DX regression
and is prohibited. Split the following concerns:

1. **Recorded semantic declarations:** committed inspection artifact, compared
   against recomputed declarations by existing trusted generation/checks.
2. **Verification basis:** local ignored receipt at
   `.sandloom/cache/inspection-receipt.json`, written only by explicit successful
   generation; includes **per-module and transitive dependency-closure semantic
   digests**, candidate file membership/content hashes, generator/SDK catalog
   identity and relevant dependency artifacts. A separate whole-project digest
   is used for full-project verification, never as the focused-bundle invalidator.
3. **Current export inputs:** files read by the cold reader for this export,
   with current hashes, selection policy and omission accounting.

`generate --check` and `sandloom test` do not write the receipt or other generated
files. Implementation-only/comment edits do not change semantic generated
output solely because their bytes changed. A receipt becoming stale is not a
new failure in those existing gates.

Cold readers compare the receipt against current input **membership and content**:
additions/deletions, instructions, layout/selection policy and dependency changes
must be noticed, not just edits to previously listed files. Ignore mtime/size as
proof. Use bounded reads with a stable-file check/retry; fail if files continue
changing during capture. Exclude the generated output and receipt itself from
recursive input hashing.

Interactive exports state one of `verified`, `missing_receipt`, `source_changed`,
`dependency_changed`, `partial`. `verified` means that enumerated source inputs
and artifacts match a known successful authoring generation, not that a running
deployment or arbitrary environment-dependent Python behavior is proven.

By default a graph may visualize recorded declarations and a context may combine
those recorded interfaces with **current** selected source, but must prominently
report any unverified/changed basis and the remediation. These are never labelled
live/current-runtime topology. `--require-verified` fails with exit 3 unless the
entire selected scope and mandatory dependency closure are verified. Unsupported
mandatory wire definitions still fail regardless of this flag.

A fresh clone without a receipt can therefore inspect recorded facts without
booting an app; explicit generation enables verified exports. Existing format-1
snapshots are not silently upgraded to a fully verified document. A missing
semantic artifact fails with `uv run sandloom generate` as the migration remedy.
Changing an unrelated module invalidates full-project verification but should
not invalidate a focused bundle whose complete dependency/input closure is
unchanged; use per-module receipts plus global layout/policy dependencies.
A focused context uses its closure digest as canonical provenance; an unrelated
change to the full inspection file is not a required input hash for that context.
Test both an unrelated implementation edit and an unrelated declaration change
followed by regeneration: the unchanged focused scope must still verify.

## 5. Context bundle contents and trust

### 5.1 Mandatory before budgeted detail

1. Module identity, owner scope, provenance/freshness, diagnostics and source map.
2. Applicable project/role/module instructions and core invariants. Honor
   Sandloom's explicit module-author contract mapping as well as ancestor AGENTS;
   nearest scoped contract refines the broader one. Preserve original text,
   path and scope; do not mechanically solve arbitrary natural-language conflicts.
   Referenced external instruction files/URLs are not fetched automatically.
3. Public SDK beginner signatures/invariants plus actually referenced advanced
   names and their tier/reason markers. No kernel implementation bodies.
4. Consumed and provided contract interfaces relevant to the module, plus the
   complete required DTO closure; produced/consumed event schemas and views.
5. Explicit `--file` module-local selections and verification commands.

Then include target-module implementation/tests/examples as whole files or
whole explicitly identified sections, in a documented deterministic priority
order. No sibling implementation, broad repository dump or LLM ranking is used.
List every omitted candidate and why. Mandatory content overflow fails with
required size and remedies; it never drops rules, signatures or a nested DTO.
Optional omission does not claim that the bundle contains the full module.

SDK signatures/docstrings are sourced from a structured catalog generated and
checked during framework authoring (extend existing public reference tooling).
Pin the shipped resource to
`src/sandloom/core/cli/inspection/sdk-catalog.json`; the current package source
map includes it in wheels and standalone sdists. Bare CLI consumption is read-only
and must work with both PyYAML and repository docs/tools absent.
Do not generate it by importing the framework in a Hatch build hook: standalone
sdists must continue to build with Hatch alone. API baseline/reference changes
must also invalidate/check this catalog.

### 5.2 Wire dependency closure

Reuse `@dto_copy("provider:qualname")` origins and generated stub imports.
Traverse nested annotations, aliases, enums, bases, relevant defaults/constraints
and local `$defs`/`$ref` with a visited set. Preserve recursive references and
schema dialect/identity. Do not execute annotations, decorators, factories,
`eval`, `get_type_hints` or dotted-parent package discovery.

Any explicitly supported external schema reference resolves through a local
allowlisted catalog. No implicit HTTP fetch. Missing mandatory symbols or
unsupported dynamic dependencies produce precise diagnostics and no fake-complete
bundle. Generated wire DTOs are not provider business behavior or authorization
semantics; include their available public docstrings and restrictions too.

### 5.3 File policy and output

Source bodies belong only to the selected checkout-owned business module and
its tests. Installed providers contribute approved public wire artifacts and
metadata, not their implementations. MVP rejects an installed-only focus that
has no owned source directory; the diagnostic explains the supported source scope.

Separate hard sensitive-path exclusions from project context ignores and ordinary
low-value exclusions. Hard exclusions also apply to tracked files and explicit
`--file`, both before and after canonical path resolution. Reject traversal,
symlink escape/cross-owner aliases, private keys, real environment files, database
dumps, runtime logs, binary/vendor/build/VCS data. Use narrow reviewed fixture
exceptions rather than a general include-secrets switch. Do not print rejected
file contents or matching secret values in diagnostics.

Reuse credential-pattern checks before emitting content, but state their limits:
this is not a guarantee that source can never contain a secret. Context is
exported locally only, never submitted to a model/network service automatically.

Markdown source blocks use collision-safe fences and explicit file/scope labels;
JSON preserves content as escaped data. File comments/docs are source material,
not instructions that override the user's/system's authority. Every output includes
actual byte accounting, selected/omitted files, freshness, semantic digest and
installed framework/catalog versions. Full detail additionally includes per-file
hashes and inclusion/provenance records; compact source labels remain traceable.
No volatile timestamp is needed in canonical output.

## 6. Declared graph semantics and viewer

Node types: module, contract, event channel, consumer, projection, capability.
Table and saga summaries are optional module details in V1; no invented
per-step causality is inferred from them. Kernel/SDK may be a labelled shared
reference in the overview, never a discovered business-module/provider node.

Edge types:

| Kind | Direction | Meaning |
| --- | --- | --- |
| `provides_contract` | module → contract | Provider declaration for a named versioned contract |
| `requires_contract` | module → contract | Consumer declaration; required/optional and methods retained |
| `produces_event` | module → event | Producer schema declaration for that channel/version |
| `consumes_event` | event → consumer | Subscription with delivery options |
| `owns_consumer` | module → consumer | Registered consumer ownership |
| `projects_event` | event → projection | Projection source-channel declaration |
| `owns_projection` | module → projection | Projection ownership |
| `requires_capability` | module → capability | Explicit capability/instance/features/requiredness |
| `declares_ordering` | dependent module → prerequisite module | Explicit load/order requirement |

Capability edges show explicit requirements; their absence is not evidence that
a module never uses default infrastructure. Sharing a capability or DTO never
creates an inter-module call edge.

For focus/hops and SCCs derive distinct module relations with witnesses:

- **Contract dependency:** caller → provider, from caller `requires_contract` →
  contract ← `provides_contract` provider. Retain both raw edges and the contract
  as evidence. Required/optional stay distinct; an absent provider remains an
  unresolved contract, not an invented module.
- **Event impact:** producer → consumer-owner, from producer → event → consumer
  ← owner; similarly for projections. These are declared potential delivery
  relationships, not proof that a handler emitted an event or called a contract.
- **Ordering:** the direct `declares_ordering` relation, kept separate from both.

A hop means one module-to-module relationship in the selected projection, not
one raw edge through a contract/channel. Default focus includes incoming and
outgoing contract/event neighbors; every selected relationship brings along its
intermediate nodes and witness edges. Capability detail is attached without
consuming a module hop. The viewer/export states which projection/filter is used.

Unresolved external providers, optional absent contracts and schema views remain
visible with their status; absence from an authoring composition is not proof
of absence in a deployed system. Available/installed/selected/deployed are
separate states. V1 does not read `.env` or infer active RPC destinations from
cached authoring settings. Deployment overlays and live traces are deferred.

Analyze SCCs on the derived module relations, not on raw module→contract
edges. Required contract dependencies, optional dependencies, ordering and event
impact remain distinguishable. A synchronous-requirement SCC is reported
as a declared cycle, not proof of a deadlock. Event feedback cycles can be
intentional; do not reject them globally. Reuse existing compatibility engines
and error codes instead of creating another schema-evolution rule set.
Never claim that event X causes RPC Y merely because both connect to a module.

The HTML viewer runs offline with no CDN, telemetry, remote fonts or app server.
It offers module search, relation filters, incoming/outgoing focus with hop
limits, reset, visible omission counts, provenance and an evidence inspector.
The overview collapses high-cardinality channel/consumer detail; expanding a
module shows actual intermediate entities without losing edge semantics.
Always provide a synchronized accessible node/edge table and downloadable
canonical JSON. Controls must work by keyboard, with visible focus; line style
and labels carry meaning in addition to color. A mobile reader can use the table
without panning a tiny canvas.

Renderer choice is a phase-0 decision based on measured offline footprint,
layout on representative cyclic/high-fan-out graphs, accessibility and licensing.
No renderer becomes a Python/backend dependency or changes the inspection
schema. Source locations are structured data, not executable editor/shell URLs;
labels/embedded JSON are escaped and untrusted link schemes are prohibited.
Mermaid output is an interchange view, not a guarantee of identical layout
across third-party renderer versions.

## 7. Acceptance and compatibility

- Agents can independently discover modules, read compact Markdown context and
  consume graph relations; integrations can use the versioned JSON protocol for
  both commands. JSON-mode non-TTY success/error output parses as one object.
  No prompts, colors, banners or prose parsing are required for integrations.
- Compact/full outputs have identical selected business facts and mandatory
  closure; metadata depth alone changes. Measure actual bytes and, with a named
  tokenizer, tokens on representative bundles before claiming savings.
- Diagnostic kind/exit code and structured recovery remain stable when human
  wording/localization changes. Output-file acknowledgements stay bounded.
- Same selected inputs/options produce byte-identical canonical JSON and context
  output. Dirty/untracked selected files and add/delete changes are accounted for.
- Cold success paths work with empty app environment and absent PostgreSQL/Redis.
  A module `__init__` import sentinel and socket/subprocess traps prove no app
  execution/network/backend probing; cover non-help invocations from installed wheels.
- Existing module naming, DTO copies, views, optional contracts and compatibility
  checks retain their behavior. No new public SDK symbols or mandatory declarations.
- Context never crosses selected-module source ownership; mandatory DTO closure
  is complete or fails. Byte budget and omission accounting are exact.
- Source/provenance uncertainty is explicit; strict verification is enforced.
  Unrelated declaration regeneration preserves a matching focused receipt.
  Raw layout changes fail safely without optional YAML parsing.
  Comments/service-body-only edits do not create a generate-required gate regression.
- Graph evidence is correct for fixtures with absent optional providers, multiple
  versions, aliases, event cycles, partial module sets and unresolved declarations.
- All paths/labels/content survive hostile HTML/Markdown/JSON fixtures without
  script execution, escaping owner scope, or leaking denied file content.
- Semantic generation refusal/interrupt/partial generation, source edits during
  generation, same-size/same-mtime content changes, installed distributions,
  wheel/sdist assets, import-layer rules and source-export allowlists are covered.
- A module-context skill is discoverable in framework and scaffolded app skill
  directories. It teaches direct CLI use, bounded expansion, freshness handling
  and existing quality gates; public docs and CLI help match those commands.
- Runtime request behavior, transactions, discovery and event delivery are unchanged.

No paid agent DX experiment runs as part of design or default tests. Implementation
must follow the repository's generator/API-change evaluation policy, with any
paid agent evaluation separately authorized. Effectiveness claims require measured
navigation/context/correctness results, not just a passing structural fixture.
