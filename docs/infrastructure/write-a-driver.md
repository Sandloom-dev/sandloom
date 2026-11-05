---
title: Write a driver
description: Connect a new backend to a Sandloom capability - implement the contract, publish it by name, select it with INFRA, and check it with the conformance kit.
type: how-to
---

# Write a driver

**Goal:** a package that adds a backend to a
[capability](index.md), selected by configuration,
with no change to business modules or to the kernel.

**Use it** for a `notification` backend that Sandloom does not ship, such as an email
API, or for a [capability of your own](own-capability.md).

**Do not use it** for business logic: that is a [module](../modules/create.md). A contract is
public only with a conformance kit, so the `cache`, `objectstore` and `taskqueue`
contracts are not available yet. The `database` and `eventbus` backends carry the
transaction and delivery guarantees and are not open to drivers
([why](database.md#the-relational-paradigm)).

!!! note "Version"
    This guide targets `sandloom==0.7.0a10`. <!-- sandloom-release -->
    [`sandloom.driverkit`](driverkit.md) is included in this release.

## Prerequisites

- An application created with `sandloom new`.
- A separate package for the driver, outside `<app>/modules`. A driver may import its
  backend's SDK and `sandloom.driverkit`; a module may not
  ([SANDLOOM-C004](../reference/error-codes.md#sandloom-c004)).

## Steps

### 1. Implement the contract

This complete driver is for development: it appends messages, including their bodies, to
a file.

```python title="sandloom_notify_file/driver.py"
--8<-- "docs/snippets/drivers/file_notifier.py"
```

- **`contract_version`** is the contract the driver was written for. It is required, and
  another major version is refused when the driver loads.
- **`supported_features`** names what the driver really does, and is its one declaration.
  A module that requires `features={"email"}` is refused at startup on a driver that does
  not advertise it. If a feature depends on an option, override the classmethod
  `features_for(options)`. Do not override `features()`: an instance that reports other
  features than it declared is refused.
- **`options`** holds the keys of the `INFRA` entry, and nothing from another driver.
  Options can hold secrets, so never log them. For validation before the driver is built,
  set `options_schema` to a pydantic model.
- **The constructor** is called on first use, or at startup if an instance is configured.
  Open no connection at import. To open a pool at startup, report health in
  `/diagnostics/infra` or release resources, override `aopen`, `health` and `aclose`.
- **`Notification.body_fields`** holds the values `body` was built from, for a driver that
  renders its own template. It is as sensitive as the body: never log it.

### 2. Publish it by name

The entry-point name is `<capability>.<driver>`, and its value is your class:

```toml title="pyproject.toml"
[project]
name = "sandloom-notify-file"
version = "0.1.0"
dependencies = ["sandloom>=0.7.0a10,<0.8"]

[project.entry-points."sandloom.drivers"]
"notification.file" = "sandloom_notify_file.driver:FileNotifier"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

Add the package to the application's dependencies. For a package next to the application:

```bash
uv add --editable ../sandloom-notify-file
```

Sandloom imports the class only when `INFRA` selects its name. A package nothing selects
runs no code, and the order of installed packages does not matter.

### 3. Select it with `INFRA`

Put this in `.env` for the application and in `.env.test` for its tests. Everything other
than `driver` becomes an option:

```bash
INFRA={"notification":{"default":{"driver":"file","path":"/tmp/myapp-mail.log"}}}
```

Module code does not change: `await notifier().send(...)` now reaches your driver. To keep
the default and add a second instance, use another name (`"audit"` instead of `"default"`)
and resolve it with `notifier("audit")`.

### 4. Check it against the contract

`sandloom.driverkit.testing` holds the conformance kit. Subclass it in the driver's tests;
each obligation is its own test, so a failure names what to fix. The kit needs `pytest` and
`pytest-asyncio`, and it sends real messages, so point it at a test backend.

```python title="tests/test_conformance.py"
--8<-- "docs/snippets/drivers/test_file_notifier.py"
```

A skipped test means the driver advertises no channel, so nothing was sent. Read the skip
reason rather than treating it as a pass.

A database driver for a new SQL database also ships a **SQL dialect renderer**. It
writes the portable forms of `sandloom.sdk.data` in that database's SQL
([Database](database.md#portable-sql-forms)).

1. Implement `SqlDialect` from `sandloom.driverkit`, with `contract_version = 2` and the
   SQLAlchemy dialect `name`.
2. Give the oldest server it supports in `minimum_server_version`.
3. Return the features the database can give from `features()`.
4. Map the database's errors to kinds in `classify_error`.
5. Return a `RowSecurity` from `row_security()` when the database can confine rows
   to the tenant a transaction binds (the setting `TENANT_SETTING`), or `None`. It
   writes the policies and binds the tenant; its queries also check the roles at startup. With
   `None`, `tenant_rls` is refused
   ([SANDLOOM-B026](../reference/error-codes.md#sandloom-b026)) and tenant isolation
   there is application-enforced.
6. Say how a unit begins: `begin_statement(write=, read_only=, snapshot=)` returns
   the `BEGIN` text, or `None` for the plain one, and `transaction_options(read_only=,
   snapshot=)` the SQLAlchemy execution options to the same effect. Declare
   `snapshot_reads` and `readonly_transactions` only when they give one snapshot and
   refuse writes. Their units then need options of their own. Without
   `snapshot_reads` a one-snapshot scope is refused
   ([SANDLOOM-B027](../reference/error-codes.md#sandloom-b027)).
7. A database whose writers are serial may declare `transaction_locks` with no
   lock statement. Its driver marks a transaction that holds the write lock:
   `WRITE_LOCK_HELD` in the connection's `info`. SQLite's `BEGIN IMMEDIATE`
   does so. The kernel's locks then succeed only in such a transaction.
8. Two renderer facts are optional, and both default to `False`. If the database runs no
   migration chain, set `schema_from_models = True`. The app
   then builds its schema with `create_all` at startup, and `sandloom migrate` refuses it
   ([SANDLOOM-B028](../reference/error-codes.md#sandloom-b028)). If it serves only
   development and tests, set `development_only = True`; production then refuses it
   without an accepted risk
   ([SANDLOOM-B029](../reference/error-codes.md#sandloom-b029)).
9. Write a feature form only when the database has it, and declare its feature
   then ([Database](database.md#forms-that-need-a-feature)):
   - `arrays`: `array_type(item_type)`;
   - `fulltext_search`: `fulltext_index_options()`, `fulltext_document(columns,
     language)` and `fulltext_match(document, query, language)`. Write the
     document's constants inline, so a query's expression is the index's;
   - `json_path`: `json_path(column, path)`, the scalar as text;
   - `json_contains`: `json_contains(column, value)`.

   A renderer that declares
   one of these features without its members is refused when it loads
   ([SANDLOOM-B025](../reference/error-codes.md#sandloom-b025)). Without the
   feature, leave the members out: the form refuses
   ([SANDLOOM-R015](../reference/error-codes.md#sandloom-r015)).
10. Name the renderer in the `sandloom.sql_dialects` entry-point group under the dialect
    name, and register the driver with `sql_dialects={"<name>"}`.

Check it with `SqlDialectConformance` from the same kit: set `renderer` and `dialect`. A
form the database cannot write returns `None`, and the kit checks that it is refused.
The kit compiles each feature form the renderer declares. Run the forms on the real
database in your own tests too.

#### Add a DBAPI adapter

A new DBAPI for a database Sandloom already renders needs no renderer. It works
on the plain path as it is. Add a DBAPI adapter only if it needs the speed
([Database](database.md#the-dbapi-adapter)):

1. Subclass `DbapiAdapter` from `sandloom.driverkit`. Set `name` to SQLAlchemy's
   `dialect.driver` for the DBAPI. Keep `contract_version = 1`: another major is
   refused ([SANDLOOM-B025](../reference/error-codes.md#sandloom-b025)).
2. Set `capabilities`. A driver package may claim only `read_only_policy` and
   `reconnect_backoff`. The other three drive asyncpg's API and SQLAlchemy's
   private state, so only the kernel's own adapters claim them (B025).
3. Override what those capabilities use. `connect_args` and `server_settings`
   carry the timeouts, and `connection_lost` and `refuses_connections` classify
   connection errors.
4. Override the pool's probes if the DBAPI has cheaper ones. `ping` checks an
   idle pooled connection. `connection_closed` spots a dead one without a round
   trip.
5. Override `prepare_engine` for setup on each new engine. The aiosqlite adapter
   uses it to emit every unit's `BEGIN` itself.
   `config_problem(url)` refuses a database the DBAPI cannot open, before
   anything starts. Name the error code in its message.
6. Call `register_dbapi_adapter(adapter)` when the driver package loads, before
   the first engine is built. Your adapter replaces the one Sandloom ships for
   the same DBAPI, and the shipped one never replaces yours.

Every member you do not override keeps the plain answer. The plain
`connection_lost` asks the renderer, so a lost connection is a 503 on any
DBAPI that reports the server's error code. A DBAPI with no
`read_only_policy` on a database without `readonly_transactions` refuses
`DB_READ_ONLY_SESSIONS`
([SANDLOOM-B031](../reference/error-codes.md#sandloom-b031)).

### 5. Prove the application uses it

A passing application test does not show the driver was selected: the default `log` driver
also accepts every `send`. Assert on the effect. With `INFRA` in `.env.test`, a module test
can check it:

```python
from pathlib import Path

from sandloom.sdk.infra import Notification, notifier  # sandloom: advanced infra


async def test_mail_goes_through_the_file_driver(client) -> None:
    await notifier().send(
        Notification(channel="email", recipient="a@b.test", subject="Hi", body="x")
    )
    assert "a@b.test" in Path("/tmp/myapp-mail.log").read_text()
```

## Verify

```bash
uv run sandloom test
```

On a running application, `/diagnostics/infra` lists the installed driver packages and the
driver of each instance. It needs `INTERNAL_API_TOKEN`
([Operator reference](../operate/operator-reference.md)).

## Errors you may hit

| Error | Cause and fix |
| --- | --- |
| [SANDLOOM-B021](../reference/error-codes.md#sandloom-b021) | The selected name has two providers, or a package published a built-in's name. Uninstall one, or rename your driver. |
| [SANDLOOM-B022](../reference/error-codes.md#sandloom-b022) | The selected driver failed to import, is not a subclass of the contract, has no `contract_version`, or was written for another major version. The message names which. |
| `Configured infrastructure instances have no registered driver` | `INFRA` names a driver no installed package publishes. |
| `ProviderContractError` | An instance reports other features than its class declares. |
| `FeatureUnsupportedError` | A module requires a feature the driver does not advertise. |
| [SANDLOOM-B033](../reference/error-codes.md#sandloom-b033) | A module uses a `sandloom.sdk.data` form, or declares a database feature, that the instance's renderer does not declare. |
| `Input should be 'log' or 'smtp'` | A driver package's name was set in `NOTIFIER_BACKEND`. Use `INFRA`. |

## Related

- [Infrastructure](index.md): interfaces, drivers and instances.
- [Add your own capability](own-capability.md): a new kind of infrastructure.
- [`sandloom.driverkit`](driverkit.md): every name it exports.
- [Customize a standard module](../modules/batteries/customize.md): what a notification driver can do for identity's mail.
- [ADR-0011](../adr/0011-driver-api.md): why it works this way.
