+++
id = "t1_notes"
title = "Tenant CRUD module + event + paid gate"
modes = ["app", "framework"]
experiment = "A"
allowed = ["src/sandloom/notes/"]
+++
We want shared notes inside an organization.

Create a new module called `notes`. A member of an organization can create a
note (title, body) in that organization, list the organization's notes, read
one, edit it and delete it. Members of other organizations must never see or
touch them.

When a note is created, other parts of the product will want to react to it
(search indexing, notifications), so announce it to the rest of the system.

Creating notes is a paid feature: only organizations with a paid subscription
may create notes. Reading stays free.

Ship it with tests, the way the rest of the codebase does things.
