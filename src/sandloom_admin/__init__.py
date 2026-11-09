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

from .config import AdminSettings
from .manifest import FILE_NAME, render
from .startup import setup

register = define_module(
    __name__,
    version="1.0.0",
    setup=setup,
    generates={FILE_NAME: render},
    settings=AdminSettings,
)

__all__ = ["register"]
