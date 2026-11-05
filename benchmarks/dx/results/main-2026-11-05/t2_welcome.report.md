The welcome email module is built, and `sandloom test welcome` passes every step, including the lint, type and migration-drift gates. That run covers the 7 new tests plus the rest of the fast suite. I did not run `--full`, so the slow tests are untested. I also didn't start the app, so nothing was checked beyond the tests, and the welcome table's migration only went through the drift check.

**1. What I built**

The app is `teamhub`, created with `sandloom new` and the identity, tenancy and billing modules. The new module is `teamhub/modules/welcome`.
- **Sending:** when identity publishes `identity.user_registered.v1`, the consumer in `consumers.py` calls `services.send_welcome`.
- **Once per user:** a `welcome_emails` table has a UNIQUE `user_id`. `send_welcome` claims that row first, so redelivered or duplicate events (and parallel ones) can't send twice.
- **Address lookup:** the email address and name come from identity's `identity.contact.v1` contract at send time. Nothing is copied into the table.
- **Sending and recording:** the mail goes out through the framework's `notifier()` in the same unit as the claim, and `sent_at` is set afterwards.
- **Failures:** if sending raises, the claim rolls back and a retry sends it. If the user is unknown or inactive, the claim is released and nothing is sent.
- **Endpoint:** `GET /api/v1/welcome/status` requires sign-in and returns `{sent, sent_at}` for the caller.
- **Tests:**
  - A registration sends exactly one mail.
  - The same event delivered three times sends one.
  - A second event for the same user sends none.
  - Each user sees only their own status.
  - A failed send leaves no record and can be retried.
  - An inactive user gets no mail.
  - The endpoint returns 401 without sign-in.

There is one known gap. If the process dies after the mail is sent but before the commit, the retry sends a second one, so "exactly one" holds except for that crash window. Closing it fully would mean giving up retries on failed sends.

**2. What I read**
- `teamhub/AGENTS.md` and `teamhub/modules/AGENTS.md`.
- `sandloom docs` output: `recipes`, `events`, `contracts`, `identity.user_registered.v1`, `identity.contact.v1`, and the recipes `call-contract`, `test-service-or-task` and `projection-from-platform-events`.
- `sandloom docs` for `durable_inbox`, `operation_key`, `notifier`, `Notification`, `deliver`, `as_user`, `fake_contract`.
- `sandloom explain C014` and `C025`.
- The `example` module, as the pattern.
- `sandloom_identity/integrations.py`, to see how identity sends mail.

**3. Mistakes and what caught them**
- **Reading the wrong docs path.** I looked for docs files that don't exist in the installed package. The `sandloom docs <name>` command was the way in.
- **Wrong event view.** The scaffolded consumer view had wrong field types, and my first version declared only `payload`. `deliver` rejected the test events because they didn't match the producer's schema, so five tests failed. I fixed it by declaring the full set of required fields and building complete events in the tests.
- **Truncated output.** I piped the first test run through `tail` and lost the failure details. I re-ran with output to a file.
- **Scaffold leftovers.** I deleted the sample item model, events and generated schema from the new module.

**4. Where I got stuck or guessed**
- **Capturing sent mail in tests.** I found no testing helper for it, so I patched `services.notifier` with a fake.
- **No tenant on the table.** I made `welcome_emails` a plain table, with no `TenantScoped`, because welcome is per user. The gates accepted that.
- **Wording and sender.** The email text and subject are my own placeholders. I didn't configure a real sender, and `dedupe_key` is set on the notification but I didn't check whether the log or SMTP driver acts on it.
