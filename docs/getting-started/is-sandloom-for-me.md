---
title: Is Sandloom for me?
description: Where Sandloom's module boundaries, transaction guarantees and deployment model help, and when a smaller or different framework is a better fit.
---

# Is Sandloom for me?

Sandloom is a Python backend framework for AI-driven development. It gives
people and coding agents a constrained module interface, checks that modules
stay isolated, and supplies transaction, authentication and event mechanisms.
Identity, organizations and billing are optional packages.

## A good fit

- A backend with several business domains that different people or agents
  will change over time: contracts and events keep those changes local.
- A product that needs tenant isolation, authorization, retries and durable
  events without assembling those mechanisms from scratch.
- An application starting as a modular monolith, with a realistic future
  need to deploy modules as separate services. Deployment configuration
  changes where modules run; moving existing data is an explicit migration.
- A team willing to follow the framework's module, transaction and generated
  contract rules and run the architecture gates in CI.

## Consider another approach

- A tiny endpoint or short-lived script where module structure and generation
  would add more work than they save. A small ASGI application may be enough.
- An existing application whose internals cannot be adapted to isolated
  modules: adopting Sandloom is architectural work, not a drop-in wrapper.
- A product requiring a production-ready MongoDB, Kafka or another backend
  driver that Sandloom does not currently ship. Capability contracts do not
  make an unimplemented adapter available.
- A team needing a mature third-party plugin marketplace, long-term stable
  releases or managed hosting today. Sandloom is an early public alpha.

## Infrastructure and guarantees

Production adapters currently include PostgreSQL, Redis, local files and SMTP.
The infrastructure boundary lets a compatible adapter be selected without
changing business modules; building and operating an adapter still takes work.
See [what ships today](../concepts/capabilities-and-infrastructure.md#what-ships-today).

Guarantees have conditions and boundaries: durable delivery is at least once,
a database transaction does not make an external payment atomic, and separate
services do not share one transaction. Read the
[guarantees](../concepts/guarantees.md) before choosing a deployment topology.

Try the [quickstart](index.md#quickstart), then the
[tutorial](tutorial.md). For questions and reproducible problems, see
[getting help](help.md).
