"""Standard composition root: the kernel app plus every installed module.

The root the framework's image, shipped compose profiles, Celery worker and
beat, ``sandloom migrate`` and the composed operator commands name with
``SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main`` (ADR-0012 decision 3), so
every entry point of a deployment builds the same composition. The kernel's
own ``sandloom.main`` stays the lean root and names no battery.

The composition is the kernel factory with every installed ``sandloom.modules``
entry point (sorted by module name) as the default module set, each required;
``SANDLOOM_MODULES`` still chooses an explicit subset. ``pip install
"sandloom[standard]"`` installs identity, tenancy and billing, so
``sandloom_standard.main:app`` serves all three.
"""

from fastapi import FastAPI

from sandloom.core.descriptors.module_names import installed_modules
from sandloom.sdk.app import create_app as _create_kernel_app
from sandloom.sdk.infra import CoreFacade


def standard_modules() -> list[str]:
    """Every installed module's entry-point name, sorted: the default set."""
    return sorted({module.name for module in installed_modules()})


def create_app() -> FastAPI:
    """Build the standard app: the kernel factory + the installed modules."""
    return _create_kernel_app(default_modules=standard_modules())


app = create_app()
core: CoreFacade = app.state.core

__all__ = [
    "app",
    "core",
    "create_app",
    "standard_modules",
]
