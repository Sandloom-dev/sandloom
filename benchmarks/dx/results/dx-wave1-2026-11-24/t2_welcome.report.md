I built the `welcome` module in a new `teamhub` app, and `uv run sandloom test` passes on the whole project.

## 1. What I built
- **App:** `teamhub/` created with `sandloom new teamhub --with identity,tenancy,billing --no-git --yes`.
- **Welcome email:** the module listens for `identity.user_registered.v1`. It looks up the user's email and name through the `identity.contact.v1` contract and sends one welcome email.
- **Exactly once:** a `welcome_emails` table has a UNIQUE `user_id`, and the row is claimed before sending. A redelivered event, a second registration event for the same user, or a concurrent delivery finds the row and sends nothing. A user who isn't active gets nothing.
- **Endpoint:** `GET /api/v1/welcome/status` returns `{sent, sent_at}` for the signed-in user.
- **Migration:** one hand-reviewed revision creates `welcome_emails`.
- **Tests:** `welcome/tests/test_welcome.py` has five tests:
  - one email on registration, and the status route reports it;
  - a redelivered envelope and a second event for the same user send nothing more;
  - an inactive user gets no email;
  - status is per user;
  - an unauthenticated call returns 401.

  The generated migration test also passes.
- **Known gap:** the email is sent inside the unit, before commit. If the send fails, the claim rolls back and the retry sends. If the commit itself fails after a successful send, the email could go out twice. I pass a `dedupe_key`, which the mail driver can use to drop the repeat. I chose this over sending after commit, because a failed post-commit send would leave the row claimed with no email ever sent.

## 2. What I read
- `sandloom new --help`, `teamhub/AGENTS.md`, and `teamhub/modules/AGENTS.md`.
- The `example` module's `api.py`, `services.py`, `events.py`, `schemas.py` and tests.
- `sandloom docs` for events and contracts, and for `on`, `on_commit`, `durable_inbox`, `operation_key`, `Notification`, `fake_contract`, `deliver` and `as_user`.
- Recipes: `call-contract`, `test-service-or-task`, `retries`.
- The identity package's `integrations.py` (to see how it builds an email `Notification`), plus the generated contract stub and DTO.

## 3. Mistakes and what caught them
- **Wrong import path:** I guessed `sandloom.sdk.time`, which doesn't exist. `sandloom migrate new` failed at module load; the right path is `sandloom.sdk.data`.
- **`sed -i` error:** BSD `sed` rejected my edit, and the `&&` chain skipped the rest of that command. I redid it with Python and the Edit tool.
- **Wrong test payload:** `deliver` needs the full event envelope, not just the payload. Schema preflight (`SANDLOOM-R004`) caught it.
- **`WallClock().now()`:** `WallClock` is a SQL expression, not a clock object. A test run hit an `AttributeError`, and I changed it to `WallClock()`.
- **Format and lint:** `ruff format` and `ruff check` flagged my first files. I fixed them with `ruff format` and `ruff check --fix`.

## 4. Where I got stuck or guessed
- I couldn't find how a global (non-tenant) table is meant to be declared. I left it without `TenantScoped`, and `sandloom check` accepted that.
- I replaced the scaffold's sample `welcome_items` table, its event, and its migration baseline with the real table.
- I found no documented way to capture outbound mail in tests, so the tests monkeypatch `services.notifier`.
- The email text ("Welcome to TeamHub") is my own placeholder.
- I didn't check the send path against a real mail driver, only against a fake notifier.
