The `welcome` module is built and `sandloom test welcome` passes every gate: 8 module tests, the smoke suite, lint, format, mypy, `sandloom check`, snapshots and migration drift. I didn't run `sandloom test --full`. I also didn't test two deliveries running at the same moment.

## 1. What I built
- **App:** `teamhub`, created with `sandloom new` using identity, tenancy and billing. There was no git repo (I ran it with `--no-git`), nothing was committed, and the services were not set up.
- **Sending:** a consumer in `teamhub/modules/welcome/consumers.py` listens for identity's `identity.user_registered.v1` event. It looks up the user's email through identity's contact contract, so no address is stored in the new table. It sends the email through the framework's `notifier()`.
- **Exactly one email per user:** a `welcome_emails` table has a UNIQUE `user_id`. The code claims the row with `INSERT … ON CONFLICT DO NOTHING` and locks it with `SELECT … FOR UPDATE`. The email is skipped if `sent_at` is already set. It also carries a `dedupe_key` of `welcome:<user_id>`, so a retry after a failed commit can't send a second copy.
- **Inactive users:** a user whose account is inactive gets no email.
- **Status endpoint:** `GET /api/v1/welcome/status` needs a signed-in user and returns `{sent, sent_at}` for the caller only.
- **Migration:** a single `0001_welcome` revision creates `welcome_emails`.
- **Tests:** they cover one email per registration, redelivery, two different events for the same user, separate users, status before and after sending, no cross-user leakage, an inactive user, and the endpoint returning 401 without a token.
- **Sample code:** I removed the sample items table, routes and events, and pointed the smoke test at the new endpoint.

## 2. What I read
`sandloom new --help`, `AGENTS.md`, `teamhub/modules/AGENTS.md`, `sandloom docs recipes` and the retries recipe, and `sandloom docs` for `on_commit`, `conflict_insert`, `open_uow`, `durable_inbox`, `operation_key`, `deliver`, `fake_contract`, `capture_effects` and `notifier`. I also read the generated `identity_contact_v1.py` and `identity_dto.py`, the `Notification` class in the framework source, and identity's `UserRegisteredEvent` in `sandloom_identity/events.py`. I ran `sandloom explain SANDLOOM-T002`.

## 3. Mistakes and what caught them
- **Scaffold flag:** I passed `--field user_id:uuid` to `sandloom add consumer`. The CLI rejected it and listed the valid types, which showed that `UUID` is the right spelling.
- **Contract import:** I imported the contract stub from `sandloom.shared.contracts`. This app's stub lives at `teamhub.shared.contracts.identity_contact_v1`. The failed import broke `migrate new`.
- **Migration chain:** autogenerate wrote a second revision that dropped the old `welcome_items` table. The gate (SANDLOOM-T002) rejected that because the baseline still created the table, so I replaced the baseline and deleted the second revision. This is safe only because nothing had been applied to a real database.
- **Test events:** my first tests passed `deliver` just the payload, and schema validation (SANDLOOM-R004) required the full event envelope. I rewrote the helper to send one.
- **Smoke test:** it still called the removed sample route and returned 404 instead of 401. The suite caught it.
- **Formatting:** `ruff format` flagged my first files.

## 4. Where I guessed or got stuck
- **Email failures:** a failed send is retried only if the whole unit rolls back. I send the email inside the consumer's unit rather than in `on_commit`, because `on_commit` swallows failures and would lose the retry.
- **Fake mail in tests:** the test fakes `services.notifier` with a monkeypatch, because I found no built-in fake for it.
- **Sent timestamp:** I stamp `sent_at` with `datetime.now(UTC)` in Python. The gates accepted it, but I didn't confirm it's the preferred form.
- **Event view:** I wrote the consumer's view of the event as `payload: dict[str, Any]` rather than a typed field. It passes the producer-match check.
- **Docs:** I couldn't find a docs file for the notifier's real email driver or its `dedupe_key` behaviour. I'm assuming drivers drop duplicate keys, as the `Notification` docstring suggests.
- **Slow command:** one early `find /` search ran in the background and was slow; I didn't use its result.
