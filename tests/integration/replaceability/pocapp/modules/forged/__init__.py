"""A forged / hostile business module: a claims contributor whose answer the
driver sets (``POST /api/v1/forged/set``). It tries to raise rights (roles,
permissions) and grant tenants (tenant_ids, tenant_id). No composition assigns
it any claim (ADR-0014), so it is the violator.

Switches:

- ``POC_FORGED_CRITICAL=1`` registers the contributor critical (trust row T-3);
- ``POC_FORGED_SQUAT=<m>`` also registers the contract ``<m>.claims.v1``
  answering the same payload (T-8: another module's claims contract);
- ``POC_FORGED_FACT=1`` also registers an access fact under ANOTHER module's
  name (``inventory.stock.available``, ``owner_module="inventory"``) that
  always answers true: does anything tie a fact name to its owner?
"""

import os
from typing import Any

from sandloom.sdk import define_module
from sandloom.sdk.authz import MODE_PROJECTION, AccessFact
from sandloom.sdk.contracts import contract_method

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


class SquatClaims:
    """Answers ``<victim>.claims.v1`` with whatever forged is told."""

    @contract_method
    async def contribute(self, user_id: str) -> dict[str, Any]:
        return dict(state.PAYLOAD)


def _setup(api: Any) -> None:
    critical = os.environ.get("POC_FORGED_CRITICAL") == "1"
    api.add_claims_contributor(contribute, critical=critical)
    squat = os.environ.get("POC_FORGED_SQUAT")
    if squat:
        api.register_service(f"{squat}.claims.v1", SquatClaims())
    if os.environ.get("POC_FORGED_FACT") == "1":
        api.register_access_fact(SQUATTED_FACT)


register = define_module(__name__, version="0.1.0", setup=_setup)
