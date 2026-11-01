"""The platform owner's console: a prebuilt web UI, served at ``/admin/``.

The UI talks only to the modules' public APIs, so this module holds no tables,
events or contracts, and every authorization decision stays with those APIs.
The screens come from the ``admin.py`` declarations of the composed modules,
which ``sandloom generate`` compiles to ``<app>/shared/admin-manifest.json``.
"""

from sandloom.sdk import define_module

register = define_module(__name__, version="1.0.0")

__all__ = ["register"]
