"""B: an independent tenant-grant provider (not sandloom_tenancy).

Supplies the kernel's ``tenant_ids`` grant through a CRITICAL claims
contributor; grants live in process memory (the protocol is under test, not
storage). Revoking a grant revokes the user's tokens on commit.
"""

from sandloom.sdk import define_module

from . import state


async def contribute(user_id: object) -> dict[str, list[str]]:
    if state.DOWN["on"]:
        raise RuntimeError("tenant-grant provider is down")
    return {"tenant_ids": sorted(state.GRANTS.get(str(user_id), set()))}


def _setup(api: object) -> None:
    api.add_claims_contributor(contribute, critical=True)  # type: ignore[attr-defined]


register = define_module(__name__, version="0.1.0", setup=_setup)
