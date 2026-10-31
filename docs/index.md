---
title: Documentation
description: Learn Sandloom, the Python backend framework for AI-driven development. Start with your first module, then explore tenancy, billing, events and deployment as services.
---

# Sandloom Framework

**Python backend framework for AI-driven development.** One codebase, one image — deploy
it as a monolith today and split it into microservices later by changing the
deployment configuration. Business modules keep the same code.

!!! note "Code and topology by configuration — data by migration"
    The split is a configuration change for the code and the topology (which
    process loads which module, in-process vs RPC contracts, which database a
    module uses). It does **not** move live data: extracting a module from a
    running monolith also means moving its tables (with its migration version
    row and pending outbox events) into the new service's database — an
    offline maintenance-window step done by `sandloom migrate extract-module`; see
    [Extracting a module](guides/deployment.md#extracting-a-module-into-its-own-service). Which
    infrastructure backends are production-ready vs contract-only is listed in
    [What ships today](concepts/capabilities-and-infrastructure.md#what-ships-today).

Sandloom provides a structured backend development path for people and AI coding agents: every
business module is **atomic and isolated**, so a developer (or an agent) can
build or modify one module without understanding — or breaking — the rest of the
system. AST-based architecture tests enforce that isolation in CI.

!!! tip "Start here"
    New to Sandloom? Start with the **[quickstart](getting-started/index.md)**:
    create your own application, add a module, run the gates and start the API.
    Then follow **[Tutorial: your first feature](getting-started/tutorial.md)**
    to connect two modules with an event and a typed contract.

<div class="grid cards" markdown>

-   :material-school: **[Tutorials](getting-started/index.md)**

    Learn by doing: quickstart, then two modules, an event, a contract call
    and tests. Installation and building a product on the wheel.

-   :material-book-open-variant: **[How-to guides](guides/index.md)**

    Do one task: add a module, an event, a contract, a consumer, a saga or a
    projection; write a migration; make a write idempotent; deploy; operate.

-   :material-api: **[Reference](reference/index.md)**

    Look it up: the `sandloom.sdk` Python API, the error catalogue, testing, the
    CLI, configuration, the security model, the bundled modules.

-   :material-cube-outline: **[Concepts](concepts/index.md)**

    Understand why: modules and levels, units and `db()`, route policy and
    sessions, events, contracts, guarantees, and the decision records.

</div>

Use [module maps](guides/module-context.md) to inspect a module before editing;
[the architecture explorer](guides/architecture-graph.md) shows declared connections,
processes and interface impact.

For agents, the short always-loaded map is the repository's `AGENTS.md`, and a
machine-readable index of this site is <a href="llms.txt"><code>llms.txt</code></a>. Every page is
also served as Markdown: append `index.md` to its HTML directory URL, e.g.
`concepts/route-policy/index.md`. On any `error[SANDLOOM-...]`, run
`sandloom explain <code>` or read the [error catalogue](reference/error-codes.md).

Before adopting Sandloom, read [Is Sandloom for me?](getting-started/is-sandloom-for-me.md).
For signup, verification and tokens, follow [authentication and email](getting-started/authentication.md).
For questions or an upgrade, see [getting help](getting-started/help.md) and
[upgrade an application](guides/upgrading.md).

## Why Sandloom

- **Monolith ↔ microservices via config.** `SANDLOOM_MODULES` decides which modules a
  process loads. The same image runs everything in one process or one service
  per module behind a gateway.
- **Atomic modules.** Business modules import only `sandloom.sdk`, generated
  contract stubs and DTOs, never each other. They talk through typed,
  versioned events (`billing.credits.spent.v1`) and typed contracts
  (`tenancy.membership.v1`).
- **Batteries included.** [Identity](modules/identity.md) (JWT auth, RBAC, 2FA,
  SSO), [Tenancy](modules/tenancy.md) (organizations, roles, invitations), and
  [Billing](modules/billing.md) (subscriptions, credit ledger, Stripe) ship as
  ready modules.
- **Infrastructure is pluggable.** Databases, caches, object stores, task
  queues, and the event bus are reached through capability contracts; the
  concrete backend is selected purely by configuration.

## Architecture at a glance

Your business logic lives in modules. Sandloom runs them, connects them and
provides their infrastructure through a public SDK.

<figure class="sl-architecture" aria-label="Sandloom architecture: business modules, contracts, kernel and infrastructure drivers">
  <div class="sl-architecture__modules">
    <div class="sl-architecture__heading"><a href="concepts/modules/">Business modules</a><span>Your application code</span></div>
    <div class="sl-architecture__peers">
      <div class="sl-architecture__module"><strong>Orders</strong><span>Order logic &amp; data</span></div>
      <div class="sl-architecture__connections">
        <a href="concepts/contracts/"><strong>Contracts</strong><span>Typed calls &amp; replies</span><span class="sl-architecture__arrow" aria-hidden="true">↔</span></a>
        <a href="concepts/events/"><strong>Events</strong><span>Asynchronous reactions</span><span class="sl-architecture__arrow sl-architecture__arrow--event" aria-hidden="true">→</span></a>
      </div>
      <div class="sl-architecture__module"><strong>Catalog</strong><span>Product logic &amp; data</span></div>
    </div>
    <div class="sl-architecture__batteries"><span>Optional modules, same boundaries</span><span>Identity · Tenancy · Billing</span></div>
  </div>
  <div class="sl-architecture__connector" aria-hidden="true"><span>use the public SDK</span></div>
  <div class="sl-architecture__kernel">
    <a class="sl-architecture__sdk" href="reference/sdk/"><span>Public SDK</span><code>sandloom.sdk</code></a>
    <div class="sl-architecture__runtime">
      <div class="sl-architecture__heading"><strong>Kernel</strong><span>Provided by Sandloom</span></div>
      <div class="sl-architecture__guarantees"><span>Transactions</span><span>Access checks</span><span>Outbox &amp; inbox</span><span>Local / RPC routing</span></div>
      <p>Each application or service runs its own kernel.</p>
    </div>
  </div>
  <div class="sl-architecture__connector" aria-hidden="true"><span>resolve infrastructure capabilities</span></div>
  <div class="sl-architecture__drivers">
    <div class="sl-architecture__heading"><a href="concepts/capabilities-and-infrastructure/">Drivers</a><span>Selected by configuration</span></div>
    <p>Adapters connect the runtime to concrete backends.</p>
    <div class="sl-architecture__backends">
      <div><span>Database · sqlalchemy</span><strong>PostgreSQL</strong></div>
      <div><span>Cache</span><strong>Redis</strong></div>
      <div><span>Events · streams</span><strong>Redis Streams</strong></div>
      <div><span>Files · local</span><strong>Disk / volume</strong></div>
      <div><span>Tasks · celery</span><strong>Celery</strong></div>
      <div><span>Email</span><strong>SMTP</strong></div>
    </div>
  </div>
  <figcaption>One modular application. The same module code in a monolith or separate services.</figcaption>
</figure>

**Contracts belong to modules.** A provider exposes a typed, versioned API;
other modules call its generated stub. The kernel routes the call locally or
over RPC. [Events](concepts/events.md) are the asynchronous alternative.
Identity, tenancy and billing are optional business modules, not kernel components.

**Drivers implement infrastructure capabilities.** Modules use the SDK; the
runtime resolves the configured backend. A replacement driver must provide the
capabilities and features the module needs. The diagram shows the current
production backends; see [what ships today](concepts/capabilities-and-infrastructure.md#what-ships-today)
for their support and deployment requirements.

## Quick start

Follow the [quickstart](getting-started/index.md), then the
[tutorial](getting-started/tutorial.md). To work on the framework
itself, [Installation](getting-started/installation.md) sets up the checkout.
