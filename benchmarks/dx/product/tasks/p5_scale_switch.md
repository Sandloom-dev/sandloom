+++
id = "p5_scale_switch"
title = "Move Academy from the monolith to microservices"
modes = ["app", "framework"]
# Framework mode edits the framework repository's own microservices profile:
# a kernel-contributor task, run only when named (README: 'Modes').
kernel_modes = ["framework"]
kind = "switch"
start = "p1_build_full"
modules = []
allowed = ["deploy/"]
+++
<!-- mode: framework -->
This repository contains the Academy product (its modules are under
`src/sandloom/`, next to the bundled identity, tenancy and billing).
It now has to run as microservices, in the existing microservices profile
(`deploy/compose/microservices.yml` and its gateway), next to the
identity, tenancy and billing services:

- an `academy` service with `catalog` and `enrollment`;
- a `learning` service with `progress`, `certificates` and `reminders`;
- an `insights` service with `reporting`.

Product behaviour must not change. Do whatever the split needs, and say in
your report which files you changed and why.
<!-- /mode -->
<!-- mode: app -->
This directory contains the Academy product, a Sandloom application (`{app}/`;
it uses the bundled identity, tenancy and billing modules). It runs as one
service today (`deploy/docker-compose.yml`). It now also has to run as
microservices, from the same code, in a second deployment
(`deploy/compose/microservices.yml`) behind one gateway, each service
with its own database:

- `identity`, `tenancy` and `billing`, one service each;
- an `academy` service with `catalog` and `enrollment`;
- a `learning` service with `progress`, `certificates` and `reminders`;
- an `insights` service with `reporting`.

Product behaviour must not change, and the one-service deployment keeps
working. Do whatever the split needs, and say in your report which files you
changed and why.
<!-- /mode -->
