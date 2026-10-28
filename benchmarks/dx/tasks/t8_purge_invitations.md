+++
id = "t8_purge_invitations"
title = "Background (Celery) task with a schedule"
modes = ["app", "framework"]
# Framework mode edits the bundled tenancy: a kernel-contributor task, run
# only when named (README: 'Modes').
kernel_modes = ["framework"]
# App mode starts from the seeded teamhub app (seeds/teamhub/): its own
# `invites` module holds the invitations.
seed = "teamhub"
allowed = ["src/sandloom_tenancy/"]
app_allowed = ["{app}/modules/invites/"]
+++
Expired invitations pile up in the database.

Every night, delete invitations that expired more than 30 days ago (whatever
organization they belong to). It must be safe if the job runs twice or a
worker dies halfway.

Ship it with tests, the way the rest of the codebase does things.
