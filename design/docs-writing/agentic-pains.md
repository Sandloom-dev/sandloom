# The pains of agentic development that Sandloom closes

Status: **draft for the owner's approval; no page text depends on it yet.** Sources retrieved 2026-11-02.
Input for Overview. The owner's framing: Sandloom is designed for agentic development, so the page starts from the pains of that work.

Text marked **Inference** is mine. "Closes" reports the mechanism, checked in the code, the generated app or the error catalogue. Nothing here measures how often Sandloom prevents a failure: the DX eval's product suite has footgun probes for these classes, and no results from it are stored in this repository.

## The thread

**Inference.** A rule written in a prompt or `AGENTS.md` is a request, and an agent can miss or ignore it. Sandloom moves the rules that matter into code: defaults the agent gets without asking, and checks that fail. That is the one message the pains below support.

Pain 1 is the result the reader feels; pains 2 to 4 are why agents get there; the others are how it shows.

## Pains

| # | Pain | Evidence | What Sandloom does | Strength |
| --- | --- | --- | --- | --- |
| 1 | The architecture decays. The first weeks are fast, then every change and every new feature costs more. | [He et al., MSR 2026](https://arxiv.org/abs/2511.04427), a difference-in-differences study of GitHub projects adopting Cursor: velocity rises only for the first two months; static analysis warnings rise 30% and code complexity 41% and stay up; that complexity drives the later slowdown. | The boundaries are checked on every change, not at the start: `sandloom new` writes `architecture_rules.yml`, a CI job running `sandloom test --ci` and a pre-commit `sandloom check`. A new module is discovered with nothing to wire. Modules talk through versioned contracts and events. | Enforced between modules. Inside a module, ruff limits each function's complexity (C901, 10) from the next release, which does not stop a module growing tangled across functions. Someone can still edit the rules file or the CI job |
| 2 | Designing an architecture and holding it in mind is hard for an agent. | [Dente et al., 2026](https://arxiv.org/abs/2605.06445): agent performance on backend tasks falls as structural requirements accumulate. | The shape is decided: one module layout, `sandloom add` generators, a [choosing page](../../docs/modules/between/index.md) for events and contracts, `sandloom graph` and `explore` to see it. | Helps. Where the module boundaries go is still the agent's or the person's call |
| 3 | Tightly coupled code does not split across subagents. | [Anthropic's C compiler](https://www.anthropic.com/engineering/building-c-compiler): on one giant task, 16 agents hit the same bug and overwrote each other. [Cognition](https://cognition.ai/blog/dont-build-multi-agents): subagents act on conflicting assumptions. | Each module owns its files, migration chain and tests, with nothing central to wire. Tested 2026-11-02 with two branches adding modules, no agents: all source merged cleanly; the one conflict was the generated `.sandloom/inspection.json`, and `sandloom generate` rewrites it (task `inspection-merge-conflict`). | Holds for separate modules on that test. Work across modules still needs its contract agreed first; not tried with real parallel agents |
| 4 | In a complex project the agent is overloaded and misses connected things. | [Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents): an attention budget, and recall falls as context grows. Our baseline: a 25k-token contract with 20-30% relevant to a task. | A generated `AGENTS.md` of 2.9 KB, and the framework's own held to a budget by a test. `sandloom context <module>` gave 1.1 KB on a sample module. Transactions and tenancy need no remembering. Gates catch what is missed. | Helps; size measured, the effect on agents not re-measured |
| 5 | Instructions are requests. The agent misses or ignores them. | The SaaStr founder on Replit's agent: no way to [enforce a code freeze](https://incidentdatabase.ai/reports/5578/). [Gloaguen et al.](https://arxiv.org/abs/2602.11988): context files do not generally raise success. | `sandloom check` and startup checks fail on boundary, transaction and route rules, each with a code (captured: C001, C006, exit 1). | Enforced for the rules checks cover; `src/sandloom/AGENTS.md` lists those no check enforces yet |
| 6 | The agent changes one place and breaks another it never read. | [DORA 2025](https://cloud.google.com/blog/products/ai-machine-learning/announcing-the-2025-dora-report): more change volume brings instability without strong foundations; loosely coupled teams gain. | Modules cannot import each other. Contract and event snapshots fail on a breaking change (T004, T005, T007). | Enforced |
| 7 | Every session starts from zero. | [Anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents): each new session begins with no memory of what came before. Our baseline experiment: 167k-257k tokens per feature, and every agent read kernel source. | `sandloom new` writes a 53-line `AGENTS.md`, `CLAUDE.md` and skills. `sandloom context` and `graph` map a module. Ordinary modules need 29 SDK names. | Helps; the baseline predates these changes and has not been re-measured |
| 8 | What nobody asked for does not get written: login checks, tenant isolation. | [Veracode 2025](https://www.veracode.com/press-release/ai-generated-code-poses-major-security-risks-in-nearly-half-of-all-development-tasks-veracode-research-reveals/): vulnerabilities in 45% of cases across 100+ models. | A route is authenticated unless it declares `public(reason=)` (B008). `TenantScoped` filters rows and migrations write the RLS policy. | Default for these two classes; not a general security scan |
| 9 | Retries and crashes corrupt data: a double charge, a lost event. | Our footgun probes, one class of production incident each. No external figure found. | The framework owns the transaction, the outbox, durable consumer dedupe and idempotency on mutating routes. | Default; effects outside a durable consumer need their own dedupe, and no check enforces that yet |
| 10 | "Almost right": it looks done and it is not. | [Stack Overflow 2025](https://survey.stackoverflow.co/2025/ai): 66% cite this as the top frustration, 45% say debugging AI code takes longer. 46% distrust accuracy, 33% trust it. | One gate, `sandloom test`. Errors carry a code, a fix and `sandloom explain`. Test kit drives the real consumer runtime. | Catches framework mistakes, not wrong business logic |
| 11 | Each feature reinvents the last one's plumbing. | [GitClear](https://www.gitclear.com/ai_assistant_code_quality_2025_research), a vendor: refactoring fell from 25% to under 10% of changed lines from 2021 to 2024, clones rose from 8.3% to 12.3%. Correlation, and He et al. found no significant effect on duplicate lines. | `sandloom add` generators, the kernel, drivers behind capabilities, standard identity, tenancy and billing modules. | Helps |
| 12 | Code arrives faster than a person can review it. | **Inference** from pains 6 and 10; DORA calls AI an amplifier of what a team already has. | Changes stay inside one module. Route and contract snapshots show policy changes in the diff. `sandloom graph` and `explore` show the links. | Helps; unmeasured |

## What Sandloom does not close

- Most decay inside one module: Sandloom limits how modules depend on each other and how complex one function is, not how tangled a module becomes.
- An agent with production credentials can still destroy data. Migrations are a dry run without `--apply`, but Sandloom does not sandbox an agent. The Replit incident is pain 5, not a promise about this class.
- Wrong business logic that passes its tests.
- General application vulnerabilities outside routes, sessions and tenancy.
- Whether agents go faster at all. [METR](https://metr.org/blog/2026-02-24-uplift-update/) calls its newer data an unreliable signal, and its 2025 slowdown figure predates current tools, so the page should not cite either.

## Open

- These are secondary evidence and vendor reports, mostly from 2025. Veracode and GitClear sell related products.
- Before any number goes on a public page: run the product suite, paid, for Sandloom's own figures. Its tasks `p3_add_module_n2` and `p4_add_module_n7` compare the cost of adding the 2nd module and the 7th, the direct test of pain 1.
- He et al. studied open-source projects adopting one tool, and its findings may not carry over to other agents or to closed teams. Velocity figures were not checked per horizon beyond the paper's summary.
- The owner added pains 2, 3 and 4 on 2026-11-02; ask before treating the list as closed.
- `SANDLOOM-C001`'s fix text names stale API (task `check-messages-stale`), and it would appear in any proof shown.
