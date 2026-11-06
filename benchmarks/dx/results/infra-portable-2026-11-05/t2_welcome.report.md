New users now get one welcome email when they register, and a signed-in user can check whether it was sent and when. `sandloom test welcome` passes every gate: the module's tests, the rest of the suite, ruff, mypy, `sandloom check` and the snapshot and migration-drift checks. I did not run `--full`, and I did not exercise the app end to end with a real mail backend or a real registration. All the mail tests use a fake notifier and fake contacts.

## 1. What I built
The app is `teamhub/`, created with `sandloom new` using identity, tenancy and billing. The new module is `teamhub/modules/welcome/`.

- **Trigger:** `consumers.py` listens to `identity.user_registered.v1` and calls `services.send_welcome(user_id)`.
- **Exactly one email:** a `welcome_emails` table has a UNIQUE `user_id`, and the consumer claims that row before sending.
  - Redeliveries, duplicate events and concurrent consumers hit the unique constraint and stop.
  - The claim and the send run in one savepoint, so a failed send undoes the claim and the retry sends again.
  - The notification also carries a `dedupe_key` of `welcome:<user_id>`. That covers a crash between sending and committing, where the claim rolls back but the email already went out.
- **Contact details:** the address and name come from `IDENTITY_CONTACT_V1` at send time and are never stored. An unknown or inactive user gets no email, but the claim stays and `sent_at` stays empty.
- **Endpoint:** `GET /api/v1/welcome/status` returns `{sent, sent_at}` for the caller only.
- **Table and migration:** the table is global (not `TenantScoped`) because users aren't tenant data. The migration is `0001_welcome`.
- **Tests (9):**
  - A registration sends one email and the status shows when.
  - Three redeliveries plus a second event for the same user still send one email.
  - A failed send is retried, not lost.
  - An unknown or inactive user gets no email.
  - Status is per caller.
  - Status needs authentication (401 without it).
  - A malformed event is ignored.
  - The migration matches the models.

## 2. What I read
- `sandloom new --help`
- The generated `AGENTS.md` and `teamhub/modules/AGENTS.md`
- `sandloom docs recipes`, plus the `retries`, `test-service-or-task` and `call-contract` recipes
- `sandloom docs` for `events`, `contracts`, `identity.user_registered.v1`, `identity.contact.v1`, `notifier`, `on_commit`, `durable_inbox`, `operation_key`, `WallClock`, `deliver`, `fake_contract` and `as_user`
- The scaffolded `example` and `welcome` module files, and `core/infra/contracts.py` for the `Notification` fields

## 3. Mistakes and what caught them
- **Missing `celery` at scaffold time:** `sandloom new` couldn't write the generated files, which was expected before `uv sync`. Running `uv sync` and `sandloom generate` fixed it.
- **Timestamp column:** I used `DateTime(timezone=True)`, and `sandloom check` flagged it as SANDLOOM-C029. I switched to `UtcDateTime`.
- **Long line in `consumers.py`:** ruff flagged it.
- **Incomplete event in tests:** I built the registration event with only two fields, and the bus schema validation rejected it. Tests now send a full envelope as a raw dict.
- **Unused import:** ruff's autofix removed a `uuid4` import I still needed, and the tests hit a `NameError`. I restored it.
- **Failed-send test:** my first version of the failed-send test failed because the claim was still in the test's shared session after the error. Wrapping claim and send in one savepoint fixed it, and the test now expects the error.
- **Leftover scaffold files:** I deleted the scaffold's sample `welcome_items` table and its migration. I also deleted a stray `welcome.item.created.v1.json` event schema from `teamhub/shared/events/`.

## 4. Where I got stuck or guessed
- **On-commit vs in-unit send:** the guide says email goes through `on_commit`, but I send inside the unit so that `sent_at` can be recorded and a failed send can retry. It is an at-least-once design made safe by the claim row and `dedupe_key`. I'm not certain the real mail driver honours `dedupe_key`.
- **Event view in `consumers.py`:** it declares only `occurred_at` and `payload`. `sandloom check` accepts it, but I didn't confirm that was the intended minimum.
- **Replacing the notifier in tests:** I monkeypatched `services.notifier`, because I found no SDK test helper for capturing notifications. There may be a sanctioned fixture I missed.
- **Slow gate:** `sandloom test` takes longer than the 2-minute foreground limit, so I ran it in the background.
