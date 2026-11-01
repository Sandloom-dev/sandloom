# academy — the worked saga example (ADR-0008)

A course-enrollment flow across three isolated modules, generated with
`sandloom new academy --no-batteries --module enrollment` and filled in:

| Module | Owns | Contract |
| --- | --- | --- |
| `academy.modules.payments` | prepaid balances, charges | `payments.charges.v1` (`charge` / `refund` / `topup`, commands) |
| `academy.modules.notifications` | a message outbox per user | `notifications.mail.v1` (`send`, a command) |
| `academy.modules.enrollment` | enrollments (status = semantic lock) | — it owns the **`ENROLL` saga** |

```text
POST /api/v1/enrollments            202 {enrollment_id, status, status_url}
  └─ in the request's UoW: the enrollment row (pending) + start_saga(ENROLL)

ENROLL  reserve_seat ──► charge ──► grant_access ──► notify
        local           payments    local, PIVOT     notifications
        undo: cancel    undo: refund                  retriable
```

* A short balance makes `charge` refuse (402): the saga compensates
  `reserve_seat` (the seat is `cancelled`) and ends `compensated`.
* After the pivot there is no undo: `notify` is retried until the deadline,
  then parked for an operator (`sandloom sagas list` / `show` / `retry`).
* Every command call carries `saga:enrollment.enroll:<id>:<step>:do|undo`;
  the provider's durable command log makes a re-call replay, never re-charge.
* `GET /api/v1/enrollments/{id}` shows the row's status (`pending` /
  `reserved` / `active` / `cancelled`) next to the saga's.

## The code (Public API 1.0)

Modules import `sandloom.sdk` and the generated contract stubs of
`academy.shared`; each `__init__.py` is one line,
`register = define_module(__name__, version="0.1.0")`, and everything else is
discovered:

* **Providers** — `payments/contracts.py` and `notifications/contracts.py`
  declare `@contract("payments.charges.v1") class Charges` /
  `@contract("notifications.mail.v1") class Mail`, every method a
  `@command(idempotency_key="operation_key")`. `sandloom generate` writes their typed
  stubs to `academy/shared/contracts/` (`PAYMENTS_CHARGES_V1`,
  `NOTIFICATIONS_MAIL_V1`).
* **Typed saga steps** — `enrollment/sagas.py` declares
  `charges = Contract(PAYMENTS_CHARGES_V1)` and names each command through
  it: `step("charge", call=charges.charge, compensate=charges.refund, ...)`,
  `step("notify", call=mail.send, ...)`. The saga definition and its
  startup check are exactly those of the string form.
* **Routes** — `module_router()` authenticates every route (`user:
  CurrentUser`) and binds the tenant (every table is `TenantScoped`).
* An advanced name carries `# sandloom: advanced <reason>` on its import line
  (SANDLOOM-T009).

## Install from the checkout

From `examples/academy/`, run `uv sync`. The editable sources in `pyproject.toml`
resolve the unpublished framework from `../../packages/`; keep this directory
inside the checkout. See the [shared example setup](../README.md#running-them)
for isolated test services and development environment configuration.

## Tests

```bash
uv run sandloom test --full       # every gate; module tests + the slow split-conformance case
```

* Tests sign in with `as_user()` and call a provider through
  `Contract(PAYMENTS_CHARGES_V1)` exactly as module code does.
* `academy/modules/enrollment/tests/test_saga_enroll.py` — the saga against
  the REAL co-loaded providers through `saga_harness` (happy path, short
  balance, a crash after the charge committed, the two one-liners).
* `tests/test_split_conformance.py` — the same script against a monolith and
  against `enrollment | payments+notifications` (own databases, signed
  internal RPC): both must reach the same terminal state.
* `tests/test_modules_boot_alone.py` — enrollment boots alone only with its
  providers bound (`register_saga` added them to its consumes).
* `tests/test_contract_snapshots.py` — the providers' committed snapshots
  (checked by the split enrollment service at startup) match the code.

## Run it

```bash
cp .env.example .env         # adjust DATABASE_URL / CACHE_URL
uv run sandloom migrate upgrade-all --apply  # SANDLOOM_COMPOSITION_ROOT=academy.asgi (.env)
uv run uvicorn academy.asgi:app --reload
```

The advance consumer and the timer sweep run in the app process, in dev
exactly as in prod. See [sagas](../../docs/modules/between/sagas.md) in the framework repository.
