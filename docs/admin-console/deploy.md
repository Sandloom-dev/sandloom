---
title: Deploy the console
description: Turn the admin console on in production, restrict who can reach it, run it in a split deployment, upgrade it and replace it with your own build.
---

# Deploy the console

## Turn it on

The console is off when `ENVIRONMENT=prod`: every `/admin/` path answers `404`. Turn it
on deliberately:

```bash
export ADMIN_CONSOLE_ENABLED=true
docker compose -f deploy/docker-compose.yml up --build
```

The manifest ships inside your application's image, so the file you committed is what
runs. Create the administrator as in [Create the first administrator](index.md#create-the-first-administrator),
with `--confirm-production` added to the command that has `--apply`.

## Restrict access

The console's files are public: they hold no data, and every API they call checks the
caller. Blocking the page alone protects little, because the APIs stay reachable. At your
proxy, restrict both paths to your operators' network or VPN:

| Path | What it is |
| --- | --- |
| `/admin/` | the console's files |
| `/api/v1/platform/` | the platform views: audit, health, event repair |

For more separation, serve the console on its own hostname, so nothing else shares its
origin. The page already refuses framing and code from other origins, and access tokens
stay in memory, never in cookies.

## Separate services

The console needs no service of its own. Put it next to identity:

```bash
uv run sandloom add deploy admin --service identity
```

This adds it to that service and generates the gateway route to `/admin/`. The console's
API calls go through the same gateway, which routes each one to the service that owns
it, so nothing in the console knows your topology. Each service reports on itself: choose
the source at the top of the platform views.

Use `--service admin` if you would rather give it a process of its own. Set
`ADMIN_CONSOLE_ENABLED` in the shared environment, as with any setting that must match.

Generate the manifest in an environment that loads every module, then deploy it in the
image of each service. See [Split into services](../operate/split-into-services.md).

## Upgrade

`sandloom-admin` and the standard modules follow the same release train as `sandloom`.
Upgrade them together, then run `uv run sandloom generate`. If they drift apart, the page
names the mismatch, for example a component this console doesn't include, rather than
showing a broken screen.

## Use your own build

Point the application at any directory holding a built console:

```bash
export ADMIN_CONSOLE_DIST=/srv/my-console
```

Build it from a fork of [`admin/`](https://github.com/Sandloom-dev/sandloom/tree/main/admin)
with `SANDLOOM_ADMIN_BASE=/admin/ npm run build`, which needs Node. From then on the
upgrades of that copy are yours. See [Develop the console](develop.md).
