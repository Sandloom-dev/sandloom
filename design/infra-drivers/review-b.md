# Review B of the infra-drivers wave (idk-review-b)

> **Status.** No finding of this report is fixed in `idk-review-a-fixes`: its ten
> lows (L1 to L10) went to LOW BATCH 5. The report below is the original text,
> unchanged.

Base: main fa5390d6, read in a detached worktree (/tmp/review-b). Read only.
Result: 0 High, 0 Medium, 10 Low, plus notes.
Out of scope, not reported: the flat CACHE_URL removal (PR #286 and follow-ups).

## Findings

### L1 Wire-command scan has gaps
tools/packaging/check_package_distributions.py (~256-337, `_WIRE_COMMAND`, `_LUA_CALL`, `check_kernel_backends`).
- Missed commands: XPENDING, XINFO, XLEN, XRANGE, EVAL, SCRIPT, ZADD. A kernel module that sends one of these through a raw client passes the scan.
- Over-matches prose such as "XACK is sent" and lowercase "xadd" in comments or docstrings.
- Verified by reading the regex list and feeding sample lines to the check function in a scratch venv.
- Fix: derive the command list from redis-py's command table (or a shared constant) and match only string literals in call position.

### L2 Ratchet blind spot for packaged drivers
tests/architecture/test_kernel_no_raw_provider_usage.py:195-215 (`driver_only_members()` scans only `core/drivers/*.py`).
- Driver-only members defined in packages/sandloom-redis or sandloom-celery are not listed, so a kernel use of such a member is not caught.
- Verified by reading. The ledger and ratchet tests pass (31 passed with --noconftest).
- Fix: scan `packages/*/src/**` for the driver-only member set.

### L3 Traceback on a lean worker check
src/sandloom/core/worker.py (~380-410, `check_worker`) and src/sandloom/core/cli/worker.py (~40-60, `_check`).
- On a lean install with no taskqueue instance, `sandloom worker check` raises SANDLOOM-B037 or InstanceNotConfiguredError uncaught. The user gets a Python traceback, not the hint.
- Reproduced in /tmp/rb-lean.
- Fix: catch both in `_check` and print the code and hint with exit code 1.

### L4 celery and redis version skew
packages/sandloom-celery/pyproject.toml and packages/sandloom-redis/pyproject.toml.
- `celery[redis]` carries no redis lower bound, so the celery and sandloom-redis installs can resolve different redis-py versions.
- sandloom-redis has no hiredis extra.
- Verified by reading the resolved venvs (rb-celery, rb-redis).
- Fix: pin the same `redis>=` bound in both and add an optional `hiredis` extra.

### L5 ADR-0016 section 4 is stale
docs/adr/0016-kernel-ports-and-driver-packages.md section 4.
- It says the celery package holds the async runner and GC tuning. Both stay in the kernel.
- Fix: edit the ADR text to match the code.

### L6 Stale contract text in the kernel AGENTS.md
src/sandloom/core/AGENTS.md.
- "Drivers construct backends ... only in sandloom.core.drivers/**" is no longer true. Drivers live in packages.
- Fix: say "only in a driver package, importing sandloom.driverkit".

### L7 `unit: Any` in ExecutionAdapter
src/sandloom/core/infra/ports/database.py:650 and 667.
- Loses typing at the port boundary.
- Fix: a Protocol or TypeVar for the unit.

### L8 `_resolved_options` reaches global settings
src/sandloom/core/config/tiers.py:807-850 calls `sandloom.core.infra.config.resolved_instances` by importlib name. `root_default_infra` reads the global `settings` (infra/config.py ~183-300, composition.py ~35-66).
- Behaviour depends on process state, not on the composition passed in.
- Will go away with the tiers.py removal in PR #286, so low priority.

### L9 Stale doc lines
- docs/operate/operator-reference.md:964 lists `idempotency_key` as a feature.
- docs/reference/configuration-tiers.md:74 says "redis lease on CACHE_URL". The CACHE_URL part is also in the #286 scope.
- docs/operate/upgrade.md has no task-API section. The steps are only in CHANGELOG.md:109-111.
- Fix: edit the three places.

### L10 Probe rows
"The probe rows" could not be identified unambiguously. I checked the lane probes (the ci.yml probe steps) and the doctor and ready probes, and found no defect. If a specific table was meant, name it.

## Checked and sound

Packaging (uv, clean venvs under /tmp, wheels built from the worktree):
- Builds succeed for sandloom, sandloom-standard, sandloom-redis and sandloom-celery.
- Lean kernel (/tmp/rb-lean): imports work, `sandloom --help` works, no redis, celery or kombu import leak (checked sys.modules).
- Standard, redis-only and celery-only installs: entry points in `sandloom.drivers` load; `sandloom --help` works.
- A named-but-missing package gives the B037 hint (rb-nodrv).
- Lockstep pins `sandloom>=0.7.0a13,<0.8` are consistent across the packages.

CI:
- .github/workflows/packages.yml runs check_package_distributions.py and the module-subsets wheel tests.
- ci.yml lanes kernel-without-batteries, kernel-without-redis, redis-drivers and celery-drivers (~776-1180) are sound.

Code and docs:
- Task API upgrade notes (CHANGELOG.md:109-111, packages/sandloom/CHANGELOG.md [Unreleased]) are accurate.
- Celery driver (packages/sandloom-celery/src/sandloom_celery/taskqueue.py): pre-broker versus maybe-published error classification is right. The app is built lazily. The enqueue executor is fork-safe. `isolation_identity` omits credentials.
- ADR-0016 three doors are respected: the driver packages import only `sandloom.driverkit`. The import-linter contracts pass.
- Tier "beta" and contract_version are declared on both drivers.
- The ledger and static-pin tests pass (31 passed).
