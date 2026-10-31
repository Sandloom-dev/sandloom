# Architecture explorer: UX research brief and checkpoint protocol

Date: 2026-10-30. This is primary-source research and source-level heuristic
inspection of the existing viewer, followed by proposed acceptance criteria.
**No participants were recruited; no usability study or measured human/agent
productivity improvement is claimed.** Keep subsequent scenario results distinct
from participant research and from architectural/runtime correctness checks.

## Jobs and evidence

A developer entering an unfamiliar app needs to find a module, understand why a
relationship exists, follow a process, and reach its interface/code. A developer
changing a contract needs a bounded account of potential clients and differences.
An operator needs the difference between a declared process and a recorded
attempt. An agent needs stable IDs, bounded typed queries, evidence and precise
uncertainty rather than visual interpretation. These jobs are design hypotheses
from the requested workflow, not interview findings.

W3C guidance for complex diagrams calls for a concise identification plus a
structured text equivalent conveying essential relationships. A generic SVG label
alone cannot replace this. Therefore the diagram, entity list and relationship
or step table must expose the same selected facts.
[Complex images](https://www.w3.org/WAI/tutorials/images/complex/).

APG specifies keyboard and ARIA behavior for actual tabs and disclosure controls.
Use native form controls wherever possible. If the workspace uses tabs, implement
selection, panel association and keyboard navigation; merely styling buttons as
tabs is insufficient. Expandable details must expose their expanded state.
[Tabs](https://www.w3.org/WAI/ARIA/apg/patterns/tabs/),
[Disclosure](https://www.w3.org/WAI/ARIA/apg/patterns/disclosure/).

Custom dragging needs a click/tap alternative in addition to keyboard access.
Provide zoom/fit and directional controls or equivalent selection/navigation;
ordinary browser scrolling can remain available. Aim for controls at least
24×24 CSS pixels without depending on exceptions. Diagram/table regions may
need two-dimensional scrolling, while surrounding controls and explanations
should reflow at 320 CSS pixels.
[Dragging](https://www.w3.org/WAI/WCAG22/Understanding/dragging-movements.html),
[Targets](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html),
[Reflow](https://www.w3.org/WAI/WCAG22/Understanding/reflow.html).

Search-result counts, copy success and errors need perceivable status feedback
without unnecessary focus movement. React Flow's accessibility guidance is useful
as an interaction reference, not a requirement to adopt React or its runtime.
[Status messages](https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html),
[React Flow accessibility](https://reactflow.dev/learn/advanced-use/accessibility).

## Prioritized findings in the current implementation

Source inspected: [viewer.html](../../src/sandloom/core/cli/inspection/viewer.html).

- **P0: navigation lacks topology.** Fixed alternating columns place nodes by
  array order; edge labels share a central coordinate. This gives no useful
  grouping for dense or cyclic graphs. SVG nodes do not select their inspector.
- **P0: selection is fragmented.** Module filtering changes the diagram and
  relations, but the entity list retains all non-module nodes. The relation-type
  filter does not correspondingly filter the raw-edge table. These are visible
  scope inconsistencies, not proof that graph facts themselves are incorrect.
- **P1: explanation is expensive.** Machine kinds and raw JSON dominate the
  evidence view. Inspect scrolls away from the selected relationship. A persistent
  inspector should explain owner, meaning, requiredness and provenance first.
- **P1: navigation state is disposable.** There is no stable shareable selection,
  explicit incoming/outgoing choice or bounded action connecting a fact to context.
- **P1: process understanding is absent.** The viewer has no sequence/state
  projection. Its static relations cannot supply business chronology.

These are heuristic findings from code. Contrast, screen-reader output, actual
layout collisions and task completion require the checkpoint tests below.

## Coherent next interface

Use one workspace with a persistent scope/freshness strip, search and semantic
layer controls; an Overview, Sequence and Changes navigation; canvas/table
alternatives; and a side inspector. Preserve module selection across view changes.
The same selection drives every visible representation and export scope.

Overview starts with modules. Expand into contracts/events and infrastructure as
explicit layers. Human labels should explain a typed relationship, for example
“Billing calls tenancy.membership.v1 — optional.” Keep the machine kind in details.
Show unresolved references and hidden counts rather than drawing invented nodes.

Sequence selects a declared saga or framework-owned route lifecycle. Use numbered
steps, clear participants and an equivalent step list. The inspector identifies
pivot, compensation, retry category and source. A saga definition expresses
possible paths; an observed attempt requires recorded evidence. A state view is
better for parking/recovery transitions. C4 recommends scenario-scoped dynamics;
Mermaid offers sequence and state constructs, not evidence of execution.
[C4 dynamic views](https://c4model.com/diagrams/dynamic),
[Mermaid sequences](https://mermaid.js.org/syntax/sequenceDiagram.html),
[Mermaid states](https://mermaid.js.org/syntax/stateDiagram.html).

Changes separates additions, removals and modifications, with the comparison
basis visible. Impact explains each potential client through witness relations.
A filtered projection remains a view of underlying facts, not a new architecture.
[Structurizr filtered views](https://docs.structurizr.com/dsl/cookbook/filtered-view/).

## Acceptance tasks and measurable checks

| Fixture/task | Checkable acceptance |
| --- | --- |
| Sparse, three modules: explain one optional call | Select it by canvas and table; both produce the same stable ID, endpoints, requiredness and source explanation. |
| Dense, 20 modules and high fan-out: find a named consumer | Search, focus and direction isolate its neighborhood; counts agree across views; fit restores all selected nodes without resetting the selection. |
| Cyclic graph: inspect a feedback loop | No recursion failure; typed cycle explanation appears; event feedback is not labelled an error or deadlock. |
| Disconnected graph: inspect an isolated module | It stays selectable; an explicit empty relation state replaces a blank panel; reset restores the full selected scope. |
| Long labels and Unicode | Full names remain discoverable in inspector/table; no node-label overlap; wrapping/truncation does not change semantic identity. |
| Saga with compensation and pivot | Ordered success steps match declaration data; compensation is distinguishable; local unknown metadata stays explicit; no observed-duration claim appears. |
| Route policy with refusal | The framework-owned phases and refusal point match policy fixtures; arbitrary handler calls remain unknown. |
| Architecture comparison | Added/removed/changed counts reconcile with the detail list and JSON; selecting a change opens its before/after evidence. |
| Context action | Generated command uses the selected module/item, preserves scope, and copies with feedback; no sibling implementation is included. |
| Keyboard and 320/390px viewport | All tasks have an operable control/list/table path; focus remains visible; no keyboard trap or drag-only essential action; surrounding UI has no horizontal overflow. |

Record failures by severity, fixture, action and expected/actual outcome. Inspect
1280px and narrow layouts, light/dark, 200% text zoom and keyboard-only operation.
Measure output bytes, rendering/interaction duration and node/edge counts on a
named device; set latency thresholds from that baseline, not invented claims.

## Iteration protocol

For each implementation slice: record the hypothesis, run its relevant tasks,
save screenshots and structured results, document failures, fix them and repeat
the failed cases. First checkpoint covers layout/selection; second sequence/state;
third impact/diff/context; final checkpoint repeats the cross-view and accessibility
matrix on actual app exports. A heuristic reviewer can identify risks during each
slice; a real participant study later measures comprehension, completion,
misinterpretation and recovery with representative developers. Agent fixtures
measure protocol behavior separately, without pretending to be human research.


## Checkpoint 1: source-level review of the replacement workspace

Reviewed `viewer.html`, `viewer.css`, `viewer.js`, `sequences.py`, `queries.py`
and `focus.py` on 2026-10-30 while implementation continued. **This checkpoint is
heuristic/source inspection only: this reviewer did not run a browser, screen
reader or participant session.** Passing code paths below are evidence of design
coverage, not demonstrated task completion. No P0 blocker was identified in this
bounded review; that is not a release clearance.

**P1 — process alternatives can look like one execution.** `drawProcess()` and
`sequences.mermaid()` append compensation messages after all success messages.
The text list gains an “Alternative” heading, but the SVG retains one uninterrupted
numbered sequence; Mermaid similarly lacks an alternative frame. Route refusal,
replay and conditional commit phases also rely on annotations. Use explicit
conditional/alternative framing or a success/compensation path choice in both
representations. Acceptance tasks 6–7 must verify that a successful run never
appears necessarily followed by undo or refusal.

**P1 — State currently draws another sequence.** The state branch substitutes
transition records into the same lifelines/message renderer. Render actual
states and transitions, or name this representation “Transition list” and expose
it as such. Preserve parked/recovery and conditional transition qualifications.

**P1 — impact loses its initial explanation.** Both `queries.impact()` and the
viewer seed every incident interface owner/user with an empty witness path, then
traverse module relations. This can expand through other interfaces of a seeded
module. Keep the selected interface witness for direct dependants, distinguish
them from conservative module reachability, and explain that broader propagation
assumes unknown internal coupling. A zero-hop label “Direct owner / user” cannot
explain why a specific consumer is affected. Verify browser and CLI agree.

**P1 — selection can be invisible or stale in scope.** At narrow widths the
inspector moves beneath all main content; `inspection()` neither announces the
new object nor exposes a nearby jump to details. Filtering can leave its previous
selection outside the visible model without saying so. Add an explicit selected
object/status and a reachable details action; mark out-of-view selections. Keep
focus stable instead of automatically moving it on every click. Process-step
IDs enter the URL but initial restoration resolves only graph objects/processes,
so a saved step selection cannot currently reopen its details.

Coverage against the ten acceptance tasks:

| Tasks | Source evidence and remaining check |
| --- | --- |
| 1–2: sparse/dense relationships | Shared layer model, readable labels, SVG/list selection, witnesses and SCC-based ranks replace the old fragmented view. Exercise dense graphs and inspect overlap/readability in browser; source-root/action improvements were concurrently being implemented. |
| 3–5: cycles/disconnected/long labels | Iterative SCC traversal, visible limit notices, isolated nodes, empty messages, truncation and full-name lists cover intended cases. Browser collision, Unicode sizing and fit checks remain open. |
| 6–7: saga/route | Declared-evidence labels and opaque handler limits are explicit; conditional-path and state-rendering P1 findings remain. |
| 8–9: changes/context | Counts, compatibility uncertainty, raw before/after evidence and scoped context commands exist. Validate removed-item selection, exact copied commands, witness paths and context boundary fixtures. |
| 10: keyboard/mobile | Native controls, ARIA tab keys, SVG button keyboard handlers, focus restoration, text alternatives, live counts/copy feedback and click pan controls are present. Verify focus, scrolling, status announcements, 320px reflow and contrast with actual assistive/browser checks; inspector discoverability remains P1. |


## Checkpoint 2: conditional views, evidence and navigation

This is a second bounded **source-level heuristic review**, not this reviewer's
browser, screen-reader or participant test. Reviewed the changed viewer,
sequence exporter, impact query and HTML serialization paths. Separately, the
implementation agent reported CUA checks on the Academy fixture: three modules,
two relationships, four forward saga steps, seven state transitions; no page
horizontal overflow at 1280px or 390px (the process canvas scrolls). Those reports
are implementation checks, not participant findings, and do not establish dense
layout or accessibility conformance.

Prior semantic findings are materially addressed: Forward and Compensation are
separate selections; Mermaid distinguishes the paths with `alt`/`else`; Saga
states now use a state/transition SVG. The conditional-evidence label remains
visible. Impact JSON preserves the original seed and its interface witnesses,
distinguishes direct from transitive scope, and explicitly qualifies propagation
through other interfaces as an overapproximation. Relationship inspectors expose
interface witnesses, methods, requiredness, source-root evidence and context
actions. These changes cover the intended explanation before raw declaration
JSON without claiming a runtime trace.

**Remaining P1 — mobile details action is scoped to the wrong tab.** The new
“View selected details” button is inside Overview. A process-step selection makes
that button logically available but its ancestor tabpanel remains hidden. At
narrow widths the inspector is below the entire process workspace, so task 6/10
still lacks the intended nearby jump. Put the shared action outside tabpanels or
provide a process-local equivalent, and verify both click and keyboard focus.

**Remaining P1 — selected step identity changes meaning across representations.**
`process#step-0` denotes forward step zero, compensation zero or state transition
zero depending on `process-view`. Changing representation preserves the old
inspector/selection ID, but reloading the resulting URL resolves that ID from the
new representation. A shared link can therefore reopen a different fact. Encode
the representation in step identity, or explicitly clear/re-resolve selection
on a representation change. Verify forward→compensation→reload and
forward→state→reload, plus selecting a different process.

Escaping inspection found label construction through `textContent`/SVG text,
script-embedded JSON escaping of `<`, `&` and Unicode line separators, shell
quoting for copied arguments, and Mermaid label escaping. No injection defect was
identified in these reviewed paths. This is not a penetration test or a claim
that arbitrary edited artifacts are trustworthy. Browser checks still needed:
compensation meaning, both mobile jumps, 320px reflow, long-label/dense/cyclic
fixtures, selected-ID round trips, actual focus and announced statuses.


## Final bounded checkpoint: selected facts and navigation

Reinspected the final shared selection action, process identity/restoration,
conditional renderers and impact/changes presentation in source. The two remaining
checkpoint-2 P1 findings are addressed: the details jump is outside tabpanels
and fixed near the bottom at narrow widths; step IDs include their representation
(`#sequence-step-N`, `#compensation-step-N`, `#state-step-N`), restoration uses
that identity, and changing process/representation clears the prior step selection.
**No additional concrete P1 was identified in this bounded semantic-display and
navigation review.** This conclusion does not extend to untested accessibility
or user-comprehension claims.

The implementation agent reported these additional CUA scenario results:

- At 390px, selecting the refund compensation step and using the shared jump
  focuses the inspector heading; reload preserves refund and its compensation ID.
- The dense synthetic fixture shows 20 nodes and 80 relationships, matching lists,
  with no overlapping node rectangles. Searching `m07` yields seven nodes, eight
  links and one match; interface search `m07.changed` yields five nodes, four links
  and two matches. These results do not prove that every dense edge is readable.
- At 320px, a synthetic Unicode isolated module stays selectable with its full
  name in the inspector, one node/zero links, and no page horizontal overflow.
  An unknown search produces explicit empty and out-of-view feedback.
- Enter preserves SVG focus after selection, and ArrowRight selects and focuses
  the Processes tab.

These are reported scripted/browser scenario checks, not participant research;
this reviewer performed source inspection only. Screen-reader output and 200%
text zoom remain untested. Reported full-gate and packaging checks establish
separate implementation evidence and are not substitutes for those UX checks.

A small nonblocking P2 remains: exported Impact/Changes rows summarize hops and
the test command rather than displaying the seed witnesses now retained in JSON.
The meaning and conservative limits are explicit, so this is explanation depth,
not a newly identified semantic error. Keep broader enhancement and real
participant validation as subsequent work rather than expanding this checkpoint.

Implementation follow-up: the final P2 explanation gap was closed as well. HTML
impact rows open an inspector with seed module, direct interface witnesses,
transitive path, scope and a copyable suggested test command. CUA confirmed the
optional tenancy contract's billing user and provider appear as direct matches,
and the inspector shows its actual `requires_contract` witness. Zero-change HTML
diff reported 0/0/0 and an explicit empty state. These are browser scenario checks,
not participant-study findings.


## CLI entry-point checkpoint (2026-10-30)

The human entry path now has one authoring command, `sandloom explore`: generate
in a child process, save an ignored offline HTML file, print its local URL and
open the browser. Failure to generate stops before opening an old artifact;
`--no-generate` and `--no-open` make the two side effects explicit. Headless
browser failure preserves a usable file and explains the remaining manual step.

Cold `graph` uses stdout TTY for a readable tree, and JSON for pipes/redirects;
explicit `--format` wins, including for error output. This is stream detection,
not agent detection. Agent skill examples explicitly request JSON so a runner's
PTY allocation cannot silently change the expected protocol. Positional module,
impact and Git-diff forms remove the separate view/item pair from common tasks;
the existing flag forms remain available for saved-file comparisons and scripts.

This follows the primary [CLI output guidance](https://clig.dev/#output):
human-readable terminal output with explicit structured machine output. Git
revision resolution follows [rev-parse's untrusted-name guidance](https://git-scm.com/docs/git-rev-parse)
with `--end-of-options` and a commit type suffix. Only the bounded committed
inspection blob is read; lazy fetching and terminal authentication prompts are
disabled. Browser fallback follows Python's documented
[webbrowser return value](https://docs.python.org/3/library/webbrowser.html).

Scenario checks used a real TTY on Academy, pipe/format regression cases,
root/nested Git repositories, missing/unsupported/oversized/sensitive baselines,
CRLF JSON, failed generation and mocked browser success/unavailability. A real
`explore --no-open` generated Academy's verified HTML; `graph diff HEAD` returned
0 added/removed/changed without checking out historical code. These are CLI
scenario checks and source-guided design, not a participant usability study.


## Map rather than source delivery: user-feedback checkpoint

The owner's concrete identity example exposed a mismatch: the tool duplicated
implementation files the agent can already read and buried navigation under SDK
reference material. The revised interface supplies outside-facing facts and file
locations; the agent chooses which implementations to inspect. This is direct
user feedback, not a new participant study.

Identity's current map was reconciled against the generated index: two provided
contracts, 15 published and two incoming event channels, 48 route-method entries,
nine tables and no sagas. File purposes use first-line module docstrings;
symbol/line metadata is parsed without execution. Grouping retains every route,
while exact paths remain in JSON. Focus was checked separately to avoid reporting
irrelevant zero-count sections or enumerating every excluded file.

Same-workspace default output decreased from 131,058 to 7,908 UTF-8 bytes; contact
interface focus is 1,301 bytes. This measures byte size, not comprehension or task
success. Canary regressions verify that even full/focused formats do not deliver
implementation, instruction or generated-DTO bodies. Paid agent DX stays deferred.


## Viewer correctness review: obstacle routing and readable scope

The Helpdesk review identified a semantic failure: a long arrow crossed an
unrelated module rectangle and implied a fictitious intermediary. The actual
module projection remains exactly tickets → billing, tickets → tenancy and
billing → tenancy; identity has no declared links in this composition.
A test of the original SVG renderer reproduced `tickets → billing crosses
unrelated identity` before any repair.

The repair separates disconnected components, keeps each rank in a vertical
column and routes orthogonal paths around node rectangles. Endpoint ports are
spread and shared collinear segments carry a cost. No unvalidated straight
shortcut is drawn if routing fails. This follows the separation of node layout
and obstacle-aware routing described by [yWorks](https://www.yworks.com/pages/drawing-orthogonal-diagrams.html);
the implementation is local, with no new runtime library or external service.
A Node test exercises the actual SVG-rendering functions and checks every path
against non-endpoint rectangles on Helpdesk, all six stored compositions and
seeded cyclic/self-loop/parallel/disconnected fixtures.

Large graphs start at readable scale; scrolling is contained in the diagram.
Fit-all remains an explicit action, with a warning when labels need zoom.
The surrounding controls/text reflow on mobile. [W3C Reflow guidance](https://www.w3.org/WAI/WCAG21/Understanding/reflow)
distinguishes diagrams requiring two-dimensional layout from the surrounding UI;
this is design guidance, not an accessibility-conformance claim.

Secondary filters move behind disclosure; module selection stays on the first
row. Interface/infrastructure module filters select the owner rather than
expanding every neighbor's interfaces; direction/distance controls apply to
module relationships. Module purposes appear in blocks, entity lists and the
inspector. Route counts open the recorded route list and declared lifecycle.
Full module metadata is carried only for the HTML's selected export scope.
Impact names the selected interface and its witnesses. Diff names added/removed
methods and concrete changed declaration fields, independently of whether the
change is backward-compatible. Route lifecycles use wrapped vertical steps;
the existing saga forward/compensation/state representations remain available.

Browser scenarios at 1440×900 and 375×812 confirm three rendered Helpdesk edges,
an isolated identity, a scoped Tickets interface view (6 entities/5 links),
visible method-level diff and impact witnesses, and an unclipped route diagram.
Canvas top: about 328px desktop and 419px mobile. No page-level horizontal
overflow was observed. These are scenario checks, not a participant usability study.
