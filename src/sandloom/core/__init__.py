"""The Sandloom kernel.

The package exports nothing: import the submodule that owns a name.
Business modules use the public core API only (``sandloom.core.di``,
``sandloom.core.facade``, ``sandloom.core.module_api``, ``sandloom.core.descriptors``,
``sandloom.core.orm``, ``sandloom.core.projections``); infrastructure is reached through
``sandloom.core.di`` and the capability registry — never via concrete-backend
globals. Importing a submodule costs only that submodule, so the static tools
(``sandloom add``, ``sandloom new``, ``sandloom check``) never build ``settings``.
"""
