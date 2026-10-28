"""ASGI entry point (the composition root).

Loads every module of ``tasklist.modules`` (found statically: each subpackage
whose ``__init__.py`` defines ``register``) plus the bundled ones, so adding
a module edits nothing here. ``SANDLOOM_MODULES`` selects a subset.

    uvicorn tasklist.asgi:app --reload
"""

from sandloom.sdk.app import (  # sandloom: advanced composition root
    app_modules,
    create_app,
)

BUNDLED: list[str] = []

app = create_app(default_modules=[*BUNDLED, *app_modules("tasklist.modules")])
