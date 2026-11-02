---
title: Overview
description: Sandloom is a Python backend framework designed for AI-driven development. What it is, which problems it solves, how, and where to look next.
type: entry
job: When I hear about Sandloom, I want to know what it is, which problems it solves and how, so I can decide whether to look further and where.
footer_next:
  title: Quickstart
  url: getting-started/
---

# Sandloom

**Sandloom is a Python backend framework designed for AI-driven development.** You can write the code by hand, but the framework is built for coding agents: generators give them a starting point, project rules tell them how to work, and automated checks stop them when they break a rule. Your backend stays quick to change as it grows, runs reliably, and can scale from a single application to microservices without a rewrite.

## Why agents need a framework

Coding agents write features quickly, but that speed fades as a backend grows. In [a study of GitHub projects adopting Cursor](https://arxiv.org/abs/2511.04427), the gain lasted two months, while code complexity rose 41% and stayed there. Each feature tangles the code a little more. An agent can't hold the whole system in mind, so it misses connected code and breaks things it never read. Agents working in parallel collide. Rules written in a prompt are only requests.

Sandloom keeps your business logic atomic, isolated from other parts of the product and from the infrastructure. Automated checks hold those boundaries, however large the backend gets. An agent only has to understand the part it is changing, several agents can work side by side, and a broken rule fails a check instead of slipping through.

!!! tip "Start here"

    === "With your coding agent"

        Tell your agent what to build and point it at the docs. It needs Python 3.11+, uv and Docker.

        ```text
        Build this backend with Sandloom: <what my product does>

        Follow https://sandloom.dev/docs/getting-started/index.md to create the project,
        then follow the AGENTS.md it generates.
        Before you say it is done, run: uv run sandloom test
        ```

    === "Yourself"

        New to Sandloom? The **[quickstart](getting-started/index.md)** takes you from an empty folder to a backend running on your machine, with its tests and checks passing.

        Then follow **[Tutorial: your first feature](getting-started/tutorial.md)**. Step by step, you build a small task list: statistics count each new task, and the task list asks them whether the quota allows another.

Agents can read these docs directly: <a href="llms.txt"><code>llms.txt</code></a> is the index, and every page is also served as Markdown when you append `index.md` to its URL.

## Who Sandloom fits

Sandloom fits a backend with several business areas that people and coding agents keep changing. It suits a product that needs each customer's data kept separate, access control and reliable events, and that may later split into services.

It is probably not for a tiny endpoint or a short script, where the structure is more work than it saves. The same goes for an existing application that can't be divided into separate parts, and for a team that doesn't want to follow the rules and run the checks in CI.

## Where to look next

<div class="grid cards" markdown>

-   :material-sitemap-outline: **[How Sandloom works](architecture.md)**

    See how the parts fit together, and which piece you write for each need.

-   :material-application-outline: **[Examples](https://github.com/Sandloom-dev/sandloom/tree/main/examples)**

    Read real applications: a minimal app, a SaaS with billing, a process with rollback.

-   :material-puzzle-outline: **[Standard modules](modules/standard/index.md)**

    Add ready-made identity, tenancy and billing, and an [admin console](admin-console/index.md).

-   :material-book-open-outline: **[Reference](reference/index.md)**

    Look up a name, a command or an error.

</div>
