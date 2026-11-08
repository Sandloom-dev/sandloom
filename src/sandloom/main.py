"""Lean kernel composition root: the kernel app plus every installed module.

The application factory itself is the kernel's
:func:`sandloom.core.app_factory.create_app`; this module only supplies the
default module set: every installed ``sandloom.modules`` entry point (sorted by
module name), each required, then the framework checkout's ``LOCAL_MODULES``.
``SANDLOOM_MODULES`` still chooses an explicit subset. It assigns no claim
authorities and names no module: a plain ``pip install sandloom`` composes
nothing, and an application can write its own authorities without the
batteries.

The standard composition the framework's image and deploy profiles run is
``sandloom_standard.main`` (the ``sandloom-standard`` distribution, which
``sandloom[standard]`` installs; ADR-0012 decision 3): they set
``SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main``. This module stays the
kernel's default root (``sandloom.core.composition``) and the framework
checkout's development root: the module-level ``app`` / ``core`` names are
kept for ``sandloom.main:app``, the Celery worker / ``sandloom-migrate``
default composition root and the tests that import them.
"""

from fastapi import FastAPI

from sandloom.core.app_factory import ContractDiffRequest
from sandloom.core.descriptors.module_names import installed_modules
from sandloom.sdk.app import create_app as _create_kernel_app
from sandloom.sdk.infra import CoreFacade

#: Modules of a framework checkout that no installed distribution registers
#: (``sandloom add module`` in the framework tree appends here). Empty in a
#: release: installed modules are discovered.
LOCAL_MODULES: list[str] = []


def default_modules() -> list[str]:
    """Every installed module's entry-point name (sorted), then LOCAL_MODULES."""
    installed = sorted({module.name for module in installed_modules()})
    return [*installed, *(m for m in LOCAL_MODULES if m not in installed)]


def create_app() -> FastAPI:
    """Build the lean app: the kernel factory + the installed modules."""
    return _create_kernel_app(default_modules=default_modules())


app = create_app()
core: CoreFacade = app.state.core

__all__ = [
    "LOCAL_MODULES",
    "ContractDiffRequest",
    "app",
    "core",
    "create_app",
    "default_modules",
]
