"""ASGI entry point (the composition root).

Loads the installed identity, tenancy and billing modules plus every module of
``helpdesk.modules`` (found statically: each subpackage whose
``__init__.py`` defines ``register``), so adding a module edits nothing
here. ``SANDLOOM_MODULES`` selects a subset.

    uvicorn helpdesk.asgi:app --reload
"""

from sandloom.sdk.app import (  # sandloom: advanced composition root
    app_modules,
    create_app,
)

BUNDLED: list[str] = ["identity", "tenancy", "billing"]

app = create_app(default_modules=[*BUNDLED, *app_modules("helpdesk.modules")])
