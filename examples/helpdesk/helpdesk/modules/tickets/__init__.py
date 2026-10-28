"""The tickets module.

Everything it declares is discovered: routes in ``api/``, the ticket events
in ``events.py``, the table in ``models/``. Organisation membership comes
from the tenancy module's ``tenancy.membership.v1`` contract — never an
import. The planner's read operations have no declaration, so a short
``setup`` registers them.
"""

from sandloom.sdk import define_module
from sandloom.sdk.module import (  # sandloom: advanced setup, explicit requirement
    ContractRequirement,
    ModuleAPI,
)

from .services.operations import OPERATIONS


def _setup(core: ModuleAPI) -> None:
    # Declared planner reads (the ticket lists): planned at boot.
    core.register_operations(*OPERATIONS)


register = define_module(
    __name__,
    version="0.1.0",
    setup=_setup,
    # Org membership checks; the endpoints degrade to 501 without it. Stated
    # explicitly while tenancy (an L2 provider) has no generated stub to
    # declare a module-level Contract(TENANCY_MEMBERSHIP_V1, optional=True).
    consumes=[ContractRequirement("tenancy.membership.v1", required=False)],
)
