"""A forged / hostile business module: a NON-critical claims contributor whose
answer the driver sets (``POST /api/v1/forged/set``). It tries to raise rights
(roles, permissions) and grant tenants (tenant_ids, tenant_id).

``POC_FORGED_FACT=1`` also registers an access fact under ANOTHER module's
name (``inventory.stock.available``, ``owner_module="inventory"``) that always
answers true: does anything tie a fact name to its owner?
"""

import os
from typing import Any

from sandloom.sdk import define_module
from sandloom.sdk.authz import MODE_PROJECTION, AccessFact

from . import state


async def contribute(user_id: object) -> dict[str, object]:
    return dict(state.PAYLOAD)


async def _always(ctx: Any) -> bool:
    return True


SQUATTED_FACT = AccessFact(
    name="inventory.stock.available",
    owner_module="inventory",
    projection_reader=_always,
    min_freshness=MODE_PROJECTION,
    default_mode=MODE_PROJECTION,
)


def _setup(api: Any) -> None:
    api.add_claims_contributor(contribute)
    if os.environ.get("POC_FORGED_FACT") == "1":
        api.register_access_fact(SQUATTED_FACT)


register = define_module(__name__, version="0.1.0", setup=_setup)
