"""kb-research-settings, probe 1: where do module prod refusals fire?

Prod, identity + billing loaded, TWOFA_ENCRYPTION_KEY and BILLING_PROVIDER unset.
create_app() is what the worker, `sandloom migrate` and the operator CLIs run
(load_composition_core); only the ASGI lifespan runs startup tasks.
No database or Redis is contacted by create_app (ports point nowhere).
"""

import asyncio
import os
import sys

os.environ.clear()
os.environ.update(
    {
        "PATH": "/usr/bin:/bin",
        "ENVIRONMENT": "prod",
        "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:9/db",
        "CACHE_URL": "redis://127.0.0.1:9/9",
        "BROKER_URL": "redis://127.0.0.1:9/9",
        "JWT_SECRET": "c1b4f0e97a2d4e6b8f3a5c7d9e1f2a3b4c5d6e7f8a9b0c1d",
        "JWT_ISSUER": "probe",
        "JWT_AUDIENCE": "probe",
        "CORS_ORIGINS": '["https://probe.example"]',
        "SANDLOOM_MODULES": '["sandloom_identity", "sandloom_tenancy", "sandloom_billing"]',
        "OUTBOX_RELAY_ENABLED": "on",
    }
)
from sandloom.core.app_factory import create_app  # noqa: E402

try:
    app = create_app()
except BaseException as exc:  # noqa: BLE001
    print("create_app() refused:", type(exc).__name__, str(exc)[:300])
    sys.exit(0)
core = app.state.core
print("create_app() (worker / migrate / CLI path) -> built; modules:", core.loaded_modules)
critical = [t.__name__ for t, crit in core.startup_tasks_with_criticality() if crit] if hasattr(core, "startup_tasks_with_criticality") else None
if critical is None:
    critical = [t.__name__ for t, crit in zip(core._startup_tasks, core._startup_critical) if crit]
print("critical startup tasks registered (run only in the ASGI lifespan):",
      [n for n in critical if n.startswith("_refuse")])

try:
    from sandloom.core.worker import build_worker_app

    worker = build_worker_app(core)
    print("build_worker_app(core) (celery worker / beat path) -> built:", type(worker).__name__)
except BaseException as exc:  # noqa: BLE001
    print("build_worker_app refused:", type(exc).__name__, str(exc)[:200])


async def lifespan() -> None:
    async with app.router.lifespan_context(app):
        pass


try:
    asyncio.run(lifespan())
    print("lifespan -> started")
except BaseException as exc:  # noqa: BLE001
    print("lifespan (API path) refused:", type(exc).__name__, str(exc)[:200])
