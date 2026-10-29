# ADR-0009: Route policy follows authority and effect — deny-by-default, live sessions, no route classes

- **Status:** Accepted (2026-10-25; owner decisions in "Decisions recorded")
- **Date:** 2026-10-25 (revised the same day: author simplicity as the first
  criterion; risk moved from route tags to its source)
- **Deciders:** Sandloom maintainers and the owner
- **Implementation brief:**
  `docs/design/route-policy-implementation-brief-2026-10-25.md` (internal research, retained in the original private repository)
- **Amends:** [ADR-0003](0003-fewer-sync-rpc-membership-reads.md). It
  supersedes ADR-0003's "Operation classes (normative)" section, its route
  tables ("Platform routes → classes", "Platform owner console routes") and
  decision 9. Everything else in ADR-0003 stands:
  - membership caching options;
  - failure rules;
  - the TOCTOU remedies;
  - operation intents;
  - "Guarantees, precisely".
- **Related:** ADR-0005 (effect model), ADR-0006 (accepted risks), DX study
  R2/R4.
- **Research:**
  `docs/design/operation-classes-research-2026-10-25.md` (internal research, retained in the original private repository):
  the inventory, the survey, candidates A–E, the simplicity scorecard, the
  mistake catalog, the trade-offs and the sources.

## Context

ADR-0003 has every route declare an operation class:

| Class | Meaning |
| --- | --- |
| `P` | public |
| `R` | ordinary read |
| `RS` | sensitive read |
| `W` | ordinary write |
| `F` | financial |
| `A` | admin or security |

Next to the class sit the `strict_revocation()` /
`revocation_freshness(N)` / `revocation_at_commit()` markers and
`AUTH_REVOCATION_STRICT_PATH_PREFIXES`. The inventory on `b231a5ca` found
the following.

1. **The class has no runtime effect.** Of its three startup checks, two
   only demand a redundant marker: the live check is already the default,
   and budgets are refused on writes. The one real check is that `F`
   refuses a weak idempotency guard. An absent guard passes.
2. **The prose promises are unchecked:**
   - that non-`P` routes are authenticated;
   - the `F`/`A` commit-time re-check;
   - `A` audit;
   - `F` idempotency;
   - step-up outside identity.
3. **The letter mixes independent facts:** the principal, the effect
   (already given by the method), risk, freshness, and the commit-time and
   idempotency needs. A typical admin route carries 4–6 policy lines; a
   payment route carries 3–4.
4. **Bounded staleness is unused and saves no round trip,** because the
   block mark is read live anyway. The strict prefixes exist only to
   override it.
5. **There are two subject marks** (`tokens_valid_after`, `blocked`) only
   because of that budget path.

The owner's overriding goal is that a module author or AI agent writes
nothing beyond business logic in the typical case, with every mistake caught
deterministically and a fix-naming error, and no guarantee weakened.

Best practice (research §2) points the same way. The strongest systems put
each requirement in the layer that every path crosses:

- a global auth default with an explicit public opt-out: Rails 8
  `allow_unauthenticated_access`, NestJS `@Public()`, ASP.NET
  `FallbackPolicy`, Django `LoginRequiredMiddleware`;
- revocation in the token-validation layer (CAE, introspection, Keycloak
  not-before);
- recency on the credential flow: Ory privileged sessions, Phoenix and
  GitHub sudo mode;
- policy on the entity (Ent / Meta privacy);
- audit at the API platform (CloudTrail);
- idempotency at the HTTP layer (Stripe, the IETF `Idempotency-Key` draft).

## Decision

### Defaults every route gets (nothing to declare)

1. **Deny by default.**
   - Every API route resolves an authenticator,
     `@authenticates(kind="user" | "service")`, or declares
     `public(reason=...)`.
   - Token issuers declare `public(issues_tokens=True, reason=...)`; their
     live issuance check is unchanged.
   - PSP webhooks are `public(reason=...)`. The verified signature is their
     authorization, checked inside the endpoint, as today. They are not
     service principals.
   - `create_app` refuses a route that is neither, or both.
2. **Live session check.** Every user authenticator reads the subject-state
   record live: one `GET`, memoised per request. Fail-closed / fail-open
   follows `AUTH_REVOCATION_FAIL_CLOSED`, unchanged. There is no
   route-level freshness setting.
3. **At-commit re-check on every authenticated write** (any method other
   than GET, HEAD or OPTIONS).
   - Today's `subject_commit_check` runs before COMMIT of every UoW the
     request opens.
   - **Self-revocation is excluded by "check, then revoke"** (detailed
     below), so a password change, self-deactivation, leaving an org or an
     ownership transfer works with no declaration.
   - It **travels in the MAC-covered RPC context** to remote commands, whose
     provider registers the same check on its own UoW against the shared
     `CACHE_URL`.
   - Local and remote behave the same. The guarantee is unchanged:
     serializable, not linearizable (ADR-0003).

   **Self-revocation, exactly.** Let `S` be the request's authenticated
   subject (from the bearer) and `U` a UoW of that request with the
   at-commit check armed.

   1. When code in `U` calls `revoke_user_tokens_on_commit(S, ...)` or
      `block_user(S, ...)` (the only two writers of `S`'s subject record),
      the kernel FIRST runs the at-commit check for `S` right there. That is
      a live read of the record, decided against the bearer's `iat_us` with
      the usual rules: `401` revoked / blocked, `503` on a fail-closed
      outage. It runs BEFORE this call writes anything, including the
      `require_immediate=True` early write.
   2. If the check passes, the kernel records `S` in `U`'s `self_revoked`
      set. Only then does the revocation proceed: early write, outbox event,
      post-commit write.
   3. At COMMIT, the armed check skips every subject in `U.self_revoked`.
      Any other subject is checked as usual.
   4. A command contract that revokes `S` in its provider's UoW does the
      same in the provider, because the propagated context names `S`. Its
      response carries the revoked subjects (MAC-covered, as part of the
      result envelope; a local call shares the context variable). The
      calling UoW adds them to its own `self_revoked`, so its COMMIT is not
      refused by a mark the request itself caused.

   Effect: a withdrawal by someone else that lands before the self-revoking
   call is still seen (`401`, rollback). The window becomes "self-revoking
   call → COMMIT" instead of "final read → COMMIT". That is the same
   serializable-not-linearizable guarantee: the revocation must already be
   the last fallible step of the UoW (AGENTS.md), so the window is the same
   length. The rule is deterministic: it depends only on calls made in this
   UoW, never on timing or mark values. A self-revoking route needs no
   opt-out.
4. **`Cache-Control: no-store`** on authenticated responses, unless the
   route sets its own.
5. **Automatic durable idempotency.**
   - Any write carrying `Idempotency-Key` is deduped by the durable guard
     (ADR-0005), scoped by route template and subject.
   - A route without a request `DbSession` gets the fail-closed in-flight
     form.
   - `idempotency=False` opts a route out (streaming).
   - An explicit `idempotency_guard(...)` remains for custom scopes.

### Risk is declared at its source, once, by its owner

6. **Admin.** The framework's admin gates carry `risk="admin"`:
   `require_org_role(≥ admin)`, `require_platform_admin` and
   `require(Role("admin"))`. This is visible at startup.
7. **Money.** The effect owner declares it once:
   - `@contract_method(command=True, risk="money")`;
   - the `MoneyLedger` model mixin, which names its unique operation-key
     column;
   - `@sensitive_effect("money")` on effect services (operation intents).

   It is detected when the effect runs in a request, before COMMIT.
8. **Security.** The credential owner (identity) uses
   `@sensitive_effect("security", step_up=300)` on credential-changing
   services. `mark_reauthenticated()` after a successful password check
   satisfies the step-up in that request. **Step-up is derived for
   `security` only** (300 s). Admin gates and money effects carry no step-up
   default; an owner may still pass `step_up=` on a specific effect.
9. **The escape hatch** is `sensitive("admin" | "money" | "security")`
   in a route's `dependencies` when its authority check is hand-rolled.
   The marker already returns a dependency; do not wrap it in `Depends`.

These **static** tags (gates, `risk=` on contracts, `MoneyLedger`,
`@sensitive_effect`, `sensitive()`) are what gates behaviour. Effects
observed by tests are informational only (item 14).

### What risk triggers (applied by the framework)

10. **Audit.** One automatic audit row per sensitive write, in the request's
    UoW, `required=True`. It records the actor, route template, method, path
    parameters, risk and status. It never records bodies.
11. **Money dedupe is mandatory.** The effect must sit under a dedupe
    record: the command's own key, the ledger row's operation key, or the
    request's `Idempotency-Key` (`request_operation_key()`).
    - A developer omission raises `MoneyEffectWithoutDedupeError` before
      COMMIT.
    - A client that sent no key gets `428 Idempotency-Key required`.
12. **Step-up.** Where an effect declares `step_up=N`, the `auth_time` claim
    (never `iat`) must be ≤ N s old, or the request must have called
    `mark_reauthenticated()`. Otherwise:
    `401 WWW-Authenticate: Bearer error="insufficient_user_authentication", max_age=N`
    (RFC 9470). It is checked before the effect.

### One subject-state record replaces the two marks

13. The record is `sandloom:auth:subject:{id}`.
    - **Values:**
      - `v1:after:<us>`: tokens issued at or before `<us>` are revoked. It
        is written at `now + skew`, with TTL, monotonic, as today.
      - `v1:blocked:<since_us>`: no TTL. It orders above every `after:`, so
        a revocation never unblocks.
    - **Unblock** replaces `blocked` with `after:<now+skew>` by
      compare-and-set, after commit. It never deletes.
    - **Unchanged:** a corrupt value fails closed and the issuance check
      repairs it; block audit and event order; the durable revocation
      consumers; `require_immediate=True`.
    - **Recovery:** `sandloom backup reconcile-security` re-derives the single
      record.

### The route snapshot

14. Each module's `routes.snapshot.json` records the static resolved policy
    per route:
    - `auth`;
    - `effect`;
    - `session`;
    - static `risk`, with its source (gate, `sensitive()`);
    - `idempotency`;
    - `audit`;
    - `step_up_max_age_s`.

    Drift fails the module's tests (`sandloom routes snapshot --check`). An
    `observed_effects` field (money / security effects that the module's
    own tests saw on that route) is **informational only**: it is written by
    `--write`, ignored by `--check`, and never gates a build or a request.

### Removed from the internal API before the initial public alpha

The following are removed, and `AUTH_REVOCATION_STRICT_PATH_PREFIXES` moves
to `config_tiers.REMOVED` with inert value `[]`:

- `operation_class`, the `P`/`R`/`RS`/`W`/`F`/`A` letters,
  `OPERATION_CLASSES`, `STRICT_CLASSES`, `OperationClassError` and
  `assert_operation_classes`;
- `strict_revocation()`, `revocation_freshness()` and
  `sandloom.core.auth.local_marks` with its invalidation channel;
- `AUTH_REVOCATION_STRICT_PATH_PREFIXES`;
- identity's `session_not_revoked`;
- `revocation_at_commit()`. `Depends(at_commit())` stays only for a rare
  non-write route that commits;
- `FinancialGuardError`, replaced by item 11;
- the ADR-0003 route tables and `test_operation_classes.py`'s `EXPECTED`,
  replaced by the per-module `routes.snapshot.json` holding the resolved
  policy (DX R2).

### Checks (deterministic; each error names its fix)

| Check | Where | Error |
| --- | --- | --- |
| authenticated xor `public()` | `create_app` | `RoutePolicyError` ("add `CurrentUser` or a gate, or `public(reason=)`") |
| money effect under a dedupe record | before COMMIT | `MoneyEffectWithoutDedupeError` ("pass `operation_key=request_operation_key()`, or set the ledger operation key") |
| the client sent a key for a money effect | per request | `428` |
| step-up | before the effect | `401` per RFC 9470 |
| live session / at commit | start / before COMMIT | `401 invalid_token` (revoked / blocked), or `503` on a fail-closed outage |
| audit store present | before COMMIT | `AuditUnavailableError` (unchanged) |
| remaining hand-placed guards after auth | `create_app` | `GuardOrderError` (unchanged) |
| hand-rolled role checks, or ledger-like models without a money tag | `sandloom check untagged-sensitive` | warning, waivable by id with a reason |
| snapshot equals resolved policy | module tests | "run `sandloom routes snapshot --write`" |

## Consequences

**Author effort.** These are policy lines beyond business logic. Auth
dependencies and authorization gates count as business logic.

| Route type | Today | After |
| --- | --- | --- |
| ordinary read | 1–2 | **0** |
| ordinary write | 2 (+ guard) | **0** |
| payment (calls a money command, or writes a money ledger) | 3–4 | **0** |
| admin (behind a framework gate) | 4–6 | **0** |
| public | 1 | 1 |
| policy concepts a route author must know | ≈ 10 | **1** (`public()`) |
| concepts for an owner of a sensitive effect | — | +2 (`risk=` on the effect, and its dedupe) |

**Mistakes:**

- **Now impossible:** an accidental unauthenticated route (it is refused at
  startup), a weakened session check, a forgotten audit, a missing
  cross-service commit re-check, the self-revocation at-commit trap, a
  cached sensitive response, and split/monolith divergence.
- **Caught at runtime before COMMIT, deterministically:** a money effect
  without dedupe.
- **Residual (heuristic warning plus snapshot review):** a hand-rolled admin
  check, or an owner forgetting to tag a new money effect. This is once per
  effect, not per route.

**Hot path:**

| Request | Today | After |
| --- | --- | --- |
| authenticated read | one `MGET` of two keys | **one `GET` of one key** |
| authenticated write | one `MGET` (+1 if opted in) | one `GET` + one `GET` before COMMIT |
| keyed write | the guard's statements, if declared | two statements in the UoW |
| sensitive write | as a write | as a write + one audit `INSERT` |
| step-up | — | none (a claim check) |

**Guarantees.**

- **Stronger:**
  - authentication presence;
  - mandatory money dedupe on every path to the effect, where today a route
    without a guard passed;
  - audit;
  - the cross-service commit re-check on all writes;
  - split equality;
  - a single subject record.
- **Unchanged:** everything in ADR-0003 "Guarantees, precisely", operation
  intents, row-lock strict serializability, the fail-closed policy, and
  durable revocation delivery.
- **Given up:** bounded revocation staleness. It is unused and saved no
  round trip; research §4.7 has the bar for reintroducing it.

**Trade-offs taken** (research §3.8):

- T1: one more Redis `GET` per authenticated write, for zero concepts.
- T2: money-dedupe omissions surface when the effect first runs (in a test),
  not at boot. The guarantee holds on every execution.
- T4: keyed writes pay two statements.

## Phased plan (input to DX R4)

1. **Subject record**, and delete the freshness knobs.
2. **Deny-by-default:** `@authenticates(kind=)` and `public()`.
3. **Write and response defaults:** at-commit on writes (self-skip, RPC
   propagation), `no-store`, automatic keyed idempotency.
4. **Sourced risk:**
   - gates, `risk=` on contracts (and in the contract snapshot),
     `MoneyLedger` and `@sensitive_effect`;
   - automatic audit, money dedupe and step-up;
   - `sandloom check untagged-sensitive`;
   - delete `operation_class`;
   - tag the platform's effects (billing credits, ledger and intents;
     identity credential services).
5. **Snapshot (with DX R2):** the static resolved policy (the gate), plus
   informational observed effects. Delete the ADR-0003 tables and
   `EXPECTED`.
6. **Performance measurement.**
   - cachegrind: the authenticated read rung (`L6S5`) and a new
     authenticated write rung, before and after.
   - The Oracle qualification (`benchmarks/qualification/oracle_perf.py`):
     `/s5/`, `/auth/me` and a write route.

   This quantifies the session-record change on reads (`MGET` → `GET`, the
   budget branch removed) and the added before-COMMIT `GET` on writes. The
   numbers are recorded in this ADR's consequences.

Each phase ships with failing-first tests, keeps the split-conformance
matrix and the effect-model suites green, and shows that the derived policy
of every platform route is at least as strict as today's explicit markers.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| (A) keep the letters, derived (DX R4 "class implies strictness") | Cleans up the redundant marker only. Admin routes still need letters, an `F` left at `W` skips its check, and nothing checks auth presence, the commit-time re-check or audit. |
| (B / B′) orthogonal declarations with a per-router risk tag | This was the first draft of this ADR. It is sound, but the risk tag is a per-router declaration whose omission is silent, and audit and idempotency become rules the author can break. E keeps B′'s foundations and removes those declarations. |
| (C) a route → policy file | Drifts from the code and breaks module isolation; the generated snapshot gives the same review surface. |
| (D) a policy engine (OPA / Cedar) for route requirements | The wrong layer. Such engines decide resource access, which the authz algebra already does; these requirements are endpoint and effect properties. |
| Macaroons / Biscuit (freshness in token caveats) | More concepts for every client and service; the single live `GET` already gives next-request revocation. |
| Keep bounded staleness with the merged record | Deferred until a measured case exists. |

## Decisions recorded (owner, 2026-10-25)

1. **At-commit re-check on every authenticated write: yes.** Self-revoking
   routes are excluded automatically by the "check, then revoke" rule
   (Decision 3).
2. **Automatic durable idempotency** on every write carrying
   `Idempotency-Key`: yes. Money dedupe stays mandatory (Decision 11).
3. **Step-up:** 300 s for `security` only, for now.
4. **Observed effects in the route snapshot:** informational only, never a
   gate. Static risk tags are the gate (Decision 14).
5. **Bounded staleness budgets are deleted.** A performance measurement step
   is added (phase 6).
6. **PSP webhooks:** `public(reason=...)` plus signature verification
   (Decision 1).
7. **A `data` risk** for personal-data exports: a follow-up, not in this
   ADR.
8. **An OpenID Shared Signals / CAEP receiver:** roadmap, not now.

## Follow-ups (not part of this decision)

- `data` risk (personal-data exports: read-audit, step-up).
- An inbound OpenID SSF/CAEP receiver (`session-revoked`,
  `credential-change`) that writes the subject record (identity edge
  adapter).
- Reintroducing bounded staleness, only under research §4.7's three
  conditions.
