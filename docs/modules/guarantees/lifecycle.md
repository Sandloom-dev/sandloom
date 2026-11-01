---
title: Transaction and replay lifecycle
description: The lifecycle matrix - for every route kind and every non-route unit, its units, session check, commit-time re-check, idempotency and write admission; the operation-intent mode.
type: concept
---

# Transaction and replay lifecycle

Every route kind and every unit outside a route has exactly one row below,
and every unit has one owner. Read the row of the code you write: it says
which units exist, when the session is checked, whether the commit-time
re-check runs and which idempotency mode applies. The terms are defined in
[Units of Work](../database/units.md), [Write admission](write-admission.md) and
[Route policy and sessions](../http/route-policy.md).

**Commit semantics are the same everywhere:** a unit commits when its body
succeeds and rolls back on any exception; a commit runs `before_commit`,
`COMMIT`, outbox dispatch and `on_commit`, in that order. Queued effects are
never dropped.

## Route kinds

"Re-check" is the commit-time session check; "mode" is the idempotency mode
(B body replay, S status replay, X release and re-execute; see
[Idempotency](../http/route-policy.md#idempotency)).

| Route kind | How it is declared | Units | Session check | Re-check | Idempotency (automatic) |
| --- | --- | --- | --- | --- | --- |
| **ordinary read** (GET, HEAD, OPTIONS) | default | request unit, lazy (first `db()`) or eager (`DbSession`); commits on success, so `before_commit` / `on_commit` run | live, at start | armed automatically **when the unit wrote**; always with `at_commit()` | none |
| **ordinary write** (any other method) | default | request unit on the write store, eager (the durable claim runs before the handler) | live, at start | armed: before the COMMIT of every committing unit; every unit with `at_commit()` | mode S, or B with `replay_safe(reason=)` |
| **sensitive write** (`money`, `admin`, `security`) | risk at its source; `sensitive(risk)` on a hand-rolled check | as an ordinary write, plus one automatic audit row | live; step-up for `security` | as an ordinary write | mode S; money also needs an effect dedupe record ([SANDLOOM-R009](../../reference/error-codes.md#sandloom-r009)) or answers `428` ([SANDLOOM-R010](../../reference/error-codes.md#sandloom-r010)) |
| **public** | `public(reason=)` | by method | none | none (no subject) | none; an explicit guard keeps its options (anonymous callers never get a stored body) |
| **token issuer** | `public(reason=, issues_tokens=True)` | as a write | the live issuance check | none | none; an explicit guard must be mode X ([SANDLOOM-B019](../../reference/error-codes.md#sandloom-b019)) |
| **webhook** | `public(reason=)`, signature verified in the handler | request unit, eager | none | none | the provider's event id claimed with `durable_inbox` or a unique key in the same unit |
| **streaming / SSE** | `no_idempotency(reason=)` | the request unit commits before the first byte; the body generator uses its own `open_uow()` read units | live, at start | before the pre-stream commit, if armed | none |
| **operation intent** (billing checkout and portal) | `operation_intent(reason=)` | no request unit; sequential `open_uow()` units | live, at start | not automatic: `release_check()` | an explicit mode-X guard is required (SANDLOOM-B019) |
| **committing read** | `at_commit()` | as a read | live | before the COMMIT of **every** unit the request opens, empty ones included | none |

## Units outside a route

| Unit | Commit | Re-check | Dedupe | Write admission |
| --- | --- | --- | --- | --- |
| **local read contract** | the provider's read unit or the request's shared read-scope connection (READ COMMITTED); **always rolled back**; results memoised per request, the memo cleared by any command | none (nothing commits) | none | cannot write: `ReadContractWriteError` |
| **planned read** (`planned=True`, planner operations) | run by the planner on its own read sessions, rolled back | none | none | read-only |
| **remote read contract** | the provider's read unit, rolled back | none; reads are allowed under an armed re-check | none | the provider's own scope |
| **local command** | the provider's command unit commits before returning | the caller's armed re-check runs in the provider unit before its COMMIT | the command log in the provider database, or the fail-closed cache fallback | a fresh scope (its own effect) |
| **remote command** | the same, in the provider process | propagated in the MAC-covered RPC context; the provider re-checks before its COMMIT | the same | the provider's own scope |
| **consumer** | per delivery; rollback and redelivery on error | none (no HTTP subject) | `durable=True`: a claim on the envelope id in the consumer unit's database | a fresh scope per delivery |
| **task** | `open_uow()` blocks inside `run_async` | none | effect level (inbox, unique key, enqueue dedupe) | a fresh scope per block |
| **saga step** | one unit per step | none | the command log and the step transition | a fresh scope per step |

## Operation-intent mode

Billing's checkout and portal routes call an external payment provider (PSP)
and return a bearer URL. They must never hold a database transaction, a
connection or a row lock across the PSP call, and must re-read the caller's
authority right before releasing the URL. The mode makes those rules
kernel-enforced:

1. **Declaration.** `operation_intent(reason=)` with an explicit
   `idempotency_guard(scope=..., fail_closed=True, store_response=False)`
   (mode X). A request-session dependency on the route is refused
   ([SANDLOOM-B019](../../reference/error-codes.md#sandloom-b019)).
2. **Units.** No request unit: `db()` outside an `open_uow()` block raises
   [SANDLOOM-R002](../../reference/error-codes.md#sandloom-r002). Units are sequential: one
   may not open while another is open
   ([SANDLOOM-R008](../../reference/error-codes.md#sandloom-r008)).
3. **External calls.** Each PSP call runs inside
   `async with external_call("psp"):`. Entering is refused while any unit of
   the request is open or a contract read-scope connection is held, and while
   the call is active no unit, read-scope connection or connection checkout
   may start in the request, in any task (SANDLOOM-R008).
4. **`release_check(account, *, authority)`.** Refused while a unit is open.
   For an organization account (`ResolvedAccount(kind="org", ...)`) the
   `authority` callback is required
   ([SANDLOOM-R012](../../reference/error-codes.md#sandloom-r012) otherwise); billing passes
   its authority re-read (403 role lost, 409 changed). It then runs the
   session check (401) and fails closed on any error. For its whole duration
   it holds the same exclusion as `external_call`; the only database work
   allowed is the callback's own read-contract calls.
5. **Release rule**, checked at response time. A 2xx response must match one
   of two cases, otherwise the body is discarded and the request fails with
   SANDLOOM-R012, so the secret never leaves:
    - **issue:** at least one `external_call`; `release_check()` after the
      last one; after it, exactly one unit commits, executing exactly one
      write statement;
    - **issued retry:** no `external_call`; `release_check()` after the last
      unit committed; no unit writes after it (the stored URL of an already
      issued intent is returned without a PSP call).

    Refusal paths (non-2xx) are unaffected.

The mode is for routes that call an external provider only; it is not a
general escape from the single request unit.

## What breaks it

| Code | What it catches |
| --- | --- |
| [SANDLOOM-B019](../../reference/error-codes.md#sandloom-b019) | an issuer or intent route with a guard other than mode X; an intent route without the guard or with a request session |
| [SANDLOOM-R008](../../reference/error-codes.md#sandloom-r008) | nested or overlapping units; a unit, read scope or checkout during an `external_call` or `release_check` |
| [SANDLOOM-R012](../../reference/error-codes.md#sandloom-r012) | an intent 2xx outside the issue and issued-retry cases; an organization release check without the authority |
| [SANDLOOM-R009](../../reference/error-codes.md#sandloom-r009), [SANDLOOM-R010](../../reference/error-codes.md#sandloom-r010) | a money effect without a dedupe record; a money write without `Idempotency-Key` |

??? note "Guarantees, precisely"

    - Units and their commit semantics: G-UOW-01, 02, 04, 05; G-UNIT-01.
    - The re-check before the COMMIT of every committing unit of an armed
      request, local and remote commands included (G-REVOKE-22, G-REVOKE-24).
    - Automatic idempotency always replays status only; `replay_safe` opts in
      to body replay (G-IDEM-17); the fail-closed in-flight form without a
      request unit (G-IDEM-18).
    - The operation-intent mode, the release rule and the lifetime exclusion
      of `external_call` and `release_check` (G-UNIT-02, G-BILLING-19).

    The ledger with the enforcing tests: [Guarantees & capabilities](../../reference/guarantees.md).

## Design rationale

- [ADR-0010: Public API 1.0](../../adr/0010-public-api-1.0.md), "Transaction and replay lifecycle"
- [ADR-0009: route policy and session freshness](../../adr/0009-route-policy-and-session-freshness.md)
- [ADR-0005: the effect model](../../adr/0005-effect-model.md)
