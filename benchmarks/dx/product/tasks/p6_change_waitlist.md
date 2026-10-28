+++
id = "p6_change_waitlist"
title = "Feature change in one module (enrollment waitlist)"
modes = ["app", "framework"]
kind = "change"
start = "p1_build_full"
modules = []
target = "enrollment"
hidden_deselect = ["conflicts_are_409", "rejected_enrollment_is_not_charged"]
allowed = ["src/sandloom/enrollment/"]
+++
<!-- mode: framework -->
This repository contains the Academy product (its modules are under
`src/sandloom/`, next to the bundled identity, tenancy and billing).
<!-- /mode -->
<!-- mode: app -->
This directory contains the Academy product, a Sandloom application (`{app}/`;
it uses the bundled identity, tenancy and billing modules).
<!-- /mode -->

New requirement for enrollments: when a course is at capacity, enrolling no
longer fails; the learner is put on the course's waitlist instead
(`status = "waitlisted"`, answer `201`, nothing is charged). A new endpoint
`POST /api/v1/enrollment/enrollments/{enrollment_id}/cancel` cancels an
active enrollment (`status = "cancelled"`); the first waitlisted learner of
that course then becomes active and is charged at that moment. Everything
else in the product keeps working. Ship it with tests, the way the rest of
the codebase does things.
