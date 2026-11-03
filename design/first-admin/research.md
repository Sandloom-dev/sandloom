# First administrator: how to create it and sign in to the console

Research note, 2026-11-03. The question: how should a developer who runs
`sandloom new myapp --with admin` for the first time create the first
administrator and sign in at `/admin/`? Below are the current state in this repo,
how 19 established tools handle it (each claim linked to a primary source), and a
recommendation. Claims marked **(unverified)** were not confirmed in a primary
source.

---

## 1. Recommendation for Sandloom

### 1.1 One command creates the first admin

Add **`sandloom admin create --email ADDRESS [--name NAME]`**. The `sandloom admin`
group already exists for `export`, and the console is where the account is used.
In one identity unit the command creates the user (hashed by identity, checked
against `password_policy_violation`), marks the email verified, assigns the
platform `admin` role, and writes the audit row
`identity.user.platform_admin_granted` (or a sibling `…platform_admin_created`).
It runs through the `identity.security.v1` contract like `grant-admin`, so it also
works when identity is a separate service.

How the password is supplied, in order of preference:

| mode | flag | for |
|---|---|---|
| interactive | *(default when stdin is a TTY)* | prompts for the password twice with no echo, re-prompts on a mismatch or a policy violation (as Django's `createsuperuser` does) |
| generated | `--generate-password` | agents, CI, a first try. Writes `email` + password to `.sandloom/first-admin.txt` (mode 0600, gitignored by `sandloom new`) and prints **the path, never the password** |
| stdin | `--password-stdin` | a password manager or secret store pipes it in (`op read … \| sandloom admin create …`) |
| none | `--login-link` | prints a single-use sign-in URL (15 min) that opens "set your password" in the console. No password exists until the human sets one |

- With no TTY and no mode flag, the command **fails** with the four options listed.
  It never creates a passwordless user silently, as Django `--noinput` does
  ([source](https://raw.githubusercontent.com/django/django/main/django/contrib/auth/management/commands/createsuperuser.py)),
  and never skips with exit code 0 as Django does without a TTY.
- No `--password VALUE` flag. A password in argv ends up in shell history and
  `ps`. Keycloak avoids this with `--password:env VAR`
  ([source](https://www.keycloak.org/server/bootstrap-admin-recovery)).
- **Re-runs:** if the email exists, exit 1 with
  "already registered: `sandloom security grant-admin --email …` makes it an admin;
  `sandloom admin reset-password` sets its password". With `--if-missing`, exit 0
  and change nothing (for scripts and agents that may run twice).
- **Dev vs prod:** this follows the existing operator convention
  (`src/sandloom/core/cli/_operator.py`). In production `--confirm-production` is
  required, and `--reason` is required (default `"first administrator"` when no
  admin exists yet). **Open question:** should `create` act without `--apply` in
  dev (it only adds data), or keep the dry-run default every `sandloom security`
  command has? A dry-run default adds one confusing step to the very first run.
  The recommendation is to act in dev and require
  `--apply --confirm-production` in prod.

Keep `sandloom security grant-admin` for promoting an account that already
exists. Add **`sandloom admin reset-password --email`** with the same password
modes. It ends the user's sessions (reuse `_end_sessions`) and writes an audit
row. This covers a forgotten admin password, which GitLab
([rake task](https://docs.gitlab.com/security/reset_user_password/)), Grafana
([`reset-admin-password`](https://grafana.com/docs/grafana/latest/cli/)), Strapi
([`admin:reset-user-password`](https://docs.strapi.io/cms/cli)) and Django
([`changepassword`](https://docs.djangoproject.com/en/5.2/ref/django-admin/)) all
provide. Optionally add **`sandloom admin list`**: the platform admins, with
email, verified, 2FA and last sign-in.

### 1.2 Optional bootstrap from the environment (containers)

`SANDLOOM_FIRST_ADMIN_EMAIL` plus `SANDLOOM_FIRST_ADMIN_PASSWORD_FILE` (a file
path, e.g. a Docker secret; plain `…_PASSWORD` only outside prod) is applied by
`sandloom migrate upgrade-all --apply` (the one-shot step, not every web worker).
It runs only **while zero platform admins exist**, under a lock, and is audited.
Afterwards it is ignored with one INFO line. This copies Directus `ADMIN_*`
([docs](https://directus.com/docs/configuration/general)), Keycloak
`KC_BOOTSTRAP_ADMIN_*` (only when the master realm is created,
[docs](https://www.keycloak.org/server/all-config)) and GitLab
`GITLAB_ROOT_PASSWORD` ("only work during the first installation",
[docs](https://docs.gitlab.com/install/package/ubuntu/)). Prefer `_FILE`: GitLab
recommends Docker secrets "without exposing it as an environment variable"
([docs](https://docs.gitlab.com/install/docker/installation/)), and Keycloak says
to "use a non-CLI configuration option for this option if possible"
([docs](https://www.keycloak.org/server/all-config)). Like the FastAPI template
([config.py](https://raw.githubusercontent.com/fastapi/full-stack-fastapi-template/master/backend/app/core/config.py)),
refuse a weak or placeholder password in prod. This can wait until after 1.1.

### 1.3 What the user sees

- **`sandloom new … --with admin` prints** one more step after
  `migrate upgrade-all` in `QUICKSTART`
  (`src/sandloom/core/cli/project.py:80`), conditional on `admin`:
  `uv run sandloom admin create --email you@example.com   # first console administrator, then open http://127.0.0.1:8000/admin/`.
  The generated README repeats it inline instead of linking to the website
  (`project.py:296` today). The quickstart CI job executes those lines verbatim,
  so it needs a non-interactive variant there (`--generate-password`).
- **At startup**, when `admin` is loaded and the platform has zero admins, log
  one WARNING (in every environment, since it goes to the operator's log):
  `No platform administrator yet. Create one: uv run sandloom admin create --email you@example.com`.
  Jupyter shows that printing the next step to the terminal works: its token
  "is logged to the terminal, so that you can copy/paste the URL"
  ([docs](https://jupyter-server.readthedocs.io/en/latest/operators/security.html)).
- **The console sign-in page**, outside prod and only when zero admins exist,
  shows a callout with the same command and a copy button. It needs an
  unauthenticated `GET /admin/api/setup-state → {"has_admin": bool}` that answers
  only outside prod (in prod the console is off by default anyway). With admins
  present the page shows nothing, and a failed sign-in keeps its generic error.
  The page also links "Forgot password?" to the identity reset flow, plus a
  "lost admin access: `sandloom admin reset-password`" hint.
- **No web first-run "create admin" form in v1.** See 3.2 for the race
  condition. If one is wanted later, gate it on *zero admins* **and** a secret
  one-time token printed at startup and written to `.sandloom/setup-token`. It
  must be secret: Metabase's setup token sits in its public settings response,
  see 2.

### 1.4 The path for an AI agent

An agent setting up the app runs
`uv run sandloom admin create --email <owner's email> --generate-password --if-missing`
and tells the owner: "your credentials are in `.sandloom/first-admin.txt`;
delete it after signing in." The agent never sees, echoes or commits the
password, nothing lands in shell history, and the re-run is a no-op. A
console-first owner can use `--login-link` instead. The URL passes through chat,
but it works once and expires in minutes. The startup banner and the CLI error
texts name these flags, so an agent can find them without docs. Mention the
command in the generated AGENTS.md/skill too.

### 1.5 Keep as is

- **Verification bypass.** The operator vouches for the address
  (`src/sandloom_identity/contracts.py:115`). This is needed: with the default
  `log` notifier the verification link is never visible (see 1.6), so a
  self-registered user cannot sign in at all in dev.
- **The admin is platform-global.** It adds no organization membership; creating
  it creates no tenant.
- **Audit row, operator name and reason** for every grant, create and reset.

### 1.6 Facts from the code that shape this

- `POST /auth/register` always answers `202` with the same body (no account
  enumeration), creates `is_verified=False`, and is rate-limited to 10 per hour per
  client (`src/sandloom_identity/api/auth.py:47-66`,
  `services/auth_service.py:300-345`).
- Login refuses an unverified email with 403
  (`services/auth_service.py:467`).
- The default `log` notifier logs only the subject and a recipient hash, **never
  the body**, so the verification link is unreachable in dev without SMTP
  (`src/sandloom/core/drivers/log_notifier.py:66-80`). Today's curl-then-grant
  sequence only works because `grant-admin` sets `is_verified`.
- Password policy: length 8–100 (schema), a built-in list of common passwords,
  and not equal to the email or its local part. There are no composition rules,
  per NIST 800-63B (`src/sandloom_identity/password_policy.py`). The docs example
  `Example-Passw0rd!` passes. Hashing happens only in identity
  (`AuthService.ahash_password`).
- `grant-admin` (`src/sandloom/core/cli/security.py:364-387`):
  - It needs a registered user and fails with "register them first (POST
    /auth/register)".
  - It is a dry-run unless `--apply`. `--apply` needs `--reason` (at least 3
    characters), and `--confirm-production` when `ENVIRONMENT=prod`
    (`_operator.py:44-61`).
  - Each run gets a fresh `operation_key`, so a re-grant after a revocation acts.
  - `_grant_admin` creates the `admin` role lazily (`is_system=True`), skips a
    duplicate `UserRole`, sets `is_verified` and `email_verified_at`, and records
    the audit row `identity.user.platform_admin_granted` (`actor_type="operator"`,
    data: operator, reason, operation_key). It is a `@sensitive_effect("admin")`
    (`contracts.py:95-128`).
- The console requires the platform `admin` role. Organization roles do not grant
  it. Tokens live in browser memory, and 2FA is optional at sign-in
  (`docs/admin-console/operations.md:9-21`).
- With `ENVIRONMENT=prod` the console is off and every `/admin/` path answers
  404 until `ADMIN_CONSOLE_ENABLED=true` (`docs/admin-console/deploy.md:10-20`).
  The docs tell prod users to add `--confirm-production`.
- The generated README only links to the website
  (`src/sandloom/core/cli/project.py:296-298`), and `sandloom new` prints no admin
  step, which is the gap the owner hit.
- A new `create` contract method would carry a plaintext password over internal
  RPC in a split deployment. The RPC is encrypted (see the key-rotation skill),
  but `--generate-password` could instead have identity generate the password and
  return it once.

---

## 2. How established tools do it

| tool | mechanism | guard / lifetime | non-interactive | source |
|---|---|---|---|---|
| Django | `createsuperuser` CLI | none; a duplicate username: re-prompt (interactive) or `CommandError` (`--noinput`) | `--noinput` + `DJANGO_SUPERUSER_PASSWORD`, `DJANGO_SUPERUSER_<FIELD>` | [docs](https://docs.djangoproject.com/en/5.2/ref/django-admin/), [source](https://raw.githubusercontent.com/django/django/main/django/contrib/auth/management/commands/createsuperuser.py) |
| Wagtail | Django's `createsuperuser`, then `/admin` | as Django | as Django | [tutorial](https://docs.wagtail.org/en/stable/getting_started/tutorial.html) |
| Rails 8 auth generator | `User.create!` in `rails console` | none | seeds / runner | [guide](https://guides.rubyonrails.org/getting_started.html) |
| ActiveAdmin | the generator plus a seed: default `admin@example.com` / `password` | none; the page has no warning to change it | `db:seed` | [install](https://activeadmin.info/docs/v3/0-installation.html) |
| Avo, Administrate | ship no auth; the app's own users are used | the app's concern | n/a | [Avo](https://docs.avohq.io/3.0/authentication.html), [Administrate](https://administrate-demo.herokuapp.com/authentication) |
| Laravel Filament | `php artisan make:filament-user` | locally every user can enter; outside local, `FilamentUser::canAccessPanel()` | flags (unverified in docs) | [install](https://filamentphp.com/docs/4.x/introduction/installation), [users](https://filamentphp.com/docs/4.x/users/overview) |
| Laravel Nova | `php artisan nova:user` | the `viewNova` gate, open in `local` | — | [install](https://nova.laravel.com/docs/v5/installation) |
| Strapi | first visit to the admin panel shows a registration form | server: transaction + lock on the super-admin role + "You cannot register a new super admin" once any admin exists | `strapi admin:create-user` (options or prompts) | [quick start](https://docs.strapi.io/cms/quick-start), [user.ts](https://raw.githubusercontent.com/strapi/strapi/develop/packages/core/admin/server/src/services/user.ts), [routes](https://raw.githubusercontent.com/strapi/strapi/develop/packages/core/admin/server/src/routes/authentication.ts), [CLI](https://docs.strapi.io/cms/cli) |
| Payload CMS | `/create-first-user` admin route | server throws `Forbidden` if any user exists | — | [admin docs](https://payloadcms.com/docs/admin/overview), [registerFirstUser.ts](https://raw.githubusercontent.com/payloadcms/payload/main/packages/payload/src/auth/operations/registerFirstUser.ts) |
| Payload `autoLogin` | dev auto-sign-in as a given email | "behind an Environment Variable… disabled in production" | — | [auth docs](https://payloadcms.com/docs/authentication/overview) |
| Directus | `ADMIN_EMAIL` / `ADMIN_PASSWORD` / `ADMIN_TOKEN` on bootstrap; otherwise the Studio onboarding screen | first boot only (community answer, see below) | env | [config](https://directus.com/docs/configuration/general), [bootstrap source 27.1.0](https://app.unpkg.com/@directus/api@27.1.0/files/dist/cli/commands/bootstrap/index.js) |
| Keycloak | `KC_BOOTSTRAP_ADMIN_USERNAME` / `_PASSWORD`, or `kc.sh bootstrap-admin user\|service` | only when the master realm is created; the account is flagged **temporary** in a console banner and in logs | `--password:env VAR`, `--no-prompt` | [recovery](https://www.keycloak.org/server/bootstrap-admin-recovery), [all-config](https://www.keycloak.org/server/all-config) |
| Grafana | default `admin` / `admin`, created on first start | change prompt shown only while the password is `admin`, **skippable** | `admin_password` set once on first run; `disable_initial_admin_creation` | [config](https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/), [sign-in](https://grafana.com/docs/grafana/latest/setup-grafana/sign-in-to-grafana/), [LoginCtrl.tsx](https://github.com/grafana/grafana/blob/main/public/app/core/components/Login/LoginCtrl.tsx) |
| GitLab | random root password in `/etc/gitlab/initial_root_password` | file removed on the first restart after 24 h | `GITLAB_ROOT_PASSWORD` (first install only), Docker secret | [package](https://docs.gitlab.com/install/package/ubuntu/), [docker](https://docs.gitlab.com/install/docker/installation/) |
| Ghost | `/ghost` prompts to create the site owner account | first visit (guard not checked) | — | [install](https://docs.ghost.org/install/ubuntu/) |
| Sentry self-hosted | `./install.sh` prompts during `upgrade` | — | `SKIP_USER_CREATION=1` or `CI`, then `docker compose run --rm web createuser` | [install script](https://raw.githubusercontent.com/getsentry/self-hosted/master/install/set-up-and-migrate-database.sh) |
| FastAPI full-stack template | `FIRST_SUPERUSER` / `FIRST_SUPERUSER_PASSWORD`, created in `init_db` if that email is missing | `"changethis"` warns in dev and is refused outside it | env | [db.py](https://raw.githubusercontent.com/fastapi/full-stack-fastapi-template/master/backend/app/core/db.py), [config.py](https://raw.githubusercontent.com/fastapi/full-stack-fastapi-template/master/backend/app/core/config.py) |
| Medusa | `npx medusa user -e … [-p …]` or `--invite` (prints an invite token, accepted at `/app/invite?token=`) | — | flags | [CLI](https://docs.medusajs.com/resources/medusa-cli/commands/user) |
| Metabase | setup wizard / `POST /api/setup` with a setup token | 403 once any user exists; token cleared after use | API | [source api.clj](https://github.com/metabase/metabase/blob/master/src/metabase/setup_rest/api.clj), [learn](https://www.metabase.com/learn/metabase-basics/administration/administration-and-operation/metabase-api) |
| Appsmith | `APPSMITH_ADMIN_EMAILS` names instance admins | signup restrictions do not apply to them | env | [docs](https://docs.appsmith.com/getting-started/setup/instance-configuration/disable-user-signup) |
| Supabase Studio | HTTP basic auth, `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` in `.env` | "A secure password MUST be set before starting"; the quick-start script generates one | env | [self-hosting](https://supabase.com/docs/guides/self-hosting/docker) |
| Jupyter Server | a random token logged to the terminal; the URL sets a cookie | per server start | `--IdentityProvider.token` (unverified) | [security](https://jupyter-server.readthedocs.io/en/latest/operators/security.html) |

### Notes per tool

- **Django.**
  - Interactive mode prompts for the password twice with `getpass` ("Password
    (again)"), refuses blank passwords, runs `validate_password`, and offers
    "Bypass password validation and create user anyway? [y/N]".
  - Without a TTY it prints "Superuser creation skipped due to not running in a
    TTY." and does not exit with an error, a silent failure in CI.
  - `--noinput` without `DJANGO_SUPERUSER_PASSWORD` creates an account with an
    unusable password, and the `--noinput` branch does not call
    `validate_password`.
  - Sources: [source](https://raw.githubusercontent.com/django/django/main/django/contrib/auth/management/commands/createsuperuser.py),
    [docs](https://docs.djangoproject.com/en/5.2/ref/django-admin/).
  - **Lesson:** good interactive UX, poor non-interactive guarantees.
- **Strapi.** The first-run form is guarded on the server, not just hidden in
  the UI. `createFirstAdmin` runs in a transaction, locks the super-admin role
  row, then counts all admin users
  ([user.ts](https://raw.githubusercontent.com/strapi/strapi/develop/packages/core/admin/server/src/services/user.ts)).
  The route is `auth: false` with only rate limiting
  ([routes](https://raw.githubusercontent.com/strapi/strapi/develop/packages/core/admin/server/src/routes/authentication.ts)),
  so whoever reaches a fresh deployment first becomes Super Admin.
- **Payload.** Same model, guarded by "any user exists, then `Forbidden`"
  ([source](https://raw.githubusercontent.com/payloadcms/payload/main/packages/payload/src/auth/operations/registerFirstUser.ts)).
  Its dev `autoLogin` with `prefillOnly` is a cheap dev convenience
  ([docs](https://payloadcms.com/docs/authentication/overview)).
- **Metabase.**
  - `POST /api/setup` refuses with 403 "can only be used to create the first
    user" once any user exists, and clears the setup token inside the same
    transaction
    ([api.clj](https://github.com/metabase/metabase/blob/master/src/metabase/setup_rest/api.clj)).
  - The code comment calls the token a value kept "in (public) Settings
    responses". A third-party guide says it is read from
    `GET /api/session/properties` (**secondary**,
    [guide](https://www.metabase.com/articles/set-up-metabase-from-code-with-the-api)).
    So the token stops cross-site and accidental calls, not a network attacker
    racing the owner.
- **Keycloak.** The bootstrap admin is explicitly *temporary*, labelled in the
  console and in logs, and admins are told to replace it with a permanent one.
  A separate `bootstrap-admin service` creates a service account for automation.
  The password comes from `--password:env`, not argv
  ([docs](https://www.keycloak.org/server/bootstrap-admin-recovery)). This is the
  most thought-through env bootstrap.
- **Grafana.** `admin` / `admin` by default. The change-password view appears
  only when the submitted password is `admin`, and `skipPasswordChange` lets the
  user continue without changing it
  ([LoginCtrl.tsx](https://github.com/grafana/grafana/blob/main/public/app/core/components/Login/LoginCtrl.tsx)).
  This is the classic default-credentials risk.
- **GitLab.** A random password, a root-only file, and automatic removal
  after 24 h. `GITLAB_ROOT_PASSWORD` needs at least 8 characters and works only
  on the first install. Recovery: `gitlab-rake "gitlab:password:reset[root]"`,
  which prompts for the password and a confirmation
  ([package](https://docs.gitlab.com/install/package/ubuntu/),
  [reset](https://docs.gitlab.com/security/reset_user_password/)). This is the
  closest precedent for `--generate-password` writing to a file.
- **Directus.** The env vars `ADMIN_EMAIL`, `ADMIN_PASSWORD` and `ADMIN_TOKEN` are
  official ([config](https://directus.com/docs/configuration/general)). Two
  behaviours come from the 27.1.0 bootstrap source and community answers, not
  from current docs:
  - A missing password is generated and **printed to the log**.
  - The variables apply on the first boot only.

  Sources: [unpkg 27.1.0](https://app.unpkg.com/@directus/api@27.1.0/files/dist/cli/commands/bootstrap/index.js),
  [community](https://community.directus.com/t/fresh-install-login-credentials-portainer-docker-setup/1007).
  **(partly unverified for current versions)**. Printing the password to logs
  leaks it to log aggregation.
- **Filament / Nova.** A CLI creates the user, and access is open in the `local`
  environment. Outside `local`, a code-level gate (`canAccessPanel`, `viewNova`)
  decides who enters
  ([Filament](https://filamentphp.com/docs/4.x/users/overview),
  [Nova](https://nova.laravel.com/docs/v5/installation)). Sandloom's
  console-off-in-prod plus a required role is stricter.
- **ActiveAdmin.** It documents signing in as `admin@example.com` / `password`
  after seeding, with no warning to change it
  ([install](https://activeadmin.info/docs/v3/0-installation.html)). This is an
  anti-pattern for anything reachable in production.
- **FastAPI full-stack template.** It is the closest stack analogue. The env
  superuser is created idempotently by email lookup on every `init_db`, and
  placeholder secrets are refused outside development
  ([db.py](https://raw.githubusercontent.com/fastapi/full-stack-fastapi-template/master/backend/app/core/db.py),
  [config.py](https://raw.githubusercontent.com/fastapi/full-stack-fastapi-template/master/backend/app/core/config.py)).
  Weakness: the password stays in `.env` for the app's lifetime.
- **Medusa `--invite`.** The CLI prints a token, and the human sets their own
  password in the UI
  ([docs](https://docs.medusajs.com/resources/medusa-cli/commands/user)). This is
  the precedent for `--login-link`.
- **Sentry self-hosted.** Creating the user is part of the interactive installer.
  `CI` / `SKIP_USER_CREATION=1` skip it and print the `createuser` command for
  later
  ([script](https://raw.githubusercontent.com/getsentry/self-hosted/master/install/set-up-and-migrate-database.sh)).
  A good pattern: the non-interactive path tells you the next command instead of
  failing silently.

---

## 3. Patterns and trade-offs

### 3.1 Interactive CLI (Django, Wagtail, Filament, Nova, Strapi CLI, GitLab reset)

- **Strengths:** the password is never in a file, env or history. Only someone
  with shell and database access can run it, the same trust as running
  migrations.
- **Weaknesses:** it needs a TTY, so it breaks CI, containers and agents unless
  there is a non-interactive mode, and that mode is where the tools leak
  (passwords in env or argv).
- **Prod:** fine; it runs as a one-shot (`docker compose run`, `kubectl exec`).

### 3.2 First-run web screen guarded by "zero admins" (Strapi, Payload, Ghost, Metabase, Directus onboarding)

- **Strengths:** the most discoverable option, because the user just opens the URL.
- **Weaknesses:**
  - Whoever reaches a fresh deployment first becomes the admin: the "setup race"
    for any internet-exposed instance.
  - A server-side check under a lock is mandatory (Strapi locks a row, Payload
    and Metabase check in a transaction).
  - A token only helps if it is secret: printed to the server console or written
    to a root-readable file, as Jupyter and GitLab do. A token in a public API
    response, as in Metabase, does not help.
  - In Sandloom it also needs an unauthenticated write endpoint in identity,
    which every route-policy rule pushes against.
- **Prod:** risky unless it is token-gated. Sandloom turns the console off in
  prod, which reduces exposure, but the API would stay reachable.

### 3.3 Env-driven bootstrap (Directus, Keycloak, GitLab, FastAPI template, Appsmith, Supabase)

- **Strengths:** fits containers, CI and infrastructure as code, and is idempotent
  when guarded by "first install" or "no admin".
- **Weaknesses:**
  - The secret sits in env: visible in `docker inspect`, `/proc`, CI logs and
    `.env` files that get committed.
  - Stale env vars mislead ("I changed `ADMIN_PASSWORD` and nothing happened").
  - Mitigations: `_FILE` / Docker secrets (GitLab), `--password:env` (Keycloak),
    a "temporary" label (Keycloak), refusing placeholders (FastAPI template).
- **Prod:** acceptable with secret files and first-run-only semantics.

### 3.4 Default or generated credentials (Grafana, ActiveAdmin, GitLab, Directus-unset)

- A **known default** (`admin/admin`, `admin@example.com/password`) is the worst
  option: a skippable change prompt does not fix it.
- A **generated** password is good when it is written to a restricted file that
  expires (GitLab), and bad when it is printed to logs (Directus fallback).

### 3.5 Invite or one-time link (Medusa `--invite`, Jupyter token URL)

- **Strengths:** no password ever exists in the CLI, so it is ideal for agents
  and remote operators.
- **Needs:** single use, a short lifetime, a server-side record, and audit.
- In Sandloom identity already has a password-reset nonce flow
  (`password_reset_nonce`, and reset marks the email verified,
  `auth_service.py:715-740`), so `--login-link` could reuse it without a mail
  server. The console needs a "set password" screen, or the link points at the
  identity reset page.

### 3.6 Dev-only auto admin (Payload `autoLogin`, Filament/Nova local-open gates)

- Convenient, but a dev shortcut that turns on by accident in prod is the risk.
  If adopted, it should be tied to `ENVIRONMENT != prod` and refused by the
  settings preflight in prod.
- Not recommended for Sandloom v1: the CLI is already one line.

---

## 4. Risks and open questions for the owner

1. **Dry-run by default for `create`?** Every operator command today defaults to
   dry-run with `--apply --reason`. For the first command a newcomer runs, that
   costs an extra step. The proposal is to act in dev and require
   `--apply --confirm-production` in prod. This breaks the uniform rule.
2. **Command home and name:** `sandloom admin create` (where the user looks) or
   `sandloom security create-admin` (next to `grant-admin`)? An alias for both is
   possible but adds surface (SANDLOOM-T008 / public API).
3. **Contract surface:** a new `identity.security.v1` method (`create_platform_admin`)
   is a contract change: snapshot, stub, and possibly a `.v2` decision. Plaintext
   password over RPC in split deployments, or have identity generate the password?
4. **Setup-state endpoint:** is an unauthenticated `has_admin` boolean
   acceptable outside prod? It reveals only that the instance is fresh, and in
   prod it would be off.
5. **2FA for platform admins:** should the console require 2FA enrolment at the
   first sign-in in prod? Keycloak's "temporary admin" idea suggests at least a
   banner.
6. **Where the generated secret file lives:** `.sandloom/first-admin.txt` must be
   in the generated `.gitignore` (today it ignores `.env*`, `.venv/`). Should the
   file be auto-removed after the first successful admin sign-in, like GitLab's
   24 h removal?
7. **Env bootstrap (1.2):** worth it now, or wait until someone deploys with
   compose or Kubernetes and asks?
8. **Multi-tenant apps:** after creating the admin, should `sandloom new
   --with tenancy` also suggest creating the first organization? Out of scope
   here; the admin is deliberately global.
9. **Docs:** `docs/admin-console/index.md` ("Create the first administrator") and
   `docs/getting-started/` must change together with `QUICKSTART`, which is
   tested against them.

Unverified items: Filament `make:filament-user` non-interactive flags; Jupyter
token CLI option name; current Directus behaviour without `ADMIN_PASSWORD`;
Metabase token readable from `/api/session/properties` (secondary source; the
code comment confirms it is public); Ghost's server-side guard on the owner
setup.
