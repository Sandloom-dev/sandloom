+++
id = "t6_team_overview"
title = "Cross-module read model (projection)"
modes = ["app", "framework"]
allowed = ["src/sandloom/team_overview/"]
+++
Our dashboard needs a "team overview" page: for the signed-in user's current
organization, list its members with their role, together with the
organization's current subscription plan and status. It must stay fast for big
organizations and keep working when tenancy and billing run as separate
services.

Create a new module called `team_overview` that serves this list.

Ship it with tests, the way the rest of the codebase does things.
