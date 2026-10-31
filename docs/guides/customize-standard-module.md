---
title: Customize a standard module
description: Change how identity, tenancy or billing behave without forking - configuration, a module next to them, a notification driver - and what is not supported yet.
type: how-to
---

# Customize a standard module

**Goal:** make a [standard module](../modules/index.md) fit your product while
you keep receiving its upgrades.

Standard modules are installed packages. Your code talks to them the way any
module talks to another: through contracts and events. Take the first option on
this page that does the job.

| You want to | Use |
| --- | --- |
| Change limits, keys, sign-in providers or other behaviour with a setting | [Configuration](#1-configuration) |
| Store more data about a user or an organization | [A module next to it](#2-a-module-next-to-it) |
| React when a user registers or is deleted | [A consumer](#2-a-module-next-to-it) |
| Add values to the access token | [A claims contributor](#add-values-to-the-access-token) |
| Deliver or restyle its email | [A notification driver](#3-a-notification-driver) |

## 1. Configuration

The settings of each module are in its page ([identity](../modules/identity.md#capabilities-configuration),
[tenancy](../modules/tenancy.md#capabilities-configuration),
[billing](../modules/billing.md#capabilities-configuration)) and in the
[configuration reference](../reference/configuration.md).

## 2. A module next to it

Add fields by adding a module, not by editing identity. This `profiles` module
stores a display name for the current user, keyed by the `user_id` from the
token, and removes it when identity deletes the user.

```bash
uv run sandloom add module profiles
uv run sandloom add consumer profiles identity.user_deleted.v1
```

Delete the generated `models/item.py` and add this model. A profile belongs to a
user, not to a tenant, so it inherits `Base` alone. Edit the generated baseline
migration to create this table, without the row-level-security calls
([Add a module](add-module.md#3-replace-the-example-with-your-domain)):

```python title="profiles/models/profile.py"
--8<-- "docs/snippets/customize/profile_model.py"
```

Replace the generated `schemas.py` and `services.py`, delete the generated
`events.py` (this module publishes nothing), and give it routes for the current
user:

```python title="profiles/schemas.py"
--8<-- "docs/snippets/customize/profile_schemas.py"
```

```python title="profiles/services.py"
--8<-- "docs/snippets/customize/profile_services.py"
```

```python title="profiles/api.py"
--8<-- "docs/snippets/customize/profile_api.py"
```

Replace the generated consumer. The event carries the id in its `payload`, with
no personal data, and the view declares only the field this module reads:

```python title="profiles/consumers.py"
--8<-- "docs/snippets/customize/profile_consumer.py"
```

Replace the generated test. Pass `session=db` to `deliver` so the consumer runs
in the test's transaction and sees the row the route wrote:

```python title="profiles/tests/test_profiles.py"
--8<-- "docs/snippets/customize/profile_test.py"
```

Run `uv run sandloom generate`, then `uv run sandloom test profiles`.

Identity's contact details (`identity.contact.v1`) are the source for email and
name at send time: call the contract instead of copying them
([how other modules use identity](../modules/identity.md#how-other-modules-use-identity)).

### Add values to the access token

A module can add claims to the tokens identity mints, with
`add_claims_contributor` in its `setup` ([`ModuleAPI`](../reference/sdk/module.md)).
Keep them small and non-secret: a token outlives the data it was built from.

## 3. A notification driver

Identity sends all mail through the `notification` capability, so a
[driver](write-a-driver.md) decides how it is delivered. Each message carries its
type in `notification.data["kind"]`: `email_verification`, `password_reset`,
`registration_attempt` or `security_notice`.

A driver can use `kind` to pick a sender or wrap the text in your layout. There is
no template API: the subject and the plain-text body come from identity, and the
secret link appears only inside the body. To reword a message, your driver must
find the link in that text, which can break when identity changes its wording.
Pin the identity version if you do this, and cover it with a test.

## What is not supported yet

- **Changing a standard module's own tables, routes or message text.** There are
  no extension points for these yet.
- **Importing a standard module's code**, or patching it at runtime. An import
  fails [SANDLOOM-C001](../reference/error-codes.md#sandloom-c001). Internals may
  change in any release.
- **A local copy with the same name next to the package.** The loader refuses it
  ([SANDLOOM-B020](../reference/error-codes.md#sandloom-b020)).
- **Ejecting a module** into your application with a command. It does not exist
  yet.

If you need one of these, [open an issue](../getting-started/help.md) with your
use case.

## Related

- [Standard modules](../modules/index.md)
- [Events](../concepts/events.md) and [contracts](../concepts/contracts.md)
- [Write a driver](write-a-driver.md)
