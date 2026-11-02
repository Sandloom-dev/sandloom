# Sandloom positioning

Status: **approved by the owner, 2026-11-02.**

Fields follow April Dunford's components as adopted in [research.md](../research.md#positioning), plus the core idea and naming. Owner answers were relayed and translated; quotes mark the owner's words. Text marked **Inference** is mine.

## Category

Owner: **"a Python backend framework designed for AI-driven development"**.

The term across the whole site is "AI-driven development", never "agentic development" (owner, 2026-11-02). Published pages already say "Python backend framework for AI-driven development" (`README.md`, `website/index.html`, `docs/index.md`); "designed for" is the owner's wording for new text.

## Alternatives

Confirmed by the owner:

| Alternative | Where it falls short for this reader |
| --- | --- |
| FastAPI, with rules in `AGENTS.md` | The rules are requests; the agent misses or ignores them. Structure, transactions and data separation are whatever the agent wrote that day. Source: `agentic-pains.md` pains 1, 5 |
| Django | Same: an agent can import across apps and tangle them; nothing fails when it does. **Inference** |

Considered, kept as background (not confirmed as reader alternatives):

- An app builder that writes and hosts the code (Lovable, Replit, Bolt): a quick first version, but the backend is hard to grow and split, and nothing enforces its architecture.
- A SaaS starter template: gives a shape once; nothing keeps it after many agent changes.
- Microservices from the start: real boundaries, at an operations cost paid before there are users.

**Owner's decision:** entry pages do not name alternatives. They talk about pains, not competitors.

## Unique attributes

Each checked in the code of this copy on 2026-11-02 unless marked.

1. **Module boundaries checked on every change.** `sandloom new` writes `architecture_rules.yml`, a CI job running `sandloom test --ci` and a pre-commit hook running `sandloom check` (`src/sandloom/core/cli/project.py`, lines 21-22, 898, 1167-1171). A cross-module import fails with SANDLOOM-C001 and exit 1 (captured in `README.md` and `agentic-pains.md`; not re-run in this session).
2. **Business logic isolated from infrastructure.** Modules use capabilities through the SDK; drivers selected by configuration; backend SDKs and raw clients rejected in modules (C003, C004).
3. **The framework owns what is easy to get wrong.** Transaction, outbox, durable consumer dedupe; `db().commit()` rejected (C006). Routes authenticated by default (SANDLOOM-B008, `src/sandloom/core/route_policy.py`); `TenantScoped` rows filtered, RLS written by migrations.
4. **Versioned contracts and events** with snapshots that fail on a breaking change (T004, T005, T007).
5. **Tuned for coding agents.** `sandloom add` generators; generated `AGENTS.md` and `CLAUDE.md` (`project.py` lines 1144-1145); `sandloom context`, `graph`; 29 beginner SDK names; error codes with `sandloom explain`.
6. **One gate**, `sandloom test`; generated apps limit function complexity (`max-complexity = 10`, `project.py` line 191).
7. **Same modules, monolith or microservices**, by configuration; existing data moves in a separate offline step (`sandloom migrate extract-module`).
8. **Standard identity, tenancy and billing modules** under the same rules.

## Value

Owner's promise: **"You can write the code yourself, but everything is tuned for coding agents: generators, rules the agent reads, and checks that fail when a rule is broken"**, and the backend gets **speed of development, reliability, scalability, and a split from a monolith into microservices without rewriting code**.

Fine print kept one link deeper: speed is not measured (no number may be stated); "without rewriting code" covers module code — deployment still needs configuration, and existing data moves in a separate step.

## Best-fit readers

- **Fit:** someone building a product backend with coding agents that must keep growing: a vibe coder steering an agent, a small team running several agents, a developer who reviews agent code; several business areas; customers' data to keep apart.
- **Not:** a tiny endpoint or script; an existing app whose internals cannot become isolated modules; a team that will not run the checks in CI. (Production `docs/index.md` "Who it fits".)

## Core idea

Owner: **"Sandloom keeps your business logic atomic and isolated from each other and from the infrastructure."**

This is the meaning; page text rewrites it into a natural sentence shown to the owner (sandloom-docs-writing step 1).

## Naming

- **Called:** Sandloom; "a Python backend framework designed for AI-driven development".
- **Use:** "AI-driven development" (never "agentic development"); coding agent; module / part of the product; checks; generators.
- **Avoid at entry depth:** kernel, driver, capability, unit of work, outbox, inbox, RLS, saga, projection, idempotency, "batteries" (say "standard modules"), "AI-first".
- **Entry pages:** no competitor names.

## Sources

The owner's answers of 2026-11-02 (relayed); `README.md`, `website/index.html`, `docs/index.md` (taken as production: this copy has no git history); `design/docs-writing/agentic-pains.md`, `research.md`; `docs/getting-started/glossary.md`; `src/sandloom/core/cli/project.py`. Not available: tm task notes, `kg_search`, `memory_retrieve`, issues, DX eval results.
