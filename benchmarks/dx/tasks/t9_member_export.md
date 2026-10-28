+++
id = "t9_member_export"  # id kept in app mode: results compare by it
title = "Authorization / billing gate on an endpoint"
modes = ["app", "framework"]
# Framework mode edits the bundled tenancy: a kernel-contributor task, run
# only when named (README: 'Modes').
kernel_modes = ["framework"]
# App mode starts from the seeded teamhub app (seeds/teamhub/): the export is
# of its own `invites` module's rows.
seed = "teamhub"
allowed = ["src/sandloom_tenancy/"]
app_allowed = ["{app}/modules/invites/"]
+++
<!-- mode: framework -->
Organization admins want to download their member list as CSV (user id, role,
joined date).

Add this export to the tenancy module. Only admins and owners of the
organization may use it, and only while the organization has an active paid
subscription — check the subscription at request time, a stale "was paid
yesterday" is not good enough.
<!-- /mode -->
<!-- mode: app -->
Organization admins want to download their organization's invitations as CSV
(email, status, sent date).

Add this export to the invites module. Only admins and owners of the
organization may use it, and only while the organization has an active paid
subscription — check the subscription at request time, a stale "was paid
yesterday" is not good enough.
<!-- /mode -->

Ship it with tests, the way the rest of the codebase does things.
