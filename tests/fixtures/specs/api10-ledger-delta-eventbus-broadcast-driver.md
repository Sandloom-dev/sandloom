# eventbus-broadcast-driver: the volatile event bus driver is `broadcast` (2026-11-17)

The `eventbus` driver that runs over a `broadcast` instance talks to no service of
its own, so it is named after the capability it runs on. The old name `redis` has no
alias: startup refuses it and names `broadcast`.

## Changed entries

- G-EVENT-14 (wording): the volatile event-bus drivers are `memory` and `broadcast`
  (they were `memory` and `redis`); the refusal of a volatile bus without the accepted
  risk is unchanged.
- G-RISK-04 (wording): the volatile event-bus drivers are `memory` and `broadcast`
  (they were `memory` and `redis`); the refusal in production and the accepted risk are
  unchanged.
