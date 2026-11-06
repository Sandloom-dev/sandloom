The "invitation sent" event now has a v2 with `channel` (`"email"`) and `recipient`. `sandloom test` passes on all its steps (tests, lint, types, check, generated files).

**1. What I built**
- **Invites module:** `InvitationSentV2` is on `invites.invitation.sent.v2`. `v1` is untouched, with its `email` field. Because the framework refuses a field named `channel`, v2 holds it as `via` with `Field(alias="channel")`, so the wire key is still `channel`. `publish_invitation_sent` sends v1 and v2 together through `publish_event_versions`, so they commit or fail as one. `services.send_invitation` now calls it. Invitations still go out by email only.
- **Activity module:** its consumer now reads the v2 channel as a view, with `via` aliased to `channel` and `recipient`, and builds the summary from `recipient`. Its test builds the v2 view.
- **Rollout:** services we don't control, and the other team's activity module before it deploys, keep reading v1 and nothing changes for them. The v1 class stays for replays.
- **Generated files:** `invites.invitation.sent.v2.json` and updated module snapshots, from `sandloom generate`.
- **Tests:** a new `invites/tests/test_event_v2.py` checks both channels are recorded together, v1 has `email` with no `channel`, and v2 has `channel` and `recipient` with no `email`. The invites HTTP test now checks that sending an invitation calls `publish_invitation_sent` with the right fields. The activity feed test is on v2.

**2. What I read**
- `AGENTS.md`
- the `sandloom-event-version-roll` skill
- `sandloom docs recipe event-v2`
- `sandloom docs events`
- the invites `events.py`, `services.py` and tests
- the activity `consumers.py` and tests
- `published()` in the framework source (`sdk/_decl/testing.py`)

**3. Mistakes and what caught them**
- I passed `channel=` as a keyword when building the aliased v2 class. mypy caught it, so I used `model_validate` with a dict.
- I used `published(InvitationSent)` in the HTTP test. It found nothing, because `publish_event_versions` events aren't recorded by `published()` (a pytest failure).
- Next I tried `recording_uow` around the HTTP call. It didn't capture the events either (a `KeyError`). I settled on a spy on the publish function for the HTTP test, and `recording_uow` in the dedicated unit test.
- ruff flagged an unused import left over from those edits, and I fixed it.

**4. Where I guessed**
- I assumed keeping the `via` alias on the activity view is fine. The view doesn't need the value, but it keeps the view matching the producer's schema.
- The HTTP test now checks the call to the publisher, not the actual outbox rows. The rows are only covered by the unit test, because I couldn't capture them through the HTTP client.
- I didn't check `sandloom streams consumers` (nothing running here), so retiring v1 is a later step.
