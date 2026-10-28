+++
id = "t5_billing_contacts"
title = "New tenant-owned table in an existing module"
modes = ["app", "framework"]
# Framework mode edits the bundled billing: a kernel-contributor task, run
# only when named (README: 'Modes').
kernel_modes = ["framework"]
# App mode starts from the seeded teamhub app (seeds/teamhub/): its own
# `workspace` module holds the organization's settings.
seed = "teamhub"
allowed = ["src/sandloom_billing/"]
app_allowed = ["{app}/modules/workspace/"]
+++
Finance teams want invoices to go to the right people.

<!-- mode: framework -->
In the billing module, let organization admins manage the billing contacts of
their organization: add a contact (name, email), list them and remove one. An
organization can have several contacts. One organization must never see or
change another organization's contacts.
<!-- /mode -->
<!-- mode: app -->
In the workspace module (the organization's settings), let organization
admins manage the billing contacts of their organization: add a contact
(name, email), list them and remove one. An organization can have several
contacts. One organization must never see or change another organization's
contacts. The product already runs in production with real data.
<!-- /mode -->

Ship it with tests, the way the rest of the codebase does things.
