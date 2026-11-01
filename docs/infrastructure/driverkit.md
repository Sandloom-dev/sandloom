---
title: sandloom.driverkit
description: The API for driver packages - the notification contract, capability specs and references, and the conformance kit. Business modules never import it.
---

# `sandloom.driverkit`

The API a [driver package](write-a-driver.md) implements. Most code should use
[`sandloom.sdk.infra`](../reference/sdk/infra.md) instead: a business module may not import this
package ([SANDLOOM-C004](../reference/error-codes.md#sandloom-c004)), and the kernel never imports
it. It ships inside `sandloom`.

A contract is public only with a conformance kit, so `cache`, `objectstore` and
`taskqueue` are not here yet. A breaking change is a new contract version
([versioning](../contributing/versioning.md)).

## `sandloom.driverkit`

| Name | What it is |
| --- | --- |
| `NotifierProvider` | The contract of the `notification` capability: implement `async send(notification)`. |
| `Notification` | One outbound message: `channel`, `recipient`, `subject`, `body`, `data`, `dedupe_key`, `body_fields`. |
| `InfraProvider` | The base of every contract. A driver sets `contract_version`, `supported_features` and optionally `options_schema`; it may override `features_for(options)`, `aopen()`, `health()` and `aclose()`. |
| `HealthResult` | What `health()` returns. |
| `CapabilitySpec` | Declares a capability: `name`, `version`, `provider_contract`, `config_schema`, `features`, `description`, `stability`. |
| `CapabilityRef` | A typed key callers resolve a capability with: `CapabilityRef[VectorIndex]("vector_index.v1")`. |

## `sandloom.driverkit.testing`

| Name | What it is |
| --- | --- |
| `NotifierConformance` | A pytest class with one test per obligation. Subclass it and override the `make_provider` fixture, and `recipients` for your test backend. It needs `pytest` and `pytest-asyncio`. |

## Entry points

| Group | Name | Value |
| --- | --- | --- |
| `sandloom.drivers` | `<capability>.<driver>` | `package.module:DriverClass` |
| `sandloom.capabilities` | `<capability>` | `package.module:SPEC` |
