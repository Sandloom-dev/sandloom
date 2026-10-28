"""L2 module-registration primitives of ``sandloom.sdk.module`` (spec §3).

The hand-written L2 form of a module is ``register(core: ModuleAPI) ->
ModuleDescriptor``; :func:`sandloom.sdk.module.define_module` (L1) builds exactly
that function from these names (``test_l1_equals_l2_registration``).
"""

from __future__ import annotations

from sandloom.core.descriptors import (
    CapabilityRequirement,
    ContractRequirement,
    ModuleDescriptor,
)
from sandloom.core.module_api import AUTO, ModuleAPI

__all__ = [
    "AUTO",
    "CapabilityRequirement",
    "ContractRequirement",
    "ModuleAPI",
    "ModuleDescriptor",
]
