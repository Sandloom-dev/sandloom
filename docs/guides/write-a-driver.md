---
title: Write a driver
description: Connect a new backend to a Sandloom capability - implement the contract, publish it by name, select it with INFRA, and check it with the conformance kit.
type: how-to
---

# Write a driver

**Goal:** a package that adds a backend to a
[capability](../concepts/capabilities-and-infrastructure.md), selected by configuration,
with no change to business modules or to the kernel.

**Use it** for a `notification` backend that Sandloom does not ship, such as an email
API, or for a [capability of your own](#a-capability-of-your-own).

**Do not use it** for business logic: that is a [module](add-module.md). A contract is
public only with a conformance kit, so the `cache`, `objectstore` and `taskqueue`
contracts are not available yet. The `database` and `eventbus` backends carry the
transaction and delivery guarantees and are not open to drivers
([why](../concepts/capabilities-and-infrastructure.md#the-relational-paradigm)).

!!! note "Version"
    [`sandloom.driverkit`](../reference/driverkit.md) arrives in the release after
    `0.7.0a4`. Depend on that release once it is published.

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
dependencies = ["sandloom>=0.7.0a4,<0.8"]

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
([Operator commands](../operations/operator-reference.md)).

## A capability of your own

A backend with no matching capability needs a contract of its own. Split the package in
two, so a module can import the contract without the driver:

```python title="vector_index/contracts.py"
--8<-- "docs/snippets/drivers/vector_index_contracts.py"
```

```python title="vector_index/driver.py"
--8<-- "docs/snippets/drivers/vector_index_driver.py"
```

```toml title="pyproject.toml"
[project.entry-points."sandloom.capabilities"]
vector_index = "vector_index.contracts:VECTOR_INDEX_SPEC"

[project.entry-points."sandloom.drivers"]
"vector_index.memory" = "vector_index.driver:MemoryVectorIndex"
```

- **Selecting it:** `INFRA={"vector_index":{"default":{"driver":"memory"}}}`.
- **Requiring it from a module:**
  `CapabilityRequirement("vector_index", features=frozenset({"exact"}))` in
  `define_module`. Startup then fails, without I/O, if it is missing.
- **Calling it from a module:** the reference carries the type, as long as your package
  ships a `py.typed` file. Without one, type checkers see `Any`.

    ```python
    from sandloom.sdk.infra import capability  # sandloom: advanced infra
    from vector_index.contracts import VECTOR_INDEX

    nearest = await capability(VECTOR_INDEX).nearest([1.0], limit=5)
    ```

- **What a module may import:** `vector_index.contracts` and nothing else from that
  package. Importing `vector_index.driver`, or `vector_index` itself, fails
  [SANDLOOM-C004](../reference/error-codes.md#sandloom-c004), because an entry point names
  a driver in it.
- **In production:** `stability="experimental"` requires the accepted risk
  `experimental_capability` ([SANDLOOM-B023](../reference/error-codes.md#sandloom-b023)).
  Use `"stable"` when you keep the contract stable yourself.
- **Reserved names:** the kernel defines `search`, `document_store`, `graph_store`,
  `workflow`, `lock` and `secrets`, so a package cannot declare them.

## Errors you may hit

| Error | Cause and fix |
| --- | --- |
| [SANDLOOM-B021](../reference/error-codes.md#sandloom-b021) | The selected name has two providers, or a package published a built-in's name. Uninstall one, or rename your driver. |
| [SANDLOOM-B022](../reference/error-codes.md#sandloom-b022) | The selected driver failed to import, is not a subclass of the contract, has no `contract_version`, or was written for another major version. The message names which. |
| `Configured infrastructure instances have no registered driver` | `INFRA` names a driver no installed package publishes. |
| `ProviderContractError` | An instance reports other features than its class declares. |
| `FeatureUnsupportedError` | A module requires a feature the driver does not advertise. |
| `Input should be 'log' or 'smtp'` | A driver package's name was set in `NOTIFIER_BACKEND`. Use `INFRA`. |

## Related

- [Capabilities and infrastructure](../concepts/capabilities-and-infrastructure.md): capabilities, drivers and instances.
- [`sandloom.driverkit`](../reference/driverkit.md): every name it exports.
- [Customize a standard module](customize-standard-module.md): what a notification driver can do for identity's mail.
- [ADR-0011](../adr/0011-driver-api.md): why it works this way.
