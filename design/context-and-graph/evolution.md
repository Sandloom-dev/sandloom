# Architecture explorer evolution

Implemented on `codex/context-graph-design`, 2026-10-30; unreleased after 0.7.0a3.
The current behavior is specified in [spec.md](spec.md). This extension follows the
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
wire files and package versions. Cold inspection still has no app,
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


## Human CLI front door (2026-10-30)

Added `sandloom explore [MODULE]` (explicit generation + offline HTML + browser),
`graph [MODULE]`, `graph impact ITEM` and `graph diff REV`. Graph defaults to text
on stdout TTY and JSON in pipes/redirects or file-writing mode; explicit formats
win. Named flags select process views, disambiguate module names and compare
saved-index files. Revision diff reads only this project's
committed index, resolves an immutable commit and reports that basis, without
checkout/fetch or old-code execution. Missing baseline data is an error.

The source specification, CLI guide/reference and four agent skill copies were
updated together. Paid agent DX remains deferred. Targeted inspection suite:
112 passing cases; lint, formatting, types and skill validation pass. Full-gate,
distribution and Linux CI results are recorded in the pull request.

Final front-door validation: 5,167 full-suite tests, 1,279 architecture tests and
all quality gates passed; existing skips/expected failure retained. Four
wheel/sdist pairs passed rebuild, license/layering and clean-install checks,
including cold positional graph/impact queries. A fresh wheel-installed app
with identity, tenancy, billing and notes generated a verified offline Explorer,
returned verified graph JSON through a pipe and received both updated skill
mirrors. The unified website, explicit architecture check, skill validator and
2,374-file reviewed-source check also passed.


## Compact module-map revision (2026-10-30)

Context now returns a structural navigation map, superseding the earlier
source-bundle prototype. Implementation bodies, copied source/instruction files
and the bulk SDK reference are absent from default/full/focused Markdown and
JSON. The source-export `--file` flag was removed before release. The agent reads
applicable rules and implementation/interface files itself from the map's paths.

The map includes provided/used contract signatures and composition-local peers,
produced/consumed events, grouped routes, tables/sagas, infrastructure requirements,
owned file sizes/purpose/public symbols/line numbers and grouped inventories.
Focus adds declared DTO field types or event schemas and counts excluded metadata
without dumping irrelevant filenames. Exact defaults/validation remain in linked
interface files. Temporarily invalid implementation syntax retains navigation with
an explicit metadata warning; no application import or execution is introduced.

Measured on the same current identity composition and default 131072-byte budget:
the earlier source bundle was 131,058 UTF-8 bytes; the module map is 7,908 bytes
(94% smaller), and contact-contract focus is 1,301 bytes. This measures serialized
bytes, not LLM tokens or productivity. Real contract-wire descriptors exercise
type rendering; canary tests cover implementation/rule/DTO body absence across
Markdown/JSON, compact/full detail and interface focus. Current specification,
public guide/reference and the three shipped skill copies were updated together.

Compact-map validation completed: 134 focused inspection cases passed; full
quality gate passed with 5,189 full-suite tests and 1,283 architecture tests,
retaining existing skips/expected failure. All four wheel/sdist pairs passed
rebuild, license/layering and clean installs. A fresh wheel-installed all-battery
application generated verified compact/full maps without an implementation body
canary and received both revised agent skill mirrors. Website build, explicit
architecture checker, skill validator and 2,376-file source-export check passed.
Linux/GitHub results for the committed revision are linked in the pull request.


## Compact-map review corrections

The follow-up review reproduced three defects: installed providers lost method
signatures, source metadata inherited whole-file export filters, and route focus
retained the entire module. Generation now records describe documents from every
registered provider, without cold-reader imports. Endpoint locations come from
registered callbacks; focused routes show exactly one endpoint, while saga focus
uses declared contracts and local callbacks. No handler call graph is inferred.

Source containment and stable hashing are separate from export content policy.
Test directories are counted without decoding/parsing bodies; all source bytes
still participate in freshness. Oversized/non-UTF-8 implementation files retain
navigation with per-file warnings. Extracted summaries/symbols remain secret
checked, and index/history/output, symlink and overwrite guards remain enforced.

Maps omit private symbols and empty headings; routes include auth/handler,
tables show tenant scope and sagas have readable version labels. Terminal
relationships are grouped/sorted semantically. Removed the unused SDK catalog,
source-body helper, old prototype header, obsolete design drafts/skill copy and
two empty identity files. Budgets, evidence, ignore policy and atomic exports
remain useful supported behaviors, rather than prototype debris.

Validation of these corrections: 143 focused cases; 5,198 full-suite tests,
1,283 architecture cases and every static gate; all 14 owned module maps across
six compositions and six file edge cases. All six generated indexes pass check.
Installed wheels expose Helpdesk's membership/entitlement/facts methods (6/3/2)
and create a verified all-battery application. Four wheel/sdist pairs, rebuilds,
lean/standard installs, site build, skill validation and the 2,368-file public
source audit passed. Browser checks cover impact selection, restored state,
copy feedback and readable owner labels. Paid agent DX remains deferred.

Linux `backend-static` and strict docs CI passed on `f522b3e` in 86 seconds,
including installed-wheel RPC transport and vulnerability checks. The final
packaged viewer also separates owner labels from impact evidence on screen.


## Facts-only map refinement

Default Markdown now contains scope-specific facts without a Next checklist,
source-opening preamble or generic focus caveats. Workflow and inference limits
remain in the shipped module-context skill. Route rows retain handlers/public
markers without duplicate group headings/default auth tags. Tables are grouped
by scope; inert import/re-export initializers are excluded and counted, while
initializer logic remains navigable. File sizes remain available in JSON.
Singleton event channels and process labels render literally/readably. Focused
unresolved facts retain only the selected interfaces and use plain text.

DTO method/field names and public references resolve against local shared wire
syntax, including explicit `dto_copy` origins and renamed class definitions.
The map points to the actual local class/file/line rather than a peer's internal
schema module. Missing local definitions stay unavailable, without guessed paths.
This lookup remains cold: no application or wire-file execution/imports.

Validation: 148 focused cases; 5,203 full-suite tests and 1,283 architecture
cases with every static gate passing; six fresh generated-file checks; 14 module
maps, 19 contract-focus maps and six filesystem edge cases. Every rendered DTO
locator in the matrix matches a local class definition. Four wheel/sdist pairs,
lean/standard installs, an installed Helpdesk DTO focus, all-battery scaffolding,
site/skill and the 2,368-file source audit passed. On the same compositions,
Markdown identity shrank 9,942 → 8,357 bytes, enrollment 2,118 → 1,585 and
membership focus 1,704 → 1,167 (serialized UTF-8 bytes, not LLM tokens).


## Unified CLI and RPC self-test timing correction

The current positional CLI supersedes historical flag examples in this record:
`graph [MODULE]`, `graph modules`, `graph impact ITEM`, `graph diff REV_OR_FILE`,
`graph sequence [ID]`, `graph state ID`. Reserved names use `module:NAME`.
Context exposes focus/format/root/verification only. Graph retains JSON provenance
and its JSON output budget. Both stream to stdout; shell redirection saves files.
Explorer writes its fixed cache HTML directly via the internal atomic writer.
Removed destination/force/recognition/protected-file branches and context size
accounting. Source/index/wire/history validation and containment remain intact.
The three skill mirrors, current guides/spec/reference and viewer commands match.

GitHub run 41130838525 (1926d80) failed only the aiohttp loopback compatibility
probe; run 41135895312 (034f9b9) later passed every required job. The failing call
hit a nested 2-second connect timeout inside a 5-second self-test. A controlled
2.1-second first connection reproduced the same ConnectTimeout; removing that
nested request timeout made the same real loopback scenario pass. The outer
self-test deadline and fallback remain; a blocked probe still times out. This is
a bounded startup probe correction, not retries or changed application RPC limits.

Targeted inspection/RPC coverage: 165 passing cases, including all five reserved
module names, import-path Explorer scope, Git/file baselines, rejected removed
flags, a complete context larger than the former limit, preserved graph budget,
managed-output symlink refusal, delayed loopback connect and overall expiry.
All seven CLI forms and Mermaid outputs passed against Academy. Four wheel/sdist
pairs, lean/standard cold tools and the strict site build passed. Browser scenario
checks cover process selection and copy feedback; clipboard bridge inspection did
not expose the browser clipboard content, so no clipboard-value claim is made.


## Project overview and complete module maps (2026-10-30)

This section supersedes the historical focus/discovery/TTY behavior above.
`graph` answers a project question with module purposes and compact facts.
`context MODULE` answers a module question with its complete navigation map.
No new commands were added. Removed `context --focus`, its DTO/location/scoping
implementation, outside-focus accounting and the `graph modules` action. The
word `modules` is an ordinary module name; action names still accept `module:NAME`.
Graph defaults to text even through a pipe; JSON requires `--format json`.

Explicit generation records each package's first docstring line as module
`summary`, including installed batteries without checkout-owned source. All six
indexes were regenerated. Cold reads still reject app/settings/YAML imports and
network access. The project overview names called/listened-to modules and leaves
infrastructure to detailed views. JSON retains stable IDs and summaries.
Default module maps show `[money]` on contract commands, retain operation keys in
signatures and direct the agent to generated interface files for exact DTOs.
Viewer copy actions now request the complete owner module map. Other Explorer,
impact/diff/sequence/state behavior and graph JSON detail/budgets remain unchanged.

### Size acceptance

| Composition | Modules | UTF-8 bytes | Bytes/module |
| --- | ---: | ---: | ---: |
| root | 3 | 552 | 184.0 |
| academy | 3 | 503 | 167.7 |
| helpdesk | 4 | 663 | 165.8 |
| linkly | 1 | 126 | 126.0 |
| projecthub | 4 | 640 | 160.0 |
| tutorial | 2 | 241 | 120.5 |

Measured complete default stdout, including header and freshness; no byte truncation.

### Verification of the simplified maps

- `sandloom test --full --ci`: every gate passed. Full suite: 5,203 passed,
  7 skipped, 1 expected failure (164.37s pytest / 165.7s runner step).
  Architecture: 1,283 passed; typing: 3 passed. Existing skips/expected failure
  and 14 test warnings remain visible. No tests were disabled: focus-only cases
  were removed with that feature and replacement behavior has regression cases.
- Targeted inspection suite: 146 passed. A subprocess with blocked app/settings/SDK
  imports and network pipes default graph output through `/bin/cat` as text.
  Removed focus and nonexistent `modules` exit 2; a real `modules` module works.
- Installed-only package metadata retains its first docstring line in the index
  and graph. Helpdesk shows billing, identity and tenancy purposes with source
  unavailable; Academy's charge/refund/topup signatures carry `[money]`.
- Root and five examples pass `generate --check`. The strict site build, explicit
  architecture checker, skill validation/three mirror comparison and reviewed
  public-source check (2,367 files) pass.
- All four wheel/sdist pairs pass reproducible runtime content, license metadata,
  import layering and clean lean/standard installation. Installed cold inspection
  covers text/JSON graphs, module maps and offline HTML with imports/network denied;
  installed RPC transport/fallback smoke also passes.

Paid agent DX, PyPI release and website deployment remain deferred.
