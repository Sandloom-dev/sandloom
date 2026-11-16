---
title: Notifications
description: The notification interface and its log and smtp drivers.
type: concept
---

# Notifications

Notifications send messages to people. Pair them with an [`@on` consumer](../modules/between/events.md)
to notify when something happens.

## What a module calls

```python
from sandloom.sdk.infra import Notification, notifier

await notifier().send(
    Notification(channel="email", recipient=..., subject=..., body=...)
)
```

A module that cannot work without email states it, and startup checks it:
`CapabilityRequirement("notification", features=frozenset({"email"}))`.

Choose the driver with `NOTIFIER_BACKEND`.

## Drivers

### `log`

The default. It only records that a message was sent, never the raw recipient or the body.

**Limits.** Logging is not delivery: in production it advertises no channel, so a module requiring
`email` fails at startup unless `SANDLOOM_ACCEPTED_RISKS` lists `log_notifier_in_prod`.

### `smtp`

Sends email through an SMTP server. Set `NOTIFIER_BACKEND=smtp` and write the server on the
`notification` instance in `INFRA`:

```sh
INFRA='{"notification":{"default":{"driver":"smtp","host":"mail.example.com","port":587,"from_addr":"no-reply@example.com","username":"app","password":"..."}}}'
```

The options are `host` (required), `port`, `username`, `password`, `from_addr` and `use_tls`.
STARTTLS (`use_tls`) is on unless you write `false`, and production refuses a login without it.
It supports the channel `email`.

Writing `use_tls: false` is a reported configuration override: every production start logs a WARNING, and `/diagnostics/core` and `sandloom doctor` list it.
The report reads the instance as the application resolves it. A `use_tls: false` set by the composition's default selection counts like one in `INFRA`.
Under `PRODUCTION_PROFILE=multi_tenant_strict` in production, `use_tls: false` on any notification instance refuses the start. No accepted risk admits it.

**Tier.** `experimental` until its conformance lane (`NotifierConformance` against a mailpit
container, `.github/workflows/notification-smtp.yml`) is green on main, then `beta`. In
production an experimental driver needs `experimental_driver` in `SANDLOOM_ACCEPTED_RISKS`
([SANDLOOM-B023](../reference/error-codes.md#sandloom-b023)).

To send through another service, [write a driver](write-a-driver.md).
