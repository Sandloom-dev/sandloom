+++
id = "t4_resend_invitation"
title = "Admin-only write endpoint in an existing module"
modes = ["app", "framework"]
# Framework mode edits the bundled tenancy: a kernel-contributor task, run
# only when named (README: 'Modes').
kernel_modes = ["framework"]
# App mode starts from the seeded teamhub app (seeds/teamhub/): its own
# `invites` module holds the organization's invitations.
seed = "teamhub"
allowed = ["src/sandloom_tenancy/"]
app_allowed = ["{app}/modules/invites/"]
+++
Organization admins keep asking us to resend an invitation when the invitee
lost the email.

<!-- mode: framework -->
Add a way for an organization admin to resend a pending invitation of their
organization: the old invitation link stops working, a new one is issued with
a fresh expiry, and the invitee gets it again. Regular members may not do this.
<!-- /mode -->
<!-- mode: app -->
In the invites module, add a way for an organization admin to resend a
pending invitation of their organization: the old invitation link stops
working, a new one is issued with a fresh expiry, and the invitee gets it
again. Regular members may not do this.
<!-- /mode -->

Ship it with tests, the way the rest of the codebase does things.
