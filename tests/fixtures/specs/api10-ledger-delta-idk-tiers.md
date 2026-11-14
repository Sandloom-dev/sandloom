# idk-tiers: driver tiers and the `experimental_driver` risk (2026-11-14)

Spec 2.5 of the infra-drivers design: every driver has a tier, and production
refuses an `experimental` one unless the deployment accepts `experimental_driver`.

## Changed entries

- G-RISK-03: the registry holds thirteen risk ids; `experimental_driver` joins
  the pinned list.

## New entries

- G-RISK-16: an experimental driver is refused in production (SANDLOOM-B023)
  unless `experimental_driver` is accepted, at startup and when a provider is
  built; a development-only driver is refused with SANDLOOM-B022 whatever its
  tier. Enforced by `test_driver_tiers.py` and `sandloom-s3`'s `test_tier.py`.
