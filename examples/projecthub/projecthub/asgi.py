"""ASGI entry point (the composition root).

Loads every module of ``projecthub.modules`` (found statically: each
subpackage whose ``__init__.py`` defines ``register``), so adding a module
edits nothing here. ``SANDLOOM_MODULES`` selects a subset.

    uvicorn projecthub.asgi:app --reload
"""

from sandloom.sdk.app import (  # sandloom: advanced composition root
    app_modules,
    create_app,
)

app = create_app(default_modules=app_modules("projecthub.modules"))
