---
title: Add your own capability
description: Publish a new kind of infrastructure as a package - its contract, a driver, how a module requires and calls it.
type: concept
---

# Add your own capability

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

- **What a module may import:** `vector_index.contracts`. Importing `vector_index.driver`
  or its parent `vector_index`, which reaches the driver by attribute, fails
  [SANDLOOM-C004](../reference/error-codes.md#sandloom-c004), because an entry point names
  that module.
- **In production:** `stability="experimental"` requires the accepted risk
  `experimental_capability` ([SANDLOOM-B023](../reference/error-codes.md#sandloom-b023)).
  Use `"stable"` when you keep the contract stable yourself.
- **Reserved names:** the kernel defines `search`, `document_store`, `graph_store`,
  `workflow`, `lock` and `secrets`, so a package cannot declare them.

See also [Write a driver](../guides/write-a-driver.md) and the
[driver kit](../reference/driverkit.md).
