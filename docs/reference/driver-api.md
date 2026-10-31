---
title: sandloom.driver
description: The experimental API for driver packages - the notification contract, the registry, capability specs and the conformance kit. Business modules never import it.
---

# `sandloom.driver`

The API a [driver package](../guides/write-a-driver.md) implements and registers. Most
code should use [`sandloom.sdk.infra`](sdk/infra.md) instead: a business module may not
import this package ([SANDLOOM-C004](error-codes.md#sandloom-c004)), and the kernel never
imports it. It ships inside `sandloom`.

**Experimental:** it may change in any release. A contract joins it only with a
conformance kit.

## `sandloom.driver`

| Name | What it is |
| --- | --- |
| `NotifierProvider` | The contract of the `notification` capability: implement `async send(notification)`. |
| `Notification` | One outbound message: `channel`, `recipient`, `subject`, `body`, `data`, `dedupe_key`, `body_fields`. |
| `InfraProvider` | The base of every contract: `features()`, `aopen()`, `health()`, `aclose()`. Subclass it for a capability of your own. |
| `HealthResult` | What `health()` returns. |
| `InstanceConfig` | What a driver factory receives: `capability`, `name`, `driver`, `options`. |
| `InfraRegistry` | What `register(registry)` receives: `register_driver(capability, driver, factory, *, features=None, config_schema=None, instance_features=None, capability_version=None)` and `register_capability(spec)`. |
| `CapabilitySpec` | Declares a capability: `name`, `version`, `provider_contract`, `config_schema`, `features`, `description`, `stability`. |

## `sandloom.driver.testing`

| Name | What it is |
| --- | --- |
| `assert_notifier_conformance(provider, *, recipient="driver-test@example.com")` | Async. Raises `AssertionError` on the first way `provider` breaks the `notification` contract. |
