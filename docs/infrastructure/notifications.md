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

Sends email through an SMTP server. Set `NOTIFIER_BACKEND=smtp` and the `SMTP_*` settings, from
the [configuration reference](../reference/configuration.md#notifications). It supports the
channel `email`.

To send through another service, [write a driver](write-a-driver.md).
