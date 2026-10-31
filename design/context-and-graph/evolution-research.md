# Architecture tooling evolution: sequences and actionable navigation

Research date: 2026-10-30. **Proposals, not implemented features.** This note
separates facts from official sources, current Sandloom evidence and engineering
recommendations. No runtime behavior or comparative DX improvement is claimed.

## What the source material establishes

A sequence diagram conveys interactions **and ordering**. Mermaid supports
participants, activation, synchronous-looking and explicit asynchronous arrows,
notes, `alt`/`opt`, `loop` and `par` fragments. These are expressive display
constructs, not evidence that a Python program follows a depicted path. Merely
rendering a graph edge with an async arrow does not prove actual concurrency.
[Mermaid sequence reference](https://mermaid.js.org/syntax/sequenceDiagram.html).

C4 dynamic diagrams target a particular feature or scenario rather than an
entire application. They can use numbered communication or sequence layouts;
the C4 guidance recommends them selectively for interactions worth explaining.
Structurizr's dynamic views instantiate relationships from a static model and
add scenario-specific ordering/descriptions. This supports reusing Sandloom's
stable entity IDs while keeping scenario ordering as separate information.
[C4 dynamic diagrams](https://c4model.com/diagrams/dynamic),
[Structurizr dynamic-view language](https://docs.structurizr.com/dsl/language#dynamic-view).

Structurizr separately documents parallel dynamic sequences and filtered views
built on another view. Consequently, showing one convenient linear order or
filtering away unrelated nodes is a presentation decision that must not silently
change the semantics of the underlying model.
[Parallel sequences](https://docs.structurizr.com/dsl/cookbook/dynamic-view-parallel/),
[Filtered views](https://docs.structurizr.com/dsl/cookbook/filtered-view/).

For observed behavior, OpenTelemetry spans contain timing, parent/context,
events, links and status; exporters can target local stdout or an external
backend. Its tracing API permits links to spans in the same or another trace.
This provides an existing interchange model rather than requiring a proprietary
Sandloom trace store. Sampling and missing instrumentation still limit what was
observed. [OTel traces](https://opentelemetry.io/docs/concepts/signals/traces/),
[OTel span links](https://opentelemetry.io/docs/specs/otel/trace/api/#specifying-links).

The messaging conventions distinguish create/send/receive/process/settle and
require propagated message-creation context to correlate producer and consumer
work. Links are the default correlation mechanism because batches and ambient
contexts cannot always be represented by one parent. A diagram must preserve
fan-out and causal links rather than inventing one global transaction or
flattening every asynchronous operation into a request's child span. Messaging
conventions currently contain development-status details, so any implementation
must pin and document its chosen convention version.
[OTel messaging spans](https://opentelemetry.io/docs/specs/semconv/messaging/messaging-spans/).

React Flow documents multiple layout approaches, including Dagre and ELK, with
different capabilities and tradeoffs; choosing a graph editor does not by itself
solve layout. Sandloom can evaluate a packaged layout engine without adopting a
new backend or React application solely for an export.
[React Flow layout overview](https://reactflow.dev/learn/layouting/layouting).

## Fit to the current implementation

The current [graph exporter](../../src/sandloom/core/cli/inspection/graph.py)
projects declared requires/provides and producer/consumer relationships. It
contains no route-body control-flow execution history. Therefore `A` consuming
an event and requiring `B` does **not** establish that this event invokes `B`,
which method it invokes, the condition, or the order.

Sandloom has richer, more reliable starting points: the
[request lifecycle](../../docs/concepts/lifecycle.md) defines framework-owned
commit/re-check/idempotency stages, and the
[saga registry](../../src/sandloom/core/sagas/registry.py) retains definitions
whose `describe()` data can expose ordered steps and compensation metadata.
Today's inspection collector stores only the manifest's saga name/version;
sequence support needs an explicit generation-time extension. In
[Saga.describe()](../../src/sandloom/core/descriptors/sagas.py), local step calls
and compensations serialize as null even when local callables exist: the collector
should add presence/symbol metadata without invoking those functions. A declared saga
path remains a model of possible execution, not an instance's observed retries.

The [tracing helper](../../src/sandloom/core/observability/tracing.py) provides
optional setup and generic spans. The saga runtime also creates step spans.
Neither fact establishes end-to-end correlation across HTTP, contracts, outbox,
broker deliveries and consumer attempts. That needs a measured instrumentation
and context-propagation audit before offering an “actual request sequence.”

## Recommended order

1. **Improve graph navigation first.** Replace the fixed placement with a tested
   layout; add pan/zoom/fit, incoming/outgoing selection, stable deep links and a
   synchronized inspector with direct source/documentation links. Offer module overview, contract/event detail and
   capability detail as projections of the same facts. Keep keyboard/table access
   and the offline asset/security contract. Test dense, cyclic and disconnected
   fixtures rather than judging one three-module screenshot.

2. **Add declared saga sequences and state views.** Start with registered ordered
   steps, call targets, compensation edges, pivot and recovery categories. Export
   a versioned sequence model plus Mermaid/HTML. Label alternatives as possible;
   mark unsupported dynamic data explicitly. Do not execute `args` callbacks in
   the cold reader. Show state transitions separately where they explain parking,
   retry and compensation more clearly than another sequence lane.

3. **Add framework lifecycle explanations.** Selecting an authenticated write
   route could show the framework's verified policy phases: admission, unit,
   commit-time re-check and commit. Separate framework-guaranteed stages from the
   opaque business-handler body. Provide educational success/refusal variants;
   never fabricate the handler's internal RPC or event order.

4. **Add change-impact and semantic diff.** Compare saved inspection snapshots by
   stable ID; report changed contracts/events, directly affected declared clients,
   transitive reachability and uncertainty. Reuse existing compatibility engines
   for breaking-change conclusions. A reachable module is potentially affected,
   not proven to execute the changed method. Emit Markdown/JSON suitable for PR
   review without automatically posting comments or claiming test completeness.

5. **Make graph actions useful to agents.** Commands such as proposed `impact`,
   `path` and `sequence` queries should return bounded typed results, witness IDs,
   evidence mode and suggested scoped context expansion. Add context slicing
   for a selected contract/event/change so an agent can request only the relevant
   interfaces and owned files instead of reloading its entire module bundle. Distinguish possible
   paths from observed calls; return “no declared path” rather than “cannot happen.”
   Reuse current discovery, byte budgets, freshness and structured-error contracts.

6. **Then add observed scenario sequences.** Initially import an explicit local
   test trace/OTLP file instead of demanding a hosted observability service.
   Preserve parent/link relationships, attempts, durations, errors, redaction and
   coverage. Default to metadata without request bodies, credentials or PII.
   Distinguish recorded scenario input from production evidence, and show gaps.

Reuse existing declarations by default; no mandatory scenario annotations are
needed for those first views. Author-curated business scenarios, if later offered,
must retain their separate provenance.

The most useful next increment is navigation plus **declared saga sequences**.
Observed route-to-consumer sequences are valuable, but require additional runtime
instrumentation; outbox rows and the current static graph alone cannot supply
that promise.

## Concrete next increment for Sandloom

Recommended MVP: improve the existing viewer's layout/semantic zoom and inspector,
then add declared saga sequences and a saga state view using one versioned
sequence representation. Route-policy lifecycle explanations can reuse that
renderer next. Keep machine-readable sequence data separate from Mermaid/HTML;
retain stable IDs, evidence/freshness, omissions and explicit unknown ordering.

Current-code seams confirmed in this audit:

- `core.facade._registries.CoreFacade.sagas` exposes registered definitions with
  owners; `core.sagas.registry.SagaRegistry.describe()` adds module/database.
  Collect their safe data during explicit generation. Read-only exports continue
  to consume committed JSON without invoking `args`, local steps or compensation.
- `core.descriptors.sagas.Saga.describe()` has step order, kind, command targets,
  compensation targets and deadlines. Local function presence/source symbols
  need separate metadata, since its serialized local calls are null.
- `core.cli.inspection.collect.collect()` currently preserves only each
  `SagaEntry` name/version but already records route policies. Add richer saga
  metadata through the existing collector rather than a second discovery engine.
- `core.eventing.envelope.EventEnvelope` carries an event ID, `trace_id` and
  timestamp. `core.rpc.outbound._rpc_headers()` propagates a request ID. Audited
  code does not establish W3C parent-context propagation or messaging span links;
  saga step tracing currently annotates the starting trace ID as an attribute.
  These identifiers help correlation but do not prove an end-to-end causal chain.

Change-impact and semantic diff should follow. Comparing index hashes alone
only establishes that a wire artifact changed; field/method breaking-change
explanations need the matching old/new wire or contract snapshot documents and
existing compatibility engines. Likewise, a suggested module test command is a
starting point, never a claim that transitive reachability identifies every
necessary test or authorizes skipping the full gates.

Deployment overlays and observed test traces are later increments. Deployment
maps should expose logical module/service/database/driver labels with secrets
excluded, and distinguish planned configuration from observed deployment. Watch
mode may refresh source status; it must keep generation an explicit authoring
action. There is no requirement to add scenario annotations to ordinary modules.

This research changes no shipped CLI, runtime, skill or public documentation.
