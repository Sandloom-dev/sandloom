# The first administrator

Status: design, 2026-11-03 (tm: `dev-admin-design`). Built on [research.md](research.md) and the
owner's decision of 2026-11-03: no password is ever written to a file, because such a file travels
(backups, rsync deploys, a careless image build) and ends up in production.

## Commands

### `sandloom admin create --email <e> [--name <n>]`

Creates a platform administrator in one identity unit: the user (email verified), the platform
`admin` role, an audit record (`identity.user.admin_created`, actor `cli:<os user>`, reason). The
password is set one of two ways; there is no `--password VALUE` flag.

- **At a terminal** (stdin and stdout are TTYs): a hidden prompt, typed twice, checked against the
  identity password policy, re-asked on failure.
- **`--invite`** (agents, CI, scripts, production): the account is created without a usable
  password (`password_reset_required`), and the command prints one single-use link:

  ```text
  Administrator you@example.com created. Set the password within 15 minutes:
    http://127.0.0.1:8000/admin/set-password#token=…
  The link works once. A new one: uv run sandloom admin invite --email you@example.com
  ```

  The token is identity's existing password-reset token (nonce-bound, so issuing a new link kills
  the old one, and using it clears the nonce), with a 15-minute lifetime for this purpose. Only the
  nonce is stored. The token travels in the URL fragment, so it does not reach server or proxy logs.
- **`--password-stdin`**: reads one line from stdin (a secret manager pipe). Refused when stdin is
  a TTY (use the prompt).

Without a TTY and without `--invite` or `--password-stdin` the command fails and names both.

Re-runs: an existing email fails with "already exists; to make it an administrator:
`sandloom security grant-admin`; a new password: `sandloom admin invite`". `--if-missing` turns
that into exit 0 without changes (for an agent that runs setup twice).

### `sandloom admin invite --email <e>`

A new set-password link for an existing administrator (forgotten password, expired link). Ends the
user's sessions, rotates the nonce, audited. Same output as `create --invite`.

### Kept as they are

`sandloom security grant-admin` (promote an existing account), `sandloom admin export`.

## Where the link points

A new console page, `/admin/set-password`, reads the token from the fragment, asks for the new
password twice, calls identity's `POST /auth/reset-password`, then signs in and opens the console.
An expired or used token shows "this link has expired; ask for a new one:" and the `admin invite`
command. The base URL is the `PUBLIC_BASE_URL` setting, defaulting to `http://127.0.0.1:8000`
outside production; in production a missing `PUBLIC_BASE_URL` makes `--invite` fail rather than
print a wrong host.

## Production

- `create` and `invite` need `--apply --confirm-production --reason "<why>"` when the profile is
  production, like every other operator command; outside production they act directly.
- In production only `--invite` and `--password-stdin` exist; the prompt works too when an operator
  is at a terminal on the host.
- Nothing is written to disk. The link, if pasted into a chat or a ticket, is worth nothing after
  first use or 15 minutes.

## Split deployments

The command calls identity through a new `identity.security.v1` command method,
`create_platform_admin(email, full_name, password: str | None, reason, operator, operation_key) ->
AdminCreated(user_id, reset_token | None)`. With `password=None` identity generates the reset
token; the password from the prompt crosses RPC only inside the existing authenticated, encrypted
channel and is never logged. Contract snapshot and stub are regenerated.

## Discoverability

- `sandloom new --with admin` prints, after the migrate step: `uv run sandloom admin create --email
  you@example.com` (and `--invite` for agents).
- The generated README shows the command inline instead of linking to the website.
- At startup, when the admin module is loaded and no platform administrator exists, the app logs
  one warning with the command (once per process).
- Outside production, the console sign-in page shows the same command while no administrator
  exists. This needs an unauthenticated `GET /admin/api/setup-state` returning `{"has_admin":
  false}`; the route is disabled in production (404).

## Dev-only email links

The `log` notifier logs only the subject and a recipient hash, so in dev without SMTP a user who
registered through the API can never verify the email. Outside production the notifier also logs
the links an email carries (verification, reset), marked `[dev only]`. In production it never does.

## Edge cases

| Case | Behaviour |
| --- | --- |
| Two agents run `admin create` for one email at once | one creates it; the other gets "already exists" (unique email, one unit); with `--if-missing` both exit 0 |
| Two links issued for one admin | the nonce rotates: only the newest works |
| Link used twice | second use: "expired or used" page |
| Link leaks into a chat after use | worthless |
| Link leaks before use | valid for at most 15 minutes, for that account only; `admin invite` again revokes it |
| Agent sets up the app for the owner | `admin create --email <owner> --invite --if-missing`, then hands the link to the owner; the agent never knows the password |
| No identity module (`--with admin` always adds it) | `admin create` fails: "the identity module is not installed" |
| Database unreachable | one-line error from the CLI handler of `sandloom dev` (SANDLOOM-E001) |
| Password policy rejects the prompt | re-asked with the reason |
| Email already registered, not admin | refused with the `grant-admin` pointer |
| Production without `PUBLIC_BASE_URL` | `--invite` refused, naming the setting |
| Account blocked | `invite` refuses until it is unblocked (the console's emergency block stands) |
| CI quickstart runs `QUICKSTART` verbatim | the quickstart step uses `--invite --if-missing`, non-interactive |

## Out of scope

Env bootstrap (`SANDLOOM_FIRST_ADMIN_*`), 2FA enrolment at first sign-in, a web first-run form,
organisation setup for tenancy apps.
