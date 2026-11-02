---
title: Overview
description: Sandloom is a Python backend framework built from isolated modules and swappable drivers. What problem it solves, what its code looks like, who it fits and where to start.
type: entry
job: When I am deciding whether to try a backend framework for a product with several business domains, I want to see what problem it solves and what its code looks like, so I can decide whether to start the quickstart.
footer_next:
  title: Quickstart
  url: getting-started/
---

# Sandloom

In a backend with several business domains, one feature soon reaches into another's
tables, and a crash between saving a change and telling the rest of the system loses the
message. Changing anything then means reading everything, for a person or a coding agent.

**Sandloom is a Python backend framework for AI-driven development:** people and coding
agents change the code together, and machine checks enforce the rules. You write your
product as isolated **modules** on FastAPI and SQLAlchemy, and work on one at a time.
**Drivers** connect them to PostgreSQL, Redis and other backends. The same code runs as a
monolith or as separate services, chosen by configuration.

<figure class="sl-architecture" aria-label="Sandloom architecture: business modules, contracts, kernel and infrastructure drivers">
  <div class="sl-architecture__modules">
    <div class="sl-architecture__heading"><a href="modules/">Business modules</a><span>Your application code</span></div>
    <div class="sl-architecture__peers">
      <div class="sl-architecture__module"><strong>Orders</strong><span>Order logic &amp; data</span></div>
      <div class="sl-architecture__connections">
        <a href="modules/between/contracts/"><strong>Contracts</strong><span>Typed calls &amp; replies</span><span class="sl-architecture__arrow" aria-hidden="true">↔</span></a>
        <a href="modules/between/events/"><strong>Events</strong><span>Asynchronous reactions</span><span class="sl-architecture__arrow sl-architecture__arrow--event" aria-hidden="true">→</span></a>
      </div>
      <div class="sl-architecture__module"><strong>Catalog</strong><span>Product logic &amp; data</span></div>
    </div>
    <div class="sl-architecture__batteries"><span>Standard modules, same boundaries</span><span>Identity · Tenancy · Billing</span></div>
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
    <div class="sl-architecture__heading"><a href="infrastructure/">Drivers</a><span>Selected by configuration</span></div>
    <p>Each driver connects the runtime to a concrete backend.</p>
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

!!! tip "Start here"
    New to Sandloom? Start with the **[quickstart](getting-started/index.md)**:
    create your own application, add a module, run the checks and start the API.
    Then follow **[Tutorial: your first feature](getting-started/tutorial.md)**
    to connect two modules with an event and a typed contract.

**Already have an application?** Pick what you need next:

<div class="grid cards" markdown>

-   :material-puzzle-outline: **[Add a module](modules/create.md)**

    Add a piece of your product: its tables, routes, events and tests.

-   :material-connection: **[Connect a backend](infrastructure/configure.md)**

    Choose a backend in the configuration, or [write a driver](infrastructure/write-a-driver.md)
    for a new one.

-   :material-tune: **[Change a standard module](modules/standard/customize.md)**

    Adjust the identity, tenancy or billing modules.

</div>

## Two modules, one event

The `tasks` module saves a task and publishes an event in the same transaction:

```python title="tasks/services.py"
--8<-- "examples/tutorial/tasklist/modules/tasks/services.py:14:19"
```

The `stats` module reacts without importing `tasks`. It declares its own view of the
event's fields:

```python title="stats/consumers.py"
--8<-- "examples/tutorial/tasklist/modules/stats/consumers.py:consumer"
```

The event commits with the task, so on the production event bus a crash right after the
commit does not lose it. A repeated delivery does not count the task twice, because the
consumer records the event's id in the same transaction as its effect. A consumer that
fails is retried, and one that keeps failing is parked for repair.
[Events and consumers](modules/between/events.md) covers the details.

## Design principles

- **A module stands alone.** It imports only the public SDK and generated contracts,
  never another module. You or a coding agent can change one by reading it and the contracts it calls.
- **The framework owns what is easy to get wrong.** It commits the transaction, authenticates
  each route unless the route declares `public(reason=...)`, and delivers events through an
  outbox. Weakening a default takes a visible line of code. Which resources a user may touch
  is yours to declare with [`require`](modules/http/authorization.md).
- **Configuration decides where things run.** It selects which process loads which
  module and which backend each capability uses. Moving a running module's existing data
  to its own database is a separate, [explicit step](operate/split-into-services.md#existing-data).
- **A machine checks the rules.** `sandloom check` and the startup checks enforce the
  boundaries and name the fix: `sandloom explain <code>`.

## Modules and drivers

A module is what your product does. A driver is what it runs on.

| | Module | Driver |
| --- | --- | --- |
| **What it is** | one slice of the business, such as orders or billing | a connection to a backend, such as Redis or SMTP |
| **Who uses it** | your product's users, through routes | modules, through the SDK; they never name the driver |
| **Imports** | `sandloom.sdk` | `sandloom.driverkit` |
| **Found through** | your `modules` package or the `sandloom.modules` entry point | the `sandloom.drivers` entry point |
| **Selected by** | `SANDLOOM_MODULES` | `INFRA` |
| **Created with** | `sandloom add module <name>` | [your own package](infrastructure/write-a-driver.md) |

Modules talk to each other in two ways: [events](modules/between/events.md) when something has
happened, and typed [contracts](modules/between/contracts.md) when one needs an answer now.

## What do I write?

| I want to | I write |
| --- | --- |
| add a product feature | a [module](modules/create.md) |
| react to something another module did | an [event consumer](modules/between/add-consumer.md) |
| ask another module for data now | a call to its [contract](modules/between/add-contract.md) |
| run a process across modules, with undo | a [saga](modules/between/add-saga.md) |
| show a list that spans modules | a [projection](modules/between/add-projection.md) |
| change how identity, tenancy or billing behave | a [customization](modules/standard/customize.md) |
| send notifications through a new service | a [driver](infrastructure/write-a-driver.md) |
| run a module as its own service | [configuration](operate/split-into-services.md), no module code |

## Who Sandloom fits

**A good fit:**

- a backend with several business domains that people and coding agents change over time;
- a product that needs tenant isolation, access control and reliable events;
- a modular monolith that may later split into services.

**Probably not:**

- a tiny endpoint or a short script, where the module structure is more work than it saves;
- an existing application whose internals cannot become isolated modules;
- a team that does not want to follow the module rules and run the checks in CI.

## Docs for coding agents

The short always-loaded map is the repository's `AGENTS.md`, and
<a href="llms.txt"><code>llms.txt</code></a> is a machine-readable index of this site. Every
page is also served as Markdown: append `index.md` to its URL.
