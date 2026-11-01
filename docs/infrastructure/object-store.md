---
title: Object store
description: The key-addressed file interface and its local driver.
type: concept
---

# Object store

An object store keeps files by a logical key, never by a host path.

## What a module calls

```python
from sandloom.sdk.infra import objectstore

key = await objectstore().put("reports/1.pdf", data, content_type="application/pdf")
```

`objectstore(name="default")` returns `put(key, data, content_type=...)`, `get`, `delete` and `url`.

## Drivers

### `local`

The filesystem. Set `STORAGE_TYPE=local` and `STORAGE_PATH`, or the options `path` and
`max_upload_size_bytes` on a named instance.

**Limits.** It works on a single node or a shared persistent volume, and production needs the
accepted risk `local_storage_in_prod`. `put` returns a key; there is no public `url`, CDN or
presigned URL.

Choose an instance in [Configure backends](configure.md).
