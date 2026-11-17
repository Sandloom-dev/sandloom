# eventbus-broadcast-driver: the volatile event bus driver is `broadcast` (2026-11-17)

The `eventbus` driver that runs over a `broadcast` instance talks to no service of
its own, so it is named after the capability it runs on. `redis` stays one release as
a deprecated alias of `broadcast` (a WARNING at startup).

## Changed entries

- G-RISK-04 (wording): the volatile event-bus drivers are `memory` and `broadcast`
  (they were `memory` and `redis`); the refusal in production and the accepted risk are
  unchanged.
