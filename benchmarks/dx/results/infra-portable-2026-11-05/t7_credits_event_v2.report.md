The invites module now announces a v2 "invitation sent" event with `channel` and `recipient`, and the activity module reads that v2 event. `uv run sandloom test` passes on everything: tests, ruff, mypy, `sandloom check`, snapshots and migration drift.

**1. What I built**
- **Producer (`invites/events.py`):** I added `InvitationSentV2` on `invites.invitation.sent.v2`. `channel` is `"email"` and the address goes in `recipient`; there is no `email` field. The v1 class is untouched.
- **Dual publish:** `publish_invitation_sent()` writes v1 and v2 together, so they commit or fail as one. `send_invitation` in `invites/services.py` now calls it. Other services reading v1 keep working, and invitations still go out by email only.
- **Field naming:** `channel` is a reserved Event API name, so the Python field is `via` with the wire alias `channel`. The wire payload still has the key `channel`.
- **Activity module (`activity/consumers.py`):** the view now reads v2 and the feed summary uses `recipient`.
- **Tests:** a new invites test checks that both versions are published in one unit. It checks the v1 and v2 payload shapes, tenant ids and token hashing. The activity test now delivers v2 events.
- **Generated files:** `sandloom generate` wrote the v2 schema `teamhub/shared/events/invites.invitation.sent.v2.json` and refreshed the snapshots.
- **Not done yet:** v1 is still published and has to stay until no consumer group reads it. That is a later step, once `sandloom streams consumers` shows v1 is unread in every environment.

**2. What I read**
- `AGENTS.md`
- the `sandloom-event-version-roll` skill
- `teamhub/modules/AGENTS.md`
- `sandloom docs recipe event-v2`
- the invites and activity modules' source and tests

**3. Mistakes and what caught them**
- I ran `sandloom` before `uv sync`. It failed with "No module named celery", so I ran `uv sync` and used `uv run`.
- I built `InvitationSentV2` with `channel=` as a keyword. Mypy flagged it, so I used `model_validate` with the alias key instead.
- My API-level assertion on the published v1 and v2 events failed. The test helper `published()` doesn't see events sent through `publish_event_versions`. I moved those checks into the `recording_uow` test.

**4. Stuck or guessed**
- The "published events" check is no longer in the API-level test. I didn't look for another way to read dual-published events from a request.
- I assumed `recipient` should hold the lowercased address, as `email` did.
