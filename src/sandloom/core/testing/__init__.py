"""Public test-harness surface for Sandloom applications and modules.

Module tests import ONLY this package (``sandloom check`` forbids them reaching
kernel internals such as ``sandloom.core._internal`` / ``sandloom.core.sql_alchemy``):

* the pytest plugin :mod:`sandloom.core.testing.plugin` — activate it once per app
  with ``addopts = ["-p", "sandloom.core.testing.plugin"]`` and every module gets the
  ``client`` / ``db`` / ``make_headers`` / ``user_headers`` fixtures with no
  ``conftest.py`` of its own;
* :func:`make_user_headers` — mint an access token for module-level helpers;
  :func:`wait_revocation_listener_subscribed` — wait for this process's
  revocation invalidation listener before warming a budgeted route's copy;
* UoW helpers (:func:`bind_test_uow`, :func:`recording_uow`,
  :func:`committing_uow`, :func:`run_on_commit`, :func:`capture_effects`) and
  :class:`InMemoryCache` for unit tests;
* migration tests (:mod:`sandloom.core.testing.migrations`):
  :func:`temporary_database` (the ``scratch_database`` fixture),
  :func:`upgrade_core_chain`, :func:`migrate_module` and :func:`run_sql`;
  :func:`migration_chains` / :func:`chain_coverage_problems` derive every
  chain from the loaded modules and report tables / indexes their owning
  chain does not create;
* :func:`deliver_event` — drive an event through the registered consumers
  (UoW, durable claim, tenant binding) without a bus;
  :func:`fake_access_fact` — answer an entitlement without its owner module;
* :func:`register_event_schemas` — schemas for channels a test publishes on
  that no module owns (every versioned channel needs one, SANDLOOM-R003);
* :func:`override_instance` — point an infrastructure instance at a test
  backend for a block (restored afterwards);
* :func:`assert_route_snapshot` — a module's routes and their resolved
  policy equal its committed ``routes.snapshot.json``; :func:`route_policy`
  reads the policy the framework applies to a route;
* token-security seams (:mod:`sandloom.core.testing.security`):
  :func:`use_security_cache`, :func:`revocation_mark_key`,
  :func:`block_mark_key`, :func:`record_revocation_checks`,
  :func:`stop_revocation_listener` and :func:`set_environment`;
* :func:`boot_module_alone` — ``create_app`` + lifespan with ONLY one module
  loaded (boot-time coupling; I-19), :class:`BootOutcome`;
* :func:`registration_surface` — record what a module's ``register(core)``
  registers, to pin it in a test;
* pytest-xdist isolation (:mod:`sandloom.core.testing.workers`):
  :func:`reload_test_env` / :func:`worker_env_values` /
  :func:`apply_worker_env` keep each worker on its own database and Redis DBs;
* resource ownership (:mod:`sandloom.core.testing.ownership`):
  :func:`flush_owned_redis` empties a test Redis database this run owns
  (:class:`TestResourceOwnershipError` for another run's).

Names resolve lazily (PEP 562): importing this package — which pytest does
for ``-p sandloom.core.testing.plugin`` before any conftest — must not import
``sandloom.core.config.settings``, whose environment snapshot has to see ``.env.test``.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.testing._uow import (
        Captured,
        InMemoryCache,
        RecordingSession,
        bind_test_uow,
        capture_effects,
        committing_uow,
        recording_uow,
        run_on_commit,
    )
    from sandloom.core.testing.auth import (
        Headers,
        email_of,
        make_user_headers,
        stop_revocation_listener,
        wait_revocation_listener_subscribed,
    )
    from sandloom.core.testing.authz import fake_access_fact
    from sandloom.core.testing.boot import (
        BootOutcome,
        ModuleBootError,
        boot_module_alone,
    )
    from sandloom.core.testing.events import deliver_event, register_event_schemas
    from sandloom.core.testing.infra import override_instance
    from sandloom.core.testing.migrations import (
        MigrationChain,
        chain_coverage_problems,
        migrate_module,
        migration_chains,
        model_tables,
        run_sql,
        tables_created_by,
        temporary_database,
        upgrade_core_chain,
    )
    from sandloom.core.testing.ownership import (
        TestResourceOwnershipError,
        flush_owned_redis,
    )
    from sandloom.core.testing.registration import (
        RegistrationSurface,
        registration_surface,
    )
    from sandloom.core.testing.routes import (
        RoutePolicy,
        assert_route_snapshot,
        route_policy,
    )
    from sandloom.core.testing.security import (
        block_mark_key,
        record_revocation_checks,
        revocation_mark_key,
        set_environment,
        use_security_cache,
    )
    from sandloom.core.testing.workers import (
        apply_worker_env,
        reload_test_env,
        worker_env_values,
    )

_EXPORTS: dict[str, str] = {
    "Captured": "sandloom.core.testing._uow",
    "InMemoryCache": "sandloom.core.testing._uow",
    "RecordingSession": "sandloom.core.testing._uow",
    "bind_test_uow": "sandloom.core.testing._uow",
    "capture_effects": "sandloom.core.testing._uow",
    "committing_uow": "sandloom.core.testing._uow",
    "recording_uow": "sandloom.core.testing._uow",
    "run_on_commit": "sandloom.core.testing._uow",
    "Headers": "sandloom.core.testing.auth",
    "email_of": "sandloom.core.testing.auth",
    "make_user_headers": "sandloom.core.testing.auth",
    "stop_revocation_listener": "sandloom.core.testing.auth",
    "wait_revocation_listener_subscribed": "sandloom.core.testing.auth",
    "BootOutcome": "sandloom.core.testing.boot",
    "boot_module_alone": "sandloom.core.testing.boot",
    "ModuleBootError": "sandloom.core.testing.boot",
    "register_event_schemas": "sandloom.core.testing.events",
    "deliver_event": "sandloom.core.testing.events",
    "fake_access_fact": "sandloom.core.testing.authz",
    "override_instance": "sandloom.core.testing.infra",
    "MigrationChain": "sandloom.core.testing.migrations",
    "chain_coverage_problems": "sandloom.core.testing.migrations",
    "migration_chains": "sandloom.core.testing.migrations",
    "model_tables": "sandloom.core.testing.migrations",
    "tables_created_by": "sandloom.core.testing.migrations",
    "migrate_module": "sandloom.core.testing.migrations",
    "run_sql": "sandloom.core.testing.migrations",
    "temporary_database": "sandloom.core.testing.migrations",
    "upgrade_core_chain": "sandloom.core.testing.migrations",
    "TestResourceOwnershipError": "sandloom.core.testing.ownership",
    "flush_owned_redis": "sandloom.core.testing.ownership",
    "RegistrationSurface": "sandloom.core.testing.registration",
    "RoutePolicy": "sandloom.core.testing.routes",
    "assert_route_snapshot": "sandloom.core.testing.routes",
    "route_policy": "sandloom.core.testing.routes",
    "FakeClock": "sandloom.core.testing.sagas",
    "SagaHarness": "sandloom.core.testing.sagas",
    "SagaRun": "sandloom.core.testing.sagas",
    "SimulatedCrash": "sandloom.core.testing.sagas",
    "open_saga_harness": "sandloom.core.testing.sagas",
    "registration_surface": "sandloom.core.testing.registration",
    "block_mark_key": "sandloom.core.testing.security",
    "record_revocation_checks": "sandloom.core.testing.security",
    "revocation_mark_key": "sandloom.core.testing.security",
    "set_environment": "sandloom.core.testing.security",
    "use_security_cache": "sandloom.core.testing.security",
    "apply_worker_env": "sandloom.core.testing.workers",
    "reload_test_env": "sandloom.core.testing.workers",
    "worker_env_values": "sandloom.core.testing.workers",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})


__all__ = [
    "BootOutcome",
    "Captured",
    "FakeClock",
    "SagaHarness",
    "SagaRun",
    "SimulatedCrash",
    "open_saga_harness",
    "Headers",
    "InMemoryCache",
    "MigrationChain",
    "ModuleBootError",
    "RecordingSession",
    "RegistrationSurface",
    "RoutePolicy",
    "TestResourceOwnershipError",
    "apply_worker_env",
    "assert_route_snapshot",
    "bind_test_uow",
    "boot_module_alone",
    "block_mark_key",
    "capture_effects",
    "chain_coverage_problems",
    "committing_uow",
    "deliver_event",
    "email_of",
    "fake_access_fact",
    "flush_owned_redis",
    "make_user_headers",
    "migrate_module",
    "migration_chains",
    "model_tables",
    "override_instance",
    "record_revocation_checks",
    "recording_uow",
    "register_event_schemas",
    "registration_surface",
    "reload_test_env",
    "revocation_mark_key",
    "route_policy",
    "run_on_commit",
    "run_sql",
    "set_environment",
    "stop_revocation_listener",
    "tables_created_by",
    "temporary_database",
    "upgrade_core_chain",
    "use_security_cache",
    "wait_revocation_listener_subscribed",
    "worker_env_values",
]
