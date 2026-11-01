---
title: Add your module
description: Give a module its own console screens with an admin.py declaration. No React and no frontend build.
---

# Add your module

A module gets its screens from a declaration, not from frontend code. The console
draws lists, details, forms and commands from it.

## Declare a resource

Create `admin.py` inside the module. This one is for the module `sandloom new --module
notes` generates, whose API lists, shows and creates items:

```python
RESOURCES = [
    {
        "name": "notes",
        "label": "Notes",
        "access": {"authenticated": True},
        "list": {"path": "/api/v1/notes/items", "method": "GET"},
        "pagination": {"mode": "offset", "offset": "skip", "limit": "limit", "max_page_size": 50},
        "columns": ["title", "owner_id"],
        "detail": {"path": "/api/v1/notes/items/{item_id}", "method": "GET"},
        "show": ["id", "title", "owner_id"],
        "create": {"path": "/api/v1/notes/items", "method": "POST", "fields": ["title"]},
        "labels": {"id": "ID", "title": "Title", "owner_id": "Owner"},
        "scope": {"parameter": None, "label": "Organization"},
    }
]
```

The console pages through a list, so the API has to accept an offset. The generated list
takes only `limit`: add `skip: Annotated[int, Query(ge=0)] = 0` to the route and pass it to
the query as `.offset(skip)`.

Here `scope` means a required, explicitly selected tenant UUID. The top bar
provides its selector; requests send `X-Tenant-Id`. The API must use `TenantScope`
and authorize the caller; the selector does not authenticate membership. If the
API also puts that tenant in the path, set `parameter` to its actual path parameter
name. Without a selected scope the resource makes no requests. Changing scope
cancels reads and replaces the query cache, cursor chains and UI store. Unscoped
resources such as identity administration omit `scope` entirely.

## Generate the manifest

```bash
uv run sandloom generate
```

This checks the declaration against your application's real API and writes
`<app>/shared/admin-manifest.json`. A field or route that doesn't exist fails here,
not in the browser. Restart the application and reload the page: the section is there.

Commit the file. `sandloom test` fails when it is out of date
([SANDLOOM-T016](../reference/error-codes.md#sandloom-t016)).

Generation imports your application's code but doesn't enter its lifespan, run
migrations or query a database. Run it with the full set of modules, even if you
deploy them as separate services.

## Keep declarations and APIs together

Change `admin.py` and the API it describes in the same commit. If you need more than a
declaration can express, see [Develop the console](develop.md).

| Failure | Check |
| --- | --- |
| An operation is not owned by a module | Bind the module's own `/api/v1/` endpoint. Don't point a declaration at another module's API. |
| An unsupported field or schema | Use a supported scalar input, or a compiled component. |
