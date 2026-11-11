"""kb-research-settings, probe 2b: create_app() wall time (dev, three bundled modules)."""
import os
import time

os.environ.clear()
os.environ.update({"PATH": "/usr/bin:/bin", "ENVIRONMENT": "test",
                   "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:9/db",
                   "CACHE_URL": "redis://127.0.0.1:9/9", "BROKER_URL": "redis://127.0.0.1:9/9",
                   "JWT_SECRET": "probe-not-a-secret-0123456789abcdef0123",
                   "SANDLOOM_MODULES": '["sandloom_identity", "sandloom_tenancy", "sandloom_billing"]'})
t0 = time.perf_counter()
from sandloom.core.app_factory import create_app  # noqa: E402
from sandloom.core.config.settings import load_settings  # noqa: E402
t1 = time.perf_counter()
load_settings()
t2 = time.perf_counter()
create_app()
t3 = time.perf_counter()
print(f"import app_factory: {(t1-t0)*1000:.0f} ms; load_settings: {(t2-t1)*1000:.0f} ms; "
      f"create_app (settings already loaded): {(t3-t2)*1000:.0f} ms")
