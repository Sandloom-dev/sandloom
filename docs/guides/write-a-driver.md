---
title: Write a driver
description: Connect a new backend to a Sandloom capability - implement the provider contract, register it through an entry point, select it with INFRA and prove it is used.
type: how-to
---

# Write a driver

**Goal:** a package that adds a backend to an existing
[capability](../concepts/capabilities-and-infrastructure.md), selected by
configuration, with no change to business modules or to the kernel.

**Use it** for a `notification` backend that Sandloom does not ship, such as an
email API, or for a [capability of your own](#a-capability-of-your-own).

**Do not use it** for business logic: that is a [module](add-module.md). Do not
use it to replace the `database` or `eventbus` backends either. The unit of work,
the outbox and the delivery guarantees depend on them, and their contracts are
not public ([why](../concepts/capabilities-and-infrastructure.md#the-relational-paradigm)).

!!! warning "Experimental"
    [`sandloom.driver`](../reference/driver-api.md) arrives in the release after
    `0.7.0a4`, and it may change in any release. Pin your driver to one minor range of
    `sandloom`.

## Prerequisites

- An application created with `sandloom new`.
- A separate package for the driver, outside `<app>/modules`. A driver is not a
  module: `sandloom check` does not apply to it, and it may import the backend's SDK
  and `sandloom.driver`, which a module may not
  ([SANDLOOM-C004](../reference/error-codes.md#sandloom-c004)).

## Steps

### 1. Implement the provider contract

`sandloom.driver` publishes a contract only together with a test kit that checks a
driver against it. Today that is `NotifierProvider` for `notification`, whose one
method is `send`.

The contracts of `cache`, `objectstore` and `taskqueue` are still private, in
`sandloom.core.infra.contracts`. You can implement them, but nothing checks your
driver, and they can change without notice.

This complete driver appends notifications to a file:

```python title="sandloom_notify_file/__init__.py"
--8<-- "docs/snippets/drivers/file_notifier.py"
```

- **The factory** is what you pass to `register_driver`. It receives one
  `InstanceConfig` (`capability`, `name`, `driver`, `options`) and returns the
  provider. A class whose constructor takes that config is a factory. It is
  called lazily, on first use: open no connection at import.
- **`options`** holds the keys of the `INFRA` entry and nothing else: replacing a
  driver leaves the previous driver's options, such as the `SMTP_*` values, behind.
  Options can include secrets, so never log them.
- **`Notification`** carries `body_fields`, the values its `body` was built from,
  for a driver that renders its own template. They are as sensitive as the body:
  never log them.
- **`features()`** names what this driver really does. It must be a subset of
  the capability's features. A module that requires `features={"email"}` is
  refused at startup on a driver that does not advertise it.
- **`capability_version`** states the contract version the driver was written for.
  A driver for another major version is refused when it registers.
- **`aopen`, `health` and `aclose`** are optional. Override them to open a pool
  at startup, report health in `/diagnostics/infra` and release resources.

### 2. Register it through an entry point

The `sandloom.infra` entry-point group is how Sandloom finds third-party
drivers. The entry point names a callable `register(registry)`:

```toml title="pyproject.toml"
[project]
name = "sandloom-notify-file"
version = "0.1.0"
dependencies = ["sandloom>=0.7.0a4,<0.8"]

[project.entry-points."sandloom.infra"]
notify-file = "sandloom_notify_file:register"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

Add the package to the application's dependencies. For a package next to the
application:

```bash
uv add --editable ../sandloom-notify-file
```

### 3. Select it with `INFRA`

The flat settings (`NOTIFIER_BACKEND`, `STORAGE_TYPE`, ...) accept only the
drivers Sandloom ships. A plugin driver is selected through
[`INFRA`](../concepts/capabilities-and-infrastructure.md#configuration-not-code-picks-backends),
in `.env` for the application and in `.env.test` for its tests. Everything other
than `driver` becomes an option:

```bash
INFRA={"notification":{"default":{"driver":"file","path":"/tmp/myapp-mail.log"}}}
```

Module code does not change: `await notifier().send(...)` now reaches your
driver. To keep the default and add a second instance, use another name
(`"audit"` instead of `"default"`) and resolve it with `notifier("audit")`.

### 4. Prove it is used

A test passing does not show the driver was selected: the default `log` driver
also accepts every `send`. Assert on the driver and on its effect. With `INFRA`
in `.env.test`, a module test can check both:

```python
from pathlib import Path

from sandloom.sdk.infra import Notification, notifier


async def test_the_file_driver_delivers(client) -> None:
    provider = notifier()
    assert provider.driver == "file"

    await provider.send(
        Notification(channel="email", recipient="a@b.test", subject="Hi", body="x")
    )

    assert "email|a@b.test|Hi" in Path("/tmp/myapp-mail.log").read_text()
```

### 5. Check it against the contract

`sandloom.driver.testing` holds the conformance kit. It sends on every channel the
driver advertises and fails on a feature the capability does not define, on a body or
`body_fields` value reaching the logs, or on `aclose` failing when called twice:

```python
from sandloom.driver import InfraRegistry, InstanceConfig
from sandloom.driver.testing import assert_notifier_conformance

from sandloom_notify_file import FileNotifier


async def test_the_driver_conforms(tmp_path) -> None:
    config = InstanceConfig(
        capability="notification",
        name="default",
        driver="file",
        options={"path": str(tmp_path / "mail.log")},
    )
    await assert_notifier_conformance(FileNotifier(config))
```

## Verify

```bash
uv run sandloom test
```

On a running application, `/diagnostics/infra` lists the registered drivers and
the driver of each instance. It needs `INTERNAL_API_TOKEN`
([Operator commands](../operations/operator-reference.md)).

## A capability of your own

A backend with no matching capability needs a contract of its own. A capability is
declared in the same `register` callable, before its drivers:

```python
from sandloom.driver import CapabilitySpec, InfraProvider, InfraRegistry


class VectorIndexProvider(InfraProvider):
    capability = "vector_index"

    async def nearest(self, vector: list[float], limit: int) -> list[str]:
        raise NotImplementedError


def register(registry: InfraRegistry) -> None:
    registry.register_capability(
        CapabilitySpec(
            name="vector_index",
            version="v1",
            provider_contract=VectorIndexProvider,
            features=frozenset({"filters"}),
            stability="experimental",
        )
    )
    registry.register_driver(
        "vector_index", "memory", MemoryVectorIndex, features={"filters"}
    )
```

- **Selecting it:** `INFRA={"vector_index":{"default":{"driver":"memory"}}}`.
- **Features:** declare them in `register_driver` too. Startup checks that list
  without building the driver, so a feature missing there fails a module's requirement.
- **Requiring it from a module:**
  `CapabilityRequirement("vector_index", features=frozenset({"filters"}))` in
  `define_module`. Startup then fails, without I/O, if it is missing.
- **Calling it from a module:** `capability("vector_index")` from
  `sandloom.sdk.infra`.
- **Name conflicts:** registering a name with another contract or version is refused.
  The kernel already defines `search`, `document_store`, `graph_store`, `workflow`,
  `lock` and `secrets`, contract only, with no driver. Their contracts are not public
  yet.

## Errors you may hit

| Error | Cause and fix |
| --- | --- |
| `Configured infrastructure instances have no registered driver` at startup | `INFRA` names a driver nobody registered. The package is not installed, or its entry point failed: a broken entry point is only logged, so look for `failed loading driver plugin` in the startup log. A loaded one logs `loaded driver plugin`. |
| `ProviderContractError` | The factory returned something that does not subclass the capability's contract. |
| `FeatureUnsupportedError` | A module requires a feature the driver does not advertise, or the driver declares a feature the capability does not define. |
| `Input should be 'log' or 'smtp'` | A plugin driver was set in `NOTIFIER_BACKEND`. Use `INFRA` (step 3). |

## Related

- [Capabilities and infrastructure](../concepts/capabilities-and-infrastructure.md): capabilities, drivers and instances.
- [`sandloom.driver`](../reference/driver-api.md): every name it exports.
- [Customize a standard module](customize-standard-module.md): what a notification driver can do for identity's mail.
