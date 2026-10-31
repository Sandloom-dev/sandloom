---
title: Write a driver
description: Connect a new backend to a Sandloom capability - implement the provider contract, register it through an entry point, select it with INFRA and prove it is used.
type: how-to
---

# Write a driver

**Goal:** a package that adds a backend to an existing
[capability](../concepts/capabilities-and-infrastructure.md), selected by
configuration, with no change to business modules or to the kernel.

**Use it** for a `notification`, `cache`, `objectstore` or `taskqueue` backend
that Sandloom does not ship, such as an email API or S3.

**Do not use it** for business logic: that is a [module](add-module.md). Do not
use it to replace the `database` or `eventbus` backends either. The unit of work,
the outbox and the delivery guarantees depend on them, and their contracts are
not part of `sandloom.sdk.drivers` ([why](../concepts/capabilities-and-infrastructure.md#the-relational-paradigm)).

!!! note "Version"
    `sandloom.sdk.drivers` arrives in the release after `0.7.0a4`. On `0.7.0a4`
    itself, these names exist only under the private `sandloom.core.infra`.

## Prerequisites

- An application created with `sandloom new`.
- A separate package for the driver. A driver is not a module: `sandloom check`
  does not apply to it, and it may import the backend's SDK, which a module may
  not.

## Steps

### 1. Implement the provider contract

Each capability has one abstract provider class in `sandloom.sdk.drivers`:

| Capability | Contract | Methods to implement |
| --- | --- | --- |
| `notification` | `NotifierProvider` | `send` |
| `cache` | `CacheProvider` | see [the reference](../reference/sdk/drivers.md#cacheprovider) |
| `objectstore` | `ObjectStoreProvider` | see [the reference](../reference/sdk/drivers.md#objectstoreprovider) |
| `taskqueue` | `TaskQueueProvider` | see [the reference](../reference/sdk/drivers.md#taskqueueprovider) |

This complete driver appends notifications to a file:

```python title="sandloom_notify_file/__init__.py"
--8<-- "docs/snippets/drivers/file_notifier.py"
```

- **The factory** is what you pass to `register_driver`. It receives one
  `InstanceConfig` (`capability`, `name`, `driver`, `options`) and returns the
  provider. A class whose constructor takes that config is a factory. It is
  called lazily, on first use: open no connection at import.
- **`options`** holds the keys of the `INFRA` entry. When you override the
  `default` instance it also holds the options of the built-in default, such as
  the `SMTP_*` values for `notification`. Read the keys you need and ignore the
  rest. Those values can include secrets, so never log `options`.
- **`features()`** names what this driver really does. It must be a subset of
  the capability's features. A module that requires `features={"email"}` is
  refused at startup on a driver that does not advertise it.
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

To test the driver alone, build an `InfraRegistry` with an `InstanceConfig`, call
`register(registry)`, then `registry.provider("notification")`.

## Verify

```bash
uv run sandloom test
```

On a running application, `/diagnostics/infra` lists the registered drivers and
the driver of each instance. It needs `INTERNAL_API_TOKEN`
([Operator commands](../operations/operator-reference.md)).

## A capability of your own

A backend with no matching capability needs a contract of its own. Subclass
`InfraProvider` and register a `CapabilitySpec` before its drivers, in the same
`register` callable:

```python
registry.register_capability(
    CapabilitySpec(
        name="search",
        version="v1",
        provider_contract=SearchProvider,
        features=frozenset({"facets"}),
        stability="experimental",
    )
)
```

A module then reaches it with `capability("search", "default")` from
`sandloom.sdk.infra`.

## Errors you may hit

| Error | Cause and fix |
| --- | --- |
| `Configured infrastructure instances have no registered driver` at startup | `INFRA` names a driver nobody registered. The package is not installed, or its entry point failed: a broken entry point is only logged, so look for `failed loading driver plugin` in the startup log. A loaded one logs `loaded driver plugin`. |
| `ProviderContractError` | The factory returned something that does not subclass the capability's contract. |
| `FeatureUnsupportedError` | A module requires a feature the driver does not advertise, or the driver declares a feature the capability does not define. |
| `Input should be 'log' or 'smtp'` | A plugin driver was set in `NOTIFIER_BACKEND`. Use `INFRA` (step 3). |

## Related

- [Capabilities and infrastructure](../concepts/capabilities-and-infrastructure.md): capabilities, drivers and instances.
- [`sandloom.sdk.drivers`](../reference/sdk/drivers.md): every contract and its signatures.
- [Customize a standard module](customize-standard-module.md): what a notification driver can do for identity's mail.
