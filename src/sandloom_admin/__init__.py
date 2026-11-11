"""The platform owner's console: a prebuilt web UI, served at ``/admin/``.

The UI talks only to the modules' public APIs, so this module holds no tables,
events or contracts, and every authorization decision stays with those APIs.
The screens come from the ``admin.py`` declarations of the composed modules:
the console declares ``<app>/shared/admin-manifest.json`` (:mod:`.manifest`),
which ``sandloom generate`` renders from the data the kernel hands it.
At startup it logs how to create the first administrator while none exists
(:mod:`.startup`).
"""

from sandloom.sdk import define_module
from sandloom.sdk.module import ModuleAPI

from .api import PAGE_OFF, PAGE_SERVED, console_enabled, router
from .config import AdminSettings
from .manifest import FILE_NAME, render
from .startup import setup as startup_setup


def setup(core: ModuleAPI) -> None:
    """Mount the console, the set-password page first (the catch-all of the
    console's router would take its path), then the startup check."""
    core.include_router(PAGE_SERVED if console_enabled() else PAGE_OFF)
    core.include_router(router)
    startup_setup(core)


register = define_module(
    __name__,
    version="1.0.0",
    router=None,
    setup=setup,
    generates={FILE_NAME: render},
    settings=AdminSettings,
)

__all__ = ["register"]
