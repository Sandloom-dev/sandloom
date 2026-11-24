Invitations now announce a v2 event with `channel` (`"email"`) and `recipient`, and the activity module reads that v2 event. `sandloom test` passes: 18 tests, lint, mypy, `sandloom check` and the generated-files check.

**1. What I built**
- **New event:** `InvitationSentV2` on `invites.invitation.sent.v2` in `invites/events.py`. It has `channel` and `recipient` instead of `email`. The Event API reserves the name `channel`, so the Python field is `via` with `Field(alias="channel")`. On the wire the key is still `channel`.
- **Dual publish:** `publish_invitation_sent()` publishes v1 and v2 together in one unit. Teams and services that still read v1 are unaffected. `send_invitation` now calls it.
- **Activity module:** its consumer now reads v2 and uses `recipient`.
- **Generated files:** `sandloom generate` wrote the new v2 event schema and refreshed the module snapshots.
- **Tests:**
  - A new invites test checks the outbox holds both versions with the right payload shapes.
  - The activity tests now deliver v2.
  - The API test no longer inspects the event, because `published()` doesn't see dual-published events. I moved the tenant and token assertions into the outbox test.
  - I dropped the old check that the token is hashed in the stored invitation. That is a loss in coverage; say if you want it back some other way.
- **Not done:** v1 is not retired. Check `uv run sandloom streams consumers` and drop it once no group reads it.

**2. What I read**
`AGENTS.md`, `.agents/skills/sandloom-event-version-roll/SKILL.md`, and `sandloom docs recipe event-v2`. I also read the invites `events.py`, `services.py` and tests, the activity `consumers.py` and tests, and the source of `published` and `recording_uow`.

**3. Mistakes and what caught them**
- I passed `channel=` to the v2 constructor, and mypy rejected it. I switched to `model_validate` with a dict.
- I kept a `published(InvitationSent)` assertion in the API test, and the suite failed with an empty list. That is when I found it ignores `publish_event_versions`.

**4. Where I guessed**
I chose the `via` alias and the `InvitationSentV2` and `publish_invitation_sent` names myself. The docs only say to use an alias.
