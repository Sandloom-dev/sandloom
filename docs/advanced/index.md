---
title: How Sandloom works
description: The layers of a Sandloom application, its design principles, modules and drivers, and which piece you write for each need.
type: concept
job: When I am about to build on Sandloom, I want to see how its parts fit together, so I can tell where my code goes.
---

# How Sandloom works

Your product is a set of isolated modules. They reach the framework through one public SDK,
and drivers connect the framework to your backends.

<figure class="sl-architecture" aria-label="Sandloom architecture: business modules, contracts, kernel and infrastructure drivers">
  <div class="sl-architecture__modules">
    <div class="sl-architecture__heading"><a href="../modules/">Business modules</a><span>Your application code</span></div>
    <div class="sl-architecture__peers">
      <div class="sl-architecture__module"><strong>Orders</strong><span>Order logic &amp; data</span></div>
      <div class="sl-architecture__connections">
        <a href="../modules/between/contracts/"><strong>Contracts</strong><span>Typed calls &amp; replies</span><span class="sl-architecture__arrow" aria-hidden="true">↔</span></a>
        <a href="../modules/between/events/"><strong>Events</strong><span>Asynchronous reactions</span><span class="sl-architecture__arrow sl-architecture__arrow--event" aria-hidden="true">→</span></a>
      </div>
      <div class="sl-architecture__module"><strong>Catalog</strong><span>Product logic &amp; data</span></div>
    </div>
    <div class="sl-architecture__batteries"><span>Standard modules, same boundaries</span><span>Identity · Tenancy · Billing</span></div>
  </div>
  <div class="sl-architecture__connector" aria-hidden="true"><span>use the public SDK</span></div>
  <div class="sl-architecture__kernel">
    <a class="sl-architecture__sdk" href="../reference/sdk/"><span>Public SDK</span><code>sandloom.sdk</code></a>
    <div class="sl-architecture__runtime">
      <div class="sl-architecture__heading"><strong>Kernel</strong><span>Provided by Sandloom</span></div>
      <div class="sl-architecture__guarantees"><span>Transactions</span><span>Access checks</span><span>Outbox &amp; inbox</span><span>Local / RPC routing</span></div>
      <p>Each application or service runs its own kernel.</p>
    </div>
  </div>
  <div class="sl-architecture__connector" aria-hidden="true"><span>resolve infrastructure capabilities</span></div>
  <div class="sl-architecture__drivers">
    <div class="sl-architecture__heading"><a href="../infrastructure/">Drivers</a><span>Selected by configuration</span></div>
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

## Design principles

- **A module stands alone.** It imports only the public SDK and generated contracts,
  never another module. You or a coding agent can change one by reading it and the contracts it calls.
- **The framework owns what is easy to get wrong.** It commits the transaction, authenticates
  each route unless the route declares `public(reason=...)`, and delivers events through an
  outbox. Weakening a default takes a visible line of code. Which resources a user may touch
  is yours to declare with [`require`](../modules/http/authorization.md).
- **Configuration decides where things run.** It selects which process loads which
  module and which backend each capability uses. Moving a running module's existing data
  to its own database is a separate, [explicit step](../operate/split-into-services.md#existing-data).
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
| **Created with** | `sandloom add module <name>` | [your own package](../infrastructure/write-a-driver.md) |

Modules talk to each other in two ways: [events](../modules/between/events.md) when something has
happened, and typed [contracts](../modules/between/contracts.md) when one needs an answer now.

## What do I write?

| I want to | I write |
| --- | --- |
| add a product feature | a [module](../modules/create.md) |
| react to something another module did | an [event consumer](../modules/between/add-consumer.md) |
| ask another module for data now | a call to its [contract](../modules/between/add-contract.md) |
| run a process across modules, with undo | a [saga](../modules/between/add-saga.md) |
| show a list that spans modules | a [projection](../modules/between/add-projection.md) |
| change how identity, tenancy or billing behave | a [customization](../modules/batteries/customize.md) |
| send notifications through a new service | a [driver](../infrastructure/write-a-driver.md) |
| run a module as its own service | [configuration](../operate/split-into-services.md), no module code |
