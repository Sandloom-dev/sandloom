+++
id = "t10_org_banner"
title = "Admin-only write endpoint on a tenant row (the cheapest task)"
modes = ["app", "framework"]
allowed = ["src/sandloom/banner/"]
+++
Organization admins want to show a short notice to everyone in their
organization (for example "Office closed on Friday").

Create a new module called `banner`. An organization admin can set the
organization's banner (a short text) and clear it; every member of the
organization can read the current banner. Regular members may not change it,
and one organization must never see or change another organization's banner.
We need to be able to tell who changed the banner and when.

Ship it with tests, the way the rest of the codebase does things.
