+++
id = "t2_welcome"
title = "Exactly-once event consumer + endpoint"
modes = ["app", "framework"]
experiment = "B"
allowed = ["src/sandloom/welcome/"]
+++
New users should get a welcome email right after they register — exactly one
email per user, even if things are retried or delivered twice.

Create a new module called `welcome` that does this, and add an endpoint where
a signed-in user can check whether their welcome email has been sent (and
when).

Ship it with tests, the way the rest of the codebase does things.
