# Owner decision: module infrastructure dependencies (2026-11-08)

Basis: [research-infra-deps.md](research-infra-deps.md) (60b118a), accepted as the basis for fixing I-19.
Tracked in tm `kb-04a-p2-taskqueue-ready`; implementation tasks `kb-ic-1` … `kb-ic-5`.

## Direction

Complete the existing `CapabilityRequirement`, `InfraRegistry`, `DriverRegistration` and module
checks. No new subsystem for infrastructure dependencies. No module wheel depends on Celery (or
any driver package) through its metadata. A module declares the abstract capability it needs, the
application chooses the driver, the kernel checks that they match.

Three problems the research found, all to be fixed:

1. Module declarations are incomplete: billing needs periodic task execution and does not say so
   in `capabilities=`.
2. The preflight is a false positive: a registered driver counts as usable even when its Python
   dependencies are missing.
3. Startup and readiness are split the wrong way: a permanent installation error shows up only in
   `/ready`, while a temporary broker outage can go unnoticed.

Success criterion: a module either gets every infrastructure guarantee it needs, or Sandloom says
exactly which guarantee is missing, not only which package is not installed.

## Answers to the eight questions

| # | question | decision | conditions |
|---|---|---|---|
| 1 | identity cleanup | optional | token expiry and revocation are checked independently of cleanup; missing cleanup is shown in diagnostics and `doctor`; storage growth is documented |
| 2 | memory cache | conditional | a separate dev/test driver, refused in production; never an automatic hidden fallback for Redis; its absence does not block the I-19 fix; for security-sensitive uses (revocation, locks, rate limiting, idempotency) document the loss of state on restart and the lack of cross-process consistency; while it is in use Sandloom states the weaker guarantees |
| 3 | web readiness on a broker outage | conditional | do not drop web readiness for a short broker outage when the web process does not use the queue directly; billing must monitor that its reconciler actually runs: no worker/beat, or the allowed processing interval exceeded, is not a hidden degradation; a separate, testable criterion is recorded; `Celery.health() == ok` alone is not enough (the broker can be up while worker and beat are absent) |
| 4 | Celery broker check | yes | a real bounded broker probe through the existing TTL single-flight cache; no extra I/O per request |
| 5 | explicitly selected optional module with an unmet required capability | fatal refusal | if a module is selected, its required capabilities must be met |
| 6 | derivation and a static check | both | the declared required/optional semantics stay; a derived feature never bypasses the declaration; the runtime check stays the last line of defence; the static analysis does not claim to be complete |
| 7 | a zero-dependency taskqueue driver | defer | only when a separate need appears, with conformance checks over the full worker/scheduler path; no second worker/scheduler system now |
| 8 | code numbers | free B/C codes | with `sandloom explain`, tests and the catalogue; the diagnostic names module, capability, instance, driver, feature and an install hint |

## Additional conditions

- `periodic` is not granted to a driver for supporting `enqueue`. It means the ability to run
  periodic jobs with the stated guarantees; today only the Celery driver provides it.
- `find_spec` is an early installation check, not proof that the driver works. Import errors of
  transitive dependencies and initialisation errors of the selected driver also give a diagnosable
  refusal.
- A synthesized, unused instance is distinguished from a capability the kernel itself requires.
  Dropping unneeded probes must not disable the revocation/security cache requirement.
- Identity and tenancy declare `notification` with feature `email` wherever the features that send
  mail are enabled, so production cannot silently send required mail (recovery, invitations) to
  the log notifier.
- KB-04 is tested not only with stubbed health probes: a missing Python driver and real
  infrastructure availability get their own lanes.
- I-19, I-20, the guarantee ledger and ADR-0011 are amended.

## I-19 (new wording, to be applied by kb-ic-4)

A supported module combination starts and becomes ready when every capability its modules require
(declared or derived) has a configured, installed driver with the required features. Otherwise
startup refuses with a diagnosable code naming the missing guarantee. A configured capability that
is temporarily unavailable is reflected in the readiness of the processes that use it; a required
background guarantee that stops being met (billing's reconciler) is not hidden behind a green
`/ready`.

## Implementation split

Small, independent PRs; W1's main work and W2 are not stopped by them.

1. `kb-ic-1` preflight and driver availability metadata.
2. `kb-ic-2` declarations, derivation and the static gate.
3. `kb-ic-3` readiness, real health probes and billing reconciler monitoring.
4. `kb-ic-4` notification(email) declarations, the install matrix lanes, I-19/I-20, ledger, ADR-0011.
5. `kb-ic-5` dev/test memory cache driver (optional, not a blocker).
