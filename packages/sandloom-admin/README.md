# sandloom-admin

**The admin console for Sandloom.**

A web console for the platform owner: users, organizations, billing, audit and
service health. It ships ready to run inside this package, so you need neither Node
nor a separate image.

Part of [Sandloom](https://github.com/Sandloom-dev/sandloom), the Python backend
framework for AI-driven development.

## Included

- The built console, served by your application at `/admin/`.
- Screens from the `admin.py` declarations of your modules, with no frontend build.
- Off in production unless you set `ADMIN_CONSOLE_ENABLED=true`.

## Install

```bash
uvx --from "sandloom[standard]==0.7.0a6" sandloom new myapp --with admin
```

Or add it to an existing application:

```bash
uv add "sandloom[admin]"
```

Add `"admin"` to `BUNDLED` in your `asgi.py`, then run `uv run sandloom generate`.

See the [Admin Console documentation](https://sandloom.dev/docs/admin-console/).
