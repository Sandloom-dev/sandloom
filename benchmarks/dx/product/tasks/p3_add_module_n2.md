+++
id = "p3_add_module_n2"
title = "Add the 2nd module (announcements) to a 1-module product"
modes = ["app", "framework"]
kind = "add"
start = "p2_build_one"
modules = ["x-announcements"]
allowed = ["src/sandloom/announcements/"]
+++
<!-- mode: framework -->
This repository contains the Academy product (its modules are under
`src/sandloom/`, next to the bundled identity, tenancy and billing).
<!-- /mode -->
<!-- mode: app -->
This directory contains the Academy product, a Sandloom application (`{app}/`;
it uses the bundled identity, tenancy and billing modules).
<!-- /mode -->
Add one more module to it, with tests, the way the rest of the codebase does
things. Do not change the existing product modules.

{spec}
