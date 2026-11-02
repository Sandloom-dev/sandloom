# Sandloom positioning

Status: **approved by the owner, 2026-11-02**, field by field, while writing Overview (see [its brief](briefs/overview.md)) and in the rounds after it. The inventory cells and the evidence are checked facts, not owner decisions. Every page brief takes its one message, promise and core idea from here (`sandloom-docs-brief`).

Fields follow April Dunford's positioning components (*Obviously Awesome*, 2019): alternatives, unique attributes (here: advantages), value, best-fit customers, category.

## Category

*Owner:* a Python backend framework designed for AI-driven development. You can write the code yourself, but everything is tuned for coding agents.

## Alternatives

*Owner, 2026-11-02:* the two that matter are FastAPI with rules in `AGENTS.md`, and Django. Entry pages do not name alternatives: they speak about the pains, not competitors. The rest of the table is a proposal the owner asked to think through from several sides.

| Side | What the reader would do instead | Where it falls short for them |
| --- | --- | --- |
| Framework | FastAPI with a structure of their own and rules in `AGENTS.md` (*owner*) | The agent designs the architecture and the rules are requests; the code tangles as it grows |
| Framework | Django (*owner*) | Apps share models and reach into each other; nothing checks the boundaries; one deployment |
| Framework, other language | A modular framework elsewhere: Spring Modulith, NestJS, Encore | Leaving Python; Encore ties the code to its own infrastructure |
| Tooling | A boundary linter on their own code: import-linter, Tach | Checks imports only; transactions, tenancy, events and the split into services stay theirs to build |
| Starting point | A SaaS template or boilerplate | Fast first day; the structure is a suggestion and drifts once the agent starts changing it |
| Architecture | Microservices from the first day | Boundaries hold, but each service costs operations, and a wrong split is expensive to undo |
| Platform | An app builder such as Lovable, Replit or Bolt, with a hosted backend | Fast start, little control over the backend as the product grows |
| Doing nothing | Let the agent write, and refactor or rewrite later | The rewrite arrives when the product is busiest |

## Advantages

### Main advantages

*Owner, 2026-11-02*, in the owner's names, with the inventory rows they cover:

- For working with agents:
  - a strict, machine-checked architecture (rows 2, 3);
  - atomic, isolated business logic (rows 1, 6);
  - skills and a toolset for the agent (rows 4, 5).
- For the backend:
  - independence from the infrastructure (row 10);
  - reliability by default (row 7);
  - security by default (row 8);
  - monolith or services (row 9);
  - ready-made modules (row 11).

Overview stays as approved; these lead on the pages written after it.

### Inventory

What Sandloom has that the alternatives lack, as an inventory pages choose from. Each row is checked where its proof column says; [agentic-pains.md](agentic-pains.md) has the detail. "Plain words" is how an entry page may say it.

| # | Advantage | Plain words | Proof | Answers |
| --- | --- | --- | --- | --- |
| 1 | Isolated modules: a module cannot import another; they talk through typed, versioned contracts and events | Each part of the product is closed off from the others | `sandloom check` C001; snapshots T004, T005, T007 fail on a breaking change | Decay; changing one place breaks another |
| 2 | Rules enforced, not requested: static checks, startup checks, architecture tests, each failure with a code, the fix and `sandloom explain` | A broken rule fails a check and says how to fix it | C001, C006 captured with exit 1; `docs/reference/error-codes.md` | Rules in a prompt are requests |
| 3 | One gate: `sandloom test` runs tests, lint, types, checks and generated-file drift | "Done" means one command passed | `sandloom test: OK` in the quickstart smoke test | Looks done and is not |
| 4 | Generators and one module layout: `sandloom new`, `sandloom add module`, `event`, `contract`, `consumer`, `saga`, `projection` | The agent starts every feature from the same correct shape | Generated app of 44 files; the CLI | Agent can't design an architecture |
| 5 | Context sized for agents: a generated 53-line `AGENTS.md` and skills, `sandloom context`, `graph`, `explore`, 29 SDK names for ordinary modules, `llms.txt` | The agent reads a page, not the whole codebase | 2.9 KB `AGENTS.md`; 1.1 KB `sandloom context` on a sample module; byte budget test | Overload; every session starts from zero |
| 6 | Parallel work: each module owns its files, migration chain and tests, with nothing central to wire | Several agents work on separate parts at once | Two branches adding modules merged cleanly, 2026-11-02; agents not tried | Coupled code does not split across subagents |
| 7 | Reliability by default: the framework owns the transaction, the outbox, durable consumer deduplication and idempotent writes | A crash or a retry does not half-save a change, lose a message or charge twice | C006 forbids `commit()`; footgun probes | Retries and crashes corrupt data |
| 8 | Secure by default: every route requires login unless it declares `public(reason=)`; tenant rows filtered automatically by `TenantScoped`; migrations also write a PostgreSQL row-level security policy a deployment can switch on | Login checks and each customer's data kept separate are not left to the agent | SANDLOOM-B008; C014, C015, T006 | What nobody asked for is not written |
| 9 | Monolith or services by configuration: `SANDLOOM_MODULES`, `SERVICE_BINDINGS`; moving existing data is a separate offline step | Start as one application, split into services without rewriting code | Deploy profiles; `sandloom add deploy` | Scale; a wrong early split |
| 10 | Infrastructure behind capabilities: drivers chosen in configuration | Change the database, cache or mail service without touching business code | `INFRA` drivers; capability interfaces | Lock-in to one provider |
| 11 | Ready-made modules: identity, organizations, billing, an admin console | Sign-in, teams and payments on day one | `sandloom_identity`, `sandloom_tenancy`, `sandloom_billing`, `admin/` | Each feature reinvents plumbing |
| 12 | A familiar stack: FastAPI, SQLAlchemy, PostgreSQL, Redis; tests drive the real runtime (`as_user`, `deliver`, `published`) | Plain Python you already know | `pyproject.toml`; `docs/reference/testing.md` | Leaving Python, learning a platform |

*Owner:* the first three combine as "generators give the agent a starting point, project rules tell it how to work, and checks that fail stop it when it breaks a rule".

## Pains

*Owner, 2026-11-02:* the pains Sandloom closes, with evidence and mechanism in [agentic-pains.md](agentic-pains.md):

- the architecture decays, and every change and feature costs more (row 1);
- an agent can't design an architecture and hold it in mind (row 2);
- tightly coupled code does not split across subagents (row 3);
- in a complex project the agent is overloaded and misses connected things (row 4);
- rules in a prompt are only requests (row 5);
- moving from a monolith to microservices means rewriting (row 13).

*Owner, 2026-11-02:* the list is closed at these six, and each is answered by:

| Pain | Answered by |
| --- | --- |
| Decay | Atomic, isolated business logic; the machine-checked architecture |
| Can't design and hold the architecture | Skills and the toolset |
| Coupled code does not split across subagents | Atomic, isolated business logic |
| Overload | Skills and the toolset; reliability and security by default |
| Rules are requests | The machine-checked architecture |
| Rewrite to reach microservices | Monolith or services; independence from the infrastructure |

Overview carries the first five.

## Value

*Owner:* speed of development, reliability, scalability, and a split from a monolith into microservices without rewriting code. As written on Overview: your backend stays quick to change as it grows, runs reliably, and can scale from a single application to microservices without a rewrite.

## Best-fit readers

- Fits: a backend with several business areas that people and coding agents keep changing, which needs each customer's data kept separate, access control and reliable events, and may later split into services.
- Arrives: a developer who knows FastAPI and SQLAlchemy, a vibe coder steering an agent, a coding agent that has never seen Sandloom (*owner*).
- Probably not: a tiny endpoint or a short script; an existing application that can't be divided into parts; a team that won't follow the rules and run the checks in CI.

## Core idea

*Owner:* "Sandloom keeps your business logic atomic and isolated from each other and from the infrastructure." On Overview: "Sandloom keeps your business logic atomic, isolated from other parts of the product and from the infrastructure."

## One message

*Owner:* Sandloom is a Python backend framework designed for AI-driven development.

## Naming

- *Owner, 2026-11-02:* "AI-driven development" across the whole site, never "agentic development".
- On entry pages, plain words instead of terms the reader has not met: "parts of the product", not "modules" (*owner*, after an outside developer stumbled on "modules").

## Decided after Overview

- *Owner, 2026-11-02:* against FastAPI with rules in `AGENTS.md`, Sandloom answers with all its advantages together, not one.
- The alternatives beyond the owner's two stay as background for writers; no page names them.
