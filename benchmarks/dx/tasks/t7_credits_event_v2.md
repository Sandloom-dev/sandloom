+++
id = "t7_credits_event_v2"  # id kept in app mode: results compare by it
title = "Roll an event from v1 to v2"
modes = ["app", "framework"]
# Framework mode rolls a bundled billing channel: a kernel-contributor task,
# run only when named (README: 'Modes').
kernel_modes = ["framework"]
# App mode starts from the seeded teamhub app (seeds/teamhub/): `invites`
# announces invites.invitation.sent.v1, `activity` consumes it.
seed = "teamhub"
allowed = ["src/sandloom_billing/"]
app_allowed = ["{app}/modules/invites/", "{app}/modules/activity/"]
+++
<!-- mode: framework -->
We are preparing fractional credits. As a first step, the "credits spent"
event that billing announces must carry the amount as a decimal string
(for example "1.50") together with a `unit` field, instead of an integer.
The ledger itself stays integer for now.

Other teams consume the current event from their own services and cannot all
upgrade on the same day: nobody may break during the rollout.
<!-- /mode -->
<!-- mode: app -->
We are preparing invitations by text message. As a first step, the
"invitation sent" event that the invites module announces must say how the
invitation travels: a `channel` field (`"email"` for now) and the address in
a `recipient` field, instead of the `email` field. Invitations themselves
still go out by email only.

The activity module is owned by another team and deployed on its own
schedule, and other services we do not control read the event too: nobody
may break during the rollout. Move the activity module to the new event as
part of this change.
<!-- /mode -->

Ship it with tests, the way the rest of the codebase does things.
