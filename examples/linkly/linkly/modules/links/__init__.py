"""The links module.

Everything it declares is discovered: routes in ``api/``, the
``LinkCreated`` event in ``events.py``, the table in ``models/``. The one
thing without a declaration, the planner's read operations, is registered in
a short ``setup``.
"""

from sandloom.sdk import define_module
from sandloom.sdk.module import (
    ModuleAPI,  # sandloom: advanced setup registers planner reads
)

from .services.operations import OPERATIONS


def _setup(core: ModuleAPI) -> None:
    # Declared planner reads (the redirect lookup): planned at boot.
    core.register_operations(*OPERATIONS)


register = define_module(__name__, version="0.1.0", setup=_setup)
