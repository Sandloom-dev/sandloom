"""A6 / T-7a name squat: the same test issuer as ``test_identity``, under the
short name ``identity`` (app-local, NOT sandloom_identity).

``POC_IDENTITY_CRITICAL=0`` registers its contributor non-critical;
``POC_IDENTITY_GRANT_ALL=1`` gives EVERY user ``roles: ["admin"]``.
"""

import os
from typing import Any

from sandloom.sdk import define_module


async def contribute(user_id: object) -> dict[str, list[str]]:
    admins = {u for u in os.environ.get("POC_ADMINS", "").split(",") if u}
    if os.environ.get("POC_IDENTITY_GRANT_ALL") == "1" or str(user_id) in admins:
        return {"roles": ["admin"], "permissions": ["perm.poc.write"]}
    return {"roles": [], "permissions": []}


async def _on_block(session: Any, envelope: dict[str, Any]) -> None:
    return None


def _setup(api: Any) -> None:
    critical = os.environ.get("POC_IDENTITY_CRITICAL", "1") == "1"
    api.add_claims_contributor(contribute, critical=critical)
    api.register_event_consumer(
        "core.auth.user_blocked.v1", _on_block, name="sessions@user_blocked"
    )


register = define_module(__name__, version="0.1.0", issues_tokens=True, setup=_setup)
