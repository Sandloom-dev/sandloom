+++
id = "t3_quotas"
title = "Command contract + call from another module"
modes = ["app", "framework"]
experiment = "C"
allowed = ["src/sandloom/quotas/", "src/sandloom_tenancy/"]
# App mode: tenancy is an installed package, so the caller is the app's own
# `invites` module (same mechanism: a required command contract, called once
# per keyed request).
app_allowed = ["{app}/modules/quotas/", "{app}/modules/invites/"]
+++
<!-- mode: framework -->
Organizations may only send a limited number of invitations per calendar
month (default: 50).

Create a new module called `quotas` that owns per-organization monthly quotas
and can consume units of one. Then make sending an invitation in the existing
tenancy module consume one unit of the organization's `invitations` quota; when
the quota is used up, sending an invitation must be refused with a clear error.
A retried request must never consume twice.
<!-- /mode -->
<!-- mode: app -->
Organization members want to invite people from outside the product by email.

Create a module called `invites`: a member can send an invitation to an email
address in their organization (we only record it for now) and list the
organization's sent invitations.

Organizations may only send a limited number of invitations per calendar
month (default: 50). Create a second module called `quotas` that owns
per-organization monthly quotas and can consume units of one. Sending an
invitation consumes one unit of the organization's `invitations` quota; when
the quota is used up, sending an invitation must be refused with a clear error.
A retried request must never consume twice.
<!-- /mode -->

Ship it with tests, the way the rest of the codebase does things.
