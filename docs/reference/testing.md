---
title: Testing reference
description: Copy-paste reference of the sandloom.sdk.testing kit, its pytest fixtures and the harness rules.
type: reference
---

# Testing reference (copy-paste)

Everything a module's tests need, without reading the framework. Module
tests import only public `sandloom.sdk` modules (the kit is `sandloom.sdk.testing`),
generated contract stubs and DTOs, and their own package (`sandloom check` rule
`test-imports`, [SANDLOOM-C013](error-codes.md#sandloom-c013)).

- Every name and exact signature: [`sandloom.sdk.testing`](sdk/testing.md), the
  fixtures of [`sandloom.sdk.testing.plugin`](sdk/testing/plugin.md).
- Running the suites, your own database and Redis, the isolation guards:
  [Testing modules](../modules/testing.md).

## The kit at a glance

The six beginner names. Everything else in `sandloom.sdk.testing` is advanced.

| Name | What it does |
| --- | --- |
| `client` (fixture) | an httpx client on the app; every request runs on the test's `db` session, rolled back after the test |
| `as_user(user_id=None, tenant_id=None, *, email="", email_verified=True, tenant=True, **claims)` | `(user_id, headers)` with a freshly minted access token |
| `published(E)` | the `E` events the code under test published with `E(...).publish()`, in order |
| `await deliver(event, payload=None, *, times=1, event_id=None, session=None, core=None)` | runs every consumer of the event's channel through the real consumer runtime; returns how many consumers ran |
| `fake_contract(REF, impl)` | serves contract `REF` from `impl` for a `with` block |
| `fake_access_fact(name, value=True, *, deny_category=None, core=None)` | answers an access fact (an entitlement) without loading its owner |

## Activate the plugin

`sandloom new` writes this into the application's `pyproject.toml`. Modules need no
`conftest.py`.

```toml
[tool.pytest.ini_options]
addopts = ["-p", "sandloom.sdk.testing.plugin"]
```

The plugin loads `.env.test` before anything imports the framework, provides
every fixture below, records `published(...)` events and resolves
`Contract(...)` through the test application. Activate this plugin or the
framework's own harness plugin, not both. A module inside the framework tree
uses a one-line conftest instead: `from sandloom.sdk.testing.fixtures import *`.

## Running the tests

`sandloom test` is the one command (from the repository root or the application root):

```bash
uv run sandloom test <module>    # iterate: the module's tests, the fast suite in parallel, the static gates
uv run sandloom test --full      # once, at the end: the full suite in parallel + gates
uv run sandloom test --plan      # print the steps; --no-gates: tests only; -n N: xdist workers
```

- **What it runs.** Environment provisioning, boot checks, the static checks
  (ruff, format, mypy, `sandloom check`, `lint-imports`), the generated files
  (contract stubs, DTO copies, event schemas, snapshots: only checked; a
  stale one fails with "generated files are stale: run `sandloom
  generate`"), the tests, and with `--full` the typing tests. A failing step names its code: `uv run sandloom explain <code>`.
- **Random order.** Every test step runs in a seeded random order (the
  files, then the tests inside each file); the seed is printed. Replay it
  with `sandloom test --seed <seed>` or `pytest --sandloom-shuffle=<seed>` (the plugin
  `-p sandloom.core.testing.shuffle`); `sandloom test --no-shuffle` keeps file order.
  A test must pass alone and in any order:
  [Test isolation](../modules/testing.md#test-isolation).
- **Never re-run a serial `pytest -q` after each fix.** The parallel run is
  several times faster.
- **Each run gets its own test database** once `uv run sandloom dev up` wrote
  `.env.test.local`. A second run in the same checkout uses `<name>_2` and its
  own Redis indexes ([test databases](../modules/testing.md#in-an-app-sandloom-dev-up)).
- **Use your own test resources** on a shared machine without `sandloom dev`:
  export `SANDLOOM_TEST_OWNER`, `SANDLOOM_TEST_DATABASE_URL`,
  `SANDLOOM_TEST_CACHE_URL` and `SANDLOOM_TEST_BROKER_URL`.

## Which fixture

| You want to | Fixture | Semantics |
| --- | --- | --- |
| call your endpoints and read the database in the same test | `client` + `db` | every request runs on the test's single `db` session, rolled back after the test, whether the route uses `db()` or `DbSession`. A request that answers 4xx does **not** undo what it or earlier requests in the test wrote; `on_commit` callbacks run after a successful request only |
| assert that a failed request rolled back, or that something happened after commit (`on_commit` effects, delivered events) | `real_uow_client` | real request units on the test database: commit on success, rollback on error. Committed rows are emptied before the next test. Never combine it with `client` in one test |
| call a service directly (no HTTP) that publishes or audits | `db_uow` | `db` bound as the unit: `publish()`, `record_audit` and `db()` land in the test transaction |
| a unit test without a database | `recording_uow()` (helper) | records `.events` and `.audits` in memory |
| the cache (idempotency, rate limits, the subject record) | `sandloom_cache` | the `default` cache on the test Redis, flushed first |
| the app or its core | `sandloom_app`, `sandloom_core` | session-scoped |
| a saga run with faked contracts | `saga_harness` | see [Sagas](../modules/between/sagas.md) |

The plugin also provides `db_engine` (refuses a database whose name lacks
`test`), `ambient_uow`, `revoked_tokens`, `scratch_database`,
`make_headers` / `user_headers` (the fixture forms of `as_user`) and the
aliases `test_db`, `async_client`, `test_engine`.

## A signed-in user

```python
from uuid import uuid4

from sandloom.sdk.testing import as_user


async def test_member_creates(client) -> None:
    user_id, headers = as_user(tenant_id=uuid4())   # token: sub=user_id, tenant_id
    response = await client.post("/api/v1/notes/", json={"title": "x"}, headers=headers)
    assert response.status_code == 201


async def test_requires_auth(client) -> None:
    response = await client.post("/api/v1/notes/", json={"title": "x"})
    assert response.status_code == 401
```

- **The token.** `as_user` mints a real access token carrying `sub`,
  `token_type`, `tenant_id`, the signed allowlist `tenant_ids`, `email` and
  `email_verified` (plus `iat`, `nbf`, `exp`, `jti`). `user_id` and
  `tenant_id` default to random UUIDs; pass `None` for `email` or
  `email_verified` to omit it. A security-risk route needs a recent
  `auth_time`: pass it as a claim (`as_user(auth_time=int(time.time()))`).
- **Extra claims** are keywords: `as_user(roles=["admin"])`.
  `tenant=False` mints a token without a tenant.
- **Idempotency.** A write without an `Idempotency-Key` header runs normally.
  Send the header to test a retry: the automatic guard replays the status
  only (mode S, see [Route policy](../modules/http/route-policy.md)). A money
  write without the header answers `428`
  ([SANDLOOM-R010](error-codes.md#sandloom-r010)).
- **Membership.** A route that checks organization membership needs a real
  organization. Create it through tenancy's HTTP API, never its code:

```python
from uuid import UUID, uuid4


async def make_org(client, headers) -> UUID:
    response = await client.post(
        "/api/v1/orgs",
        json={"name": "Org", "slug": f"o-{uuid4().hex[:8]}"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])

# owner_id, h = as_user(); org_id = await make_org(client, h)
# _, scoped = as_user(owner_id, org_id)     # now a member-scoped token
```

## Reading your rows back (tenant-scoped models)

`TenantScoped` reads fail closed without a bound tenant. Bind it in the test:

```python
from sandloom.sdk.tenant import bind_tenant  # sandloom: advanced test reads a tenant row


async def test_row_is_stored(client, db) -> None:
    tenant = uuid4()
    _, headers = as_user(tenant_id=tenant)
    created = (await client.post("/api/v1/notes/", json={"title": "x"}, headers=headers)).json()
    with bind_tenant(tenant):
        row = await db.get(Note, UUID(created["id"]))
    assert row is not None
```

## Events you publish

```python
from sandloom.sdk.testing import published

from ..events import NoteCreated


async def test_create_publishes_once(client) -> None:
    _, headers = as_user(tenant_id=uuid4())
    await client.post("/api/v1/notes/", json={"title": "x"}, headers=headers)
    assert len(published(NoteCreated)) == 1
    assert published(NoteCreated)[0].title == "x"
```

- `published(E)` reads what the plugin records for the current test.
  `published("notes.note.created.v1")` lists every recorded event of a
  channel. Another class of the channel (a view) is decoded like a delivery.
- Under `client` the event also lands in `outbox_messages` in the test
  transaction, so the outbox row can be asserted too.
- Without the plugin, `published()` raises and says how to activate it.

## Events you consume

Test a consumer through the registered runtime, not by calling the handler:
a bare-handler test misses a consumer that is not wired or fails at runtime.

```python
from sandloom.sdk.testing import deliver

from ..consumers import NoteCreated   # this module's view of the channel


async def test_counted_once(client, db) -> None:
    tenant = uuid4()
    applied = await deliver(NoteCreated(note_id=uuid4(), tenant_id=tenant), times=2, session=db)
    assert applied == 1               # one consumer of the channel ran
    ...                               # assert one row: the redelivery was claimed once
```

- **What it does.** `deliver` validates the payload against the channel's
  schema, as a real publish does, then runs every consumer registered for the
  channel with the real unit, the durable inbox claim and the tenant binding.
  The envelope id is the same across `times`, so `times=2` is a redelivery.
- **Channel form.** `deliver("notes.note.created.v1", {"note_id": ..., "tenant_id": ...})`
  takes a payload; an `Event` instance takes none (`TypeError` otherwise).
- **Foreign channels.** When the producer module is not loaded, the view's
  schema stands in for the producer's (test only).
- **Errors.** `LookupError` when nothing consumes the channel.
- **`session=db`** runs the consumer on the test's session, so `client`
  requests in the same test see its rows.

For a full chain through the running app (publish, relay, consumer), use
`real_uow_client` inside `async with sandloom_app.router.lifespan_context(sandloom_app):`
and poll for the effect.

## Asserting an email was sent

Events carry no PII: read the address at send time from identity's
`identity.contact.v1` (`get_contact(user_id)` returns a `UserContact` with
`email`, `full_name`, `email_verified`, `is_active`), and send through
`notifier()` after commit. In tests the default notifier is the `log` driver:
it logs one `notification.sent` record per send, with the subject and a hash
of the recipient (never the address).

```python
from sandloom.sdk.testing import deliver, fake_contract
from sandloom.shared.contracts.identity_contact_v1 import IDENTITY_CONTACT_V1
from sandloom.shared.contracts.identity_dto import UserContact


class Contacts:
    async def get_contact(self, user_id):
        return UserContact(
            user_id=user_id, email="ada@example.com", full_name="Ada",
            email_verified=True, is_active=True,
        )


async def test_welcome_is_sent_once(client, caplog) -> None:
    with fake_contract(IDENTITY_CONTACT_V1, Contacts()), caplog.at_level(
        "INFO", logger="sandloom.core.notification"
    ):
        await deliver(UserRegistered(user_id=uuid4()), times=2)
    sent = [r for r in caplog.records if r.getMessage() == "notification.sent"]
    assert [r.subject for r in sent] == ["Welcome"]
```

## Calling another module's contract

Replace the provider with a test double for a block:

```python
from sandloom.sdk.testing import fake_contract
from myapp.shared.contracts.stats_quota_v1 import STATS_QUOTA_V1


class Deny:
    async def may_create_note(self) -> bool:
        return False


async def test_quota_refuses(client) -> None:
    _, headers = as_user(tenant_id=uuid4())
    with fake_contract(STATS_QUOTA_V1, Deny()):
        response = await client.post("/api/v1/notes/", json={"title": "x"}, headers=headers)
    assert response.status_code == 402
```

- `fake_contract` takes the generated `ContractRef` or the contract name. It
  nests and restores the previous resolution.
- Without a fake, `Contract(REF)` resolves through the test application:
  a provider loaded in the app runs on the test session.
- **Seed the provider's state** through its HTTP API, never its code.
- `fake_contract` also serves the contract to kernel code that resolves it
  through the application (the billing and membership authorization atoms,
  access facts), so faking `billing.entitlements.v1` reaches
  `SubscriptionActive` / `Credits`.
- **Faking an entitlement** without loading billing:
  `with fake_access_fact("organization.subscription.active", True): ...`.
  Pass a callable for a per-subject answer and `deny_category=` for a
  specific denial. The two subscription facts also drive the live atoms
  that ask the same question, so every form of an organization paid gate is
  faked the same way:

```python
from sandloom.sdk.testing import fake_access_fact

# Any of these gates on the route:
#   require(OrgRole("admin") & SubscriptionActive(of=Account.org(param="org_id")))
#   require(Subscribed(of=Account.org(param="org_id")))
#   require(Entitled("organization.subscription.active"))


async def test_export_needs_a_paid_organization(client, org_admin) -> None:
    org_id, headers = org_admin
    url = f"/api/v1/orgs/{org_id}/export"
    with fake_access_fact("organization.subscription.active", False):
        assert (await client.get(url, headers=headers)).status_code == 402
    with fake_access_fact("organization.subscription.active", True):
        assert (await client.get(url, headers=headers)).status_code == 200
```

  `"subscription.active"` does the same for a personal gate
  (`of=Account.user()`).

## Testing a contract you provide

```python
from sandloom.sdk.testing import ContractCase, assert_contract_conformance  # sandloom: advanced conformance


async def test_contract_local_equals_rpc(sandloom_core) -> None:
    org_id = ...   # seed through your HTTP API first
    assert_contract_conformance(sandloom_core, "quotas.usage.v1", [
        ContractCase("get_remaining", (org_id, "invitations"), tenant=org_id),
        ContractCase("consume", (org_id, "invitations", 1, "op-1"), tenant=org_id),
    ])
```

`assert_contract_conformance` checks that the local call and the in-process
RPC call answer the same, errors and decoded types included. `sandloom generate`
regenerates the contract stub, the DTO copies and the snapshot; an
incompatible change fails before anything is written ([SANDLOOM-T004](error-codes.md#sandloom-t004),
[SANDLOOM-T007](error-codes.md#sandloom-t007)).

## Route policy, boot and registration

Every route is authenticated unless it declares `public(reason=...)`
([Route policy](../modules/http/route-policy.md)). The resolved policy of every
route is recorded in the module's snapshot and checked by `sandloom test`
([SANDLOOM-T001](error-codes.md#sandloom-t001)).

```python
from sandloom.sdk.testing import assert_route_snapshot, boot_module_alone, route_policy  # sandloom: advanced wiring


def test_routes_match_the_snapshot(sandloom_app) -> None:
    assert_route_snapshot(sandloom_app)


def test_export_is_authenticated(sandloom_app) -> None:
    route = next(r for r in sandloom_app.routes if getattr(r, "path", "") == "/api/v1/notes/export")
    assert route_policy(route).auth != "public"


async def test_boots_alone(db_engine) -> None:
    outcome = await boot_module_alone("myapp.modules.notes")
    assert outcome.started or outcome.missing_contracts == {"stats.quota.v1"}
```

## Migrations match models

On SQLite the test database takes its schema from the models and runs no
migration chain. Production on PostgreSQL takes it from the chain. One test
per module keeps the two equal: the chain at head must build the schema the
models declare. `sandloom add module` writes it as `tests/test_migrations.py`:

```python
from sandloom.sdk.testing import assert_migrations_match_models  # sandloom: advanced gate
def test_migrations_match_models() -> None:
    assert_migrations_match_models("notes")
```

The test makes a scratch PostgreSQL database next to the test database. It
runs the core chain there, then the chains the module depends on, then the
module's own chain to head. It compares the result with the models. The comparison is
Alembic's autogenerate with the options of `sandloom migrate new`: types and
server defaults, over the tables the chain owns. Indexes are compared as the
database reflects them, against the same indexes created from the models in a
second scratch database. That covers access method, operator classes, sort
order, included columns, `NULLS NOT DISTINCT` and a partial index's
condition. It fails with one line per difference and the fix:

```text
the notes migration chain at head differs from its models (SANDLOOM-T006):
  column notes_items.priority (INTEGER): in the models, not created by the chain
fix: sandloom migrate new notes "<message>" (or correct the model), then review the revision
```

On SQLite it skips with the reason, and for a module with no `migrations/`
directory. `migration_drift("notes")` returns the lines instead of failing.
It does not compare what models cannot declare: RLS policies, extensions,
triggers. CHECK constraints and an index's collation are not compared either,
since neither is reflected.

## Tests that need a database feature

A module's tests run on PostgreSQL and on a SQLite file
(`SANDLOOM_TEST_DATABASE_URL=sqlite+aiosqlite:///<dir>/<name>_test.db`; each
xdist worker gets `<name>_test_gw<N>.db`,
[SQLite for tests](../infrastructure/database.md#module-tests-on-sqlite)).
Mark a test that needs a database feature the other one lacks. It is skipped
there, and the skip names the missing feature and the instance:

```python
from sandloom.sdk.testing import skip_unless_db_feature  # sandloom: advanced database features
@skip_unless_db_feature("arrays")
async def test_tags_are_stored_as_an_array(client) -> None: ...
```

A test that races two writers needs `concurrent_writers`: write
transactions that overlap, each holding its own row locks. SQLite has one writer,
and the harness does not emulate PostgreSQL's concurrency: the race runs on
PostgreSQL only. Bound every wait between the racers (`asyncio.wait_for(...,
30)`). A racer that never arrives then fails the test instead of hanging it.
A test still running after 300 s dumps every thread's stack
(`faulthandler_timeout` in the generated `pyproject.toml`).

On SQLite the units a test's code opens nest in the `db` session once it
wrote. That holds for the test's task and the tasks it starts. A task started
in a fresh context (the in-memory bus delivering an event to a consumer, the
post-commit dispatcher) does not nest: its unit waits for the `db` session's
write lock like any other writer, so a consumer that writes runs after the
test's transaction ends, or times out while the test still holds it. Their
deferred effects (outbox dispatch, `on_commit`) run when the
host's unit ends. The `client` fixture's request end runs them. `db_uow`,
`bind_test_uow` and a bare `await db.commit()` do not: assert those effects
with `real_uow_client` or `run_on_commit`. An app that star imports
the fixtures instead of loading the plugin registers the marker in its
`conftest.py`: `config.addinivalue_line("markers", "sandloom_db_feature: ...")`
in `pytest_configure`.

The features are the `database` capability's
([Forms that need a feature](../infrastructure/database.md#forms-that-need-a-feature)): `concurrent_writers`, `skip_locked`,
`transaction_locks`, `snapshot_reads`, `readonly_transactions`, `arrays`,
`fulltext_search`, `json_path`, `json_contains`. The harness reads them as
the application advertises them for the instance (`database="default"`; pass
another name for another instance). Put the mark on a function, a class, or
`pytestmark`.

## The skip ledger

CI runs the tests on two databases (PostgreSQL, then SQLite). A test may skip
on one; a test that skips on both runs nowhere. Each lane loads
`-p sandloom.core.testing.skip_ledger` and writes the tests it ran and the
tests it skipped, with the reasons, to a JSON file:

```bash
uv run pytest -p sandloom.core.testing.skip_ledger \
  --sandloom-skip-ledger=skip-ledger/sqlite.json --sandloom-skip-ledger-lane=sqlite
python3 tools/ci/skip_ledger.py skip-ledger/*.json \
  --allowlist tools/ci/skip_ledger_allowlist.json --require-lanes postgresql,sqlite
```

The second command fails on a test that was skipped on some lane and ran on
none, printing each lane's reason. A module skipped while it was collected
(`importorskip`) is recorded by its path and counts as run when one of its
tests ran on another lane. Make it run on one lane, or add it to the
allowlist with the reason CI cannot run it. An allowlist entry whose test ran
fails too: the list shrinks with the tests it excuses. With
`--strict-allowlist` (full validation), so does an entry no lane collected.
`SANDLOOM_SKIP_LEDGER` (and `_LANE`, `_BASE`) stand for the options.
`PYTEST_ADDOPTS="-p sandloom.core.testing.skip_ledger"
SANDLOOM_SKIP_LEDGER=skips.json uv run sandloom test` writes one too.

## Harness rules

These are the still-true rules of the test harness. Each one is enforced by
the harness or a check unless marked.

- **Imports.** Module tests import only `sandloom.sdk` public modules, generated
  stubs and DTOs, and their own package ([SANDLOOM-C013](error-codes.md#sandloom-c013)).
  Seed another module's state through its HTTP API, contracts or events. An
  allowlist entry for a test file needs a written justification; a stale one
  fails the suite.
- **Run isolation.** A test database or Redis index carries an owner marker
  (`SANDLOOM_TEST_OWNER`, else the machine id and the checkout root). A run refuses another owner's
  resources before any test (exit 4); `SANDLOOM_TEST_TAKE_OWNERSHIP=1` takes over.
  Flush a test Redis only with `flush_owned_redis`. Never `pkill` by pattern.
- **Pools.** Test processes get small pools (at most 13 connections) and the
  session fails fast when the server lacks room (`-n auto` is capped;
  `SANDLOOM_TEST_SKIP_CONNECTION_CHECK=1` skips the check). The harness restores
  the active core, infrastructure, bus and module lists after every test.
- **Server errors re-raise.** An endpoint that raises an unhandled exception
  re-raises it in the test ([SANDLOOM-R006](error-codes.md#sandloom-r006));
  `@pytest.mark.sandloom_allow_server_errors` keeps the `500` response.
- **Deprecations are errors.** Apps set
  `filterwarnings = ["error::DeprecationWarning"]`.
- **Gate on `settings.is_prod`, never on `is_test`.**
- **Contracts on the test session.** Under `client` the test session is bound
  for contract units automatically.
- **The in-memory bus** delivers asynchronously: `await bus.drain()` before
  asserting on consumer side effects in a bus-level test.
- **Users are minted as tokens** (`as_user`); never create identity rows from
  another module's tests.
- **Savepoints.** After a service call that may roll back a savepoint
  (`db().begin_nested()`), read `entity.id` before the call: a rollback
  expires ORM instances.
- **Webhooks.** Sign test payloads with `sandloom_billing.psp.sign_stripe_payload`
  (the default PSP outside production rejects unsigned webhooks).
- **Never pass a session to another task** in a test either: a child task
  opens its own unit with `open_uow()` ([Units](../modules/database/units.md)).
