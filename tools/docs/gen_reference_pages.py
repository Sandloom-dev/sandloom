"""Generate the route reference page before the Zensical build.

Used by ``tools/docs/docs_reference.py``:

* ``reference/routes.md`` — every route of the batteries platform with its
  resolved route policy (model E, ADR-0009), built from the composed app
  (module routes as approved in each module's route snapshot, kernel routes as
  the kernel mounts them). Same text as ``sandloom routes reference``.

Generated and committed so both readers and the site builder see the same reference. The
error catalogue and the ``sandloom.sdk`` reference are committed pages generated
by ``tools/docs/docs_reference.py`` (agents read them on disk).
"""

from __future__ import annotations

import os

_DOCS_BUILD_ENV = {
    "ENVIRONMENT": "dev",
    "DATABASE_URL": "postgresql+asyncpg://docs:docs@localhost:5432/docs",
    "CACHE_URL": "redis://localhost:6379/0",
    "BROKER_URL": "redis://localhost:6379/1",
    "JWT_SECRET": "docs-build-placeholder-secret-not-used-at-runtime",
}
for _key, _value in _DOCS_BUILD_ENV.items():
    os.environ.setdefault(_key, _value)

def render_pages() -> dict[str, str]:
    from sandloom.core import route_snapshots
    from sandloom.main import app

    return {"reference/routes.md": route_snapshots.render_reference(app)}
