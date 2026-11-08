# Ledger delta: /ready is not green with an unusable taskqueue (KB-02, 2026-11-08)

KB-02 of `design/kernel-boundary/migration-plan.md` (wave W0) closes the owner's
pre-implementation item "`/ready` 200 with an unusable taskqueue" (ADR-0012
Context, invariant I-20: no silent degradation).

| Id | Disposition |
| --- | --- |
| G-OPS-23 | New: while a loaded module has a tasks submodule (tasks or periodic tasks), `/ready` opens the default taskqueue through the cached readiness probes and answers 503 with `taskqueue_status: fail`, naming the capability, when it cannot open; with no such module the taskqueue is never probed and readiness is unchanged; `/health` never fails on it. Enforced by `test_ready_taskqueue.py`. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
