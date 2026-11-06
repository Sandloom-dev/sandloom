The invitation event now has a v2 that carries `channel` and `recipient`, the activity module reads it, and v1 is still published alongside it. `sandloom test` passes on every step. I did not run the slow suite or `--full`.

**1. What I built**
- **`InvitationSentV2`** on `invites.invitation.sent.v2`, in `invites/events.py`. It has `channel` (wire key; `"email"` for now) and `recipient` in place of `email`. The Python attribute is `delivery_channel`, because `channel` is reserved by the Event API.
- **v1 is unchanged and still published.** A new `publish_invitation_sent()` helper sends both versions together with `publish_event_versions`, so other services reading v1 keep working. `send_invitation` calls the helper, and invitations still go out by email only.
- **The activity consumer** now reads v2 and writes "Invitation sent to <recipient>".
- **Generated files:** `sandloom generate` added `invites.invitation.sent.v2.json` and refreshed the snapshots.
- **Tests:**
  - The activity test delivers the v2 view and still checks that a redelivery is recorded once.
  - A new invites test uses `recording_uow` to check that both channels are published and that v2 has `channel` and `recipient` but no `email`.
- **Retiring v1 is left for later.** Do that once `uv run sandloom streams consumers` shows no group still reads it.

**2. What I read**
- `AGENTS.md`
- `teamhub/modules/AGENTS.md`
- The `sandloom-event-version-roll` skill
- `sandloom docs recipe event-v2`
- The invites and activity modules and their tests
- The source of `sandloom.sdk.testing.published` and `recording_uow`

**3. Mistakes and what caught them**
- I built the v2 event with `channel=...` as a keyword. mypy caught it, so I switched to `model_validate` with a dict.
- I asserted on `published(InvitationSent)` after dual-publishing. The test run caught it: `published()` only sees events sent with `.publish()`, not `publish_event_versions`.
- I tried wrapping the HTTP request in `recording_uow`. That recorded nothing, since the client runs its own unit. I moved the logic into a helper and tested it with `recording_uow`, as the recipe does.

**4. Where I guessed**
- The alias handling is unusual: the view needs `Field(alias="channel")`, and v2 is dumped with `by_alias=True`. I settled on it from the recipe's note and the generated schema, not from a worked example.
- I did not check what the other consuming services do with v2; I could only keep v1 intact for them.
