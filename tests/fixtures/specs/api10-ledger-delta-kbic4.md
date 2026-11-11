# KB-IC-4: driver availability, periodic, install-matrix lanes (2026-11-09)

Owner decision on module infrastructure dependencies
(`design/kernel-boundary/owner-decision-infra-deps.md`), items 1, 2 and 4; ADR-0011 amended.

## Changed entries

- G-MODULE-15 (amended, enforced): a bundled module booting alone may also be
  refused with SANDLOOM-B037 for a required capability with no installed
  driver (G-INFRA-12).

## New entries

- G-INFRA-10 (new, enforced): a driver registration names the packages it
  needs; the preflight checks them with `find_spec`; a missing one is
  SANDLOOM-B037 (KB-IC-1). Tests existed in
  `test_infra_driver_availability.py`; the entry lists them.
- G-INFRA-11 (new, enforced): `periodic` is a `taskqueue` feature only
  `celery` advertises; a module's effective capabilities add what its code
  shows, and SANDLOOM-C033 asks for the declaration (KB-IC-2). Tests existed
  in `test_module_capability_derivation.py`; the entry lists them.
- G-INFRA-12 (new, enforced): the new I-19. A module combination whose
  required capabilities all have an installed driver with their features
  starts and is ready; otherwise it is refused with SANDLOOM-B037 naming the
  module, capability and missing feature. Proved on the sixteen subsets with
  real services, an unreachable broker, a missing redis or celery package and
  a taskqueue driver without `periodic`
  (`tests/integration/module_subsets/test_module_subsets_infrastructure.py`,
  the P2 unstubbed lane in `test_module_subsets_wheels.py`).
