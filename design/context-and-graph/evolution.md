# Architecture explorer evolution

Implemented on `codex/context-graph-design`, 2026-10-30; unreleased after 0.7.0a3.
The initial implementation and its earlier validation remain documented in
[implementation.md](implementation.md). This extension follows the
[evolution research](evolution-research.md) and [UX checkpoints](ux-research.md).
Research/checkpoint findings are source heuristics and scenario checks, not a
participant usability study or measured productivity claim.

## Shared model and boundaries

Explicit generation collects resolved route policy from its planned snapshots,
registered saga definitions (including versions, pivot, compensation availability,
unit boundaries and safe local callback locations) and contract describe data.
It inspects metadata without invoking step callbacks or argument functions.
Multiple saga versions have distinct stable IDs. Existing schema-1 indexes remain
readable; absent process/interface metadata remains unavailable, not fabricated.
The verification basis covers these declarations as well as existing sources,
wire files, package versions and SDK catalog. Cold inspection still has no app,
SDK/settings, environment, backend or implicit generation dependency.

Pure contract and event-schema compatibility were extracted into the existing
lower `core.contracts` layer. Authoring/runtime interfaces re-export the same
functions; the offline diff reuses their rules rather than duplicating them.
Generation fingerprints stream raw bytes: export-only secret/UTF-8/size checks
must not block ordinary generation. Unavailable optional inspection metadata
withholds new verification; a detectable source race aborts before publication.

## Product surface

- Overview: strongly connected components condensed into a ranked DAG layout,
  semantic layers, search with immediate context, module/direction/distance
  filters, pan/zoom/fit, synchronized entity/relationship lists and inspector.
- Processes: separate declared forward and compensation paths; saga states use
  a state diagram. Route sequences show conditional framework phases and an
  opaque handler. Mermaid represents compensation as an explicit alternative.
- Changes: JSON/HTML comparison of two saved inspection indexes, with basis,
  reconciled counts, before/after declarations and compatibility evidence.
  Historical paths/code are not loaded; implementation bodies are excluded.
- Impact: direct interface/owner witnesses followed by potentially affected
  module reachability. Transitive edges can involve other interfaces, so results
  explicitly overapproximate possible impact and cannot justify skipping gates.
- Context focus: stable interface/process ID, whole public wire/DTO closure,
  scoped instructions, explicit owned helpers/tests and an omission ledger.
  Source matching is a navigation hint, not a complete static call graph.
- Offline HTML embeds local assets/data, supports light/dark, keyboard tabs and
  SVG actions, click alternatives to dragging, mobile inspector navigation,
  JSON/SVG downloads and saved URL state. Selection IDs include process version
  and representation; they cannot silently resolve to a different path's step.

HTML is a bounded export of its selected composition. Its infrastructure layer
shows declared requirements, not actual deployment bindings or backend health.
There is no observed trace replay, live topology, automatic control-flow inference,
provider source exposure, publication or package version change in this work.

## Acceptance record

The implementation includes regression scenarios for real saga collection without
callback execution, resolved route policy, conditional public/intent lifecycle,
wire-closure focus and explicit helpers, impact witnesses, semantic body-only
stability, contract/event incompatibility, argument errors, safe artifacts and
inspection failures/source races during generation.

Browser checkpoint: actual Academy declarations provide three business modules,
two module relations, one four-step saga and five routes. Compensation was
verified as a separate two-step path. Its selected refund step survives reload
with the same representation/ID. At 390px the details control moves focus to the
inspector; the page itself has no horizontal overflow. Diagram scrolling remains
local to its two-dimensional canvas. Further validation results are recorded below.

Validation completed locally:

- Full quality gate: 5,100 full-suite tests and 1,273 architecture tests passed,
  with existing skips/expected failure retained; lint, formatting, types,
  architecture/imports, snapshots, drift, public API and typing checks passed.
  The focused inspection suite has 45 passing cases, including parallel saga
  versions and ambiguous process selection.
- All four wheel/sdist distributions rebuilt consistently, license metadata and
  layering passed, and bare/standard installs passed. The final viewer revision
  was rebuilt again, its three embedded assets compared byte-for-byte with source,
  and the installed wheel passed cold-tool success with app/SDK/settings/YAML
  imports and network blocked.
- Five fresh wheel-installed applications passed: no batteries, identity, tenancy,
  billing and all three. Each generated/checked, produced verified module/focused
  context, listed process metadata, exported HTML, ran impact and zero-change
  diff queries, and received matching agent skill mirrors.
- Every checked-in example regenerated and passed `generate --check`. Unified
  website build, explicit architecture checker, skill validation and reviewed
  source-export checks passed. The paid AI-agent DX experiment stays deferred.
- Browser checks reconciled the dense fixture's 20 modules / 80 module relations
  across SVG and lists, with zero overlapping node rectangles. Searching `m07`
  yielded seven visible nodes/eight relations/one match. Interface search yielded
  five nodes/four relations/two matches in both representations.
- At 320px, an isolated Unicode/long-name module remained selectable with its
  full name in the inspector and no page overflow. Empty search and a selection
  outside filters were explicit. Enter retained SVG focus; ArrowRight activated
  and focused the Processes tab. HTML impact exposed direct witnesses and the
  suggested module test; zero-change diff reconciled all three counts.

No real participant study, screen-reader audit, 200% text-zoom audit or observed
runtime tracing was conducted. No measured human/AI productivity claim is made.

Compact-output checkpoint: the three-battery authoring graph dropped from
85,799 to 18,640 UTF-8 bytes (78% smaller) by deferring full module/route/process
bodies to explicit detail and process queries. Nodes, raw edges, module relations,
cycles and unresolved facts were compared and remain identical. Focused regression
coverage is now 46 passing cases; this measures serialized bytes, not LLM tokens
or productivity. Linux fallback CI on 08801b9 passed backend-static, docs and all
five examples in 2m15s.

Final focus-closure regression: selecting a consumer/projection preserves its
incoming event interfaces; infrastructure focus retains the module's public
closure and owned source. The focused suite has 47 passing cases. This closes
the graph-inspector-to-agent-context path as well as direct contract/event focus.


## Independent audit corrections (2026-10-30)

All nine actionable findings from the independent code/spec/UX review were
addressed before owner review:

| Finding | Correction and regression evidence |
| --- | --- |
| Forced export can overwrite Git metadata or unrelated files | Reserved/VCS directories and project inputs are protected; force only refreshes a recognized Sandloom inspection artifact. An optimistic destination check preserves concurrent edits/creation. CLI and filesystem regressions cover all three formats. |
| Future/malformed receipt can be trusted or cause a traceback | Receipt schema/version, module records and hash basis are validated before use; failures are typed JSON errors without writes. |
| HTML exposes out-of-scope process metadata | Selection carries explicit module IDs; embedded workspaces/process catalogs and freshness use that scope. A process selection exports only its declaration. Topology/module/process/impact regressions cover excluded owners. |
| Mermaid omits pivot and transaction boundaries | Messages mark the pivot and carry unit/call/condition notes, including compensation. Escaping uses Mermaid numeric entities, not HTML entities. |
| Cycle detection scales cubically | Iterative strongly connected components replace repeated reachability scans; deep chains, cycles, self-loops and relation-family independence are covered. |
| State labels depend on transition array order | Each transition owns its full and short label; reversing the array preserves its meaning in the SVG. |
| Changes/Impact selection restores as the wrong entity | URL state records selection kind and restores the corresponding report row/evidence. |
| Process step loses selected appearance after changing tabs | Rebuilt lists synchronize selected styling and aria-pressed with the inspector and URL. |
| Search silently misses another layer's interfaces | Zero matches offers a scoped cross-layer/filter action; activation retains the query and returns keyboard focus to it. |

Actual browser checks covered Impact and saved-diff reloads with matching evidence,
state-step selection across Overview/Processes, reversed state transitions,
module-only process catalogs, cross-layer event search in the 20-module fixture,
and 320px long/Unicode inspector and keyboard search. The page and inspector had
no horizontal overflow in the narrow fixture. Synthetic fixtures are explicitly
temporary validation data, not observed runtime traces.

Targeted inspection regression suite: 82 passed. Local cycle-detection samples
were 1.5ms / 3.6ms / 16.1ms for acyclic chains of 800 / 2,500 / 10,000 edges;
these are single-machine samples, not an application performance guarantee.
Sequence/state exports and a reserved-word/special-character sequence also parsed
with the official Mermaid 12.2.0 parser in a temporary DOM harness. Its
[sequence syntax documentation](https://mermaid.js.org/syntax/sequenceDiagram.html)
specifies the numeric entity escaping used here. Mermaid/DOM harness packages
were not added to the shipped dependencies.

Final correction validation: 5,137 full-suite tests and 1,273 architecture tests
passed, with the existing skips/expected failure retained. All quality gates,
the unified website build and the 2,371-file reviewed-source check passed. All
four final wheel/sdist pairs passed rebuild, license/layering and clean
bare/standard installation checks, including the installed cold inspection
smoke. Browser reload also preserves interactive impact preview and its mobile
details action. The local Explorer was regenerated from the final source.
