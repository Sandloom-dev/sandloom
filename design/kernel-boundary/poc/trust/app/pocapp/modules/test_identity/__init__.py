"""A: a token issuer that is NOT sandloom_identity and is not named ``identity``.

It supplies roles/permissions through the ordinary claims-contributor mechanism.
``POC_ADMINS`` (comma-separated user ids) are given ``roles: ["admin"]``.
``POC_TEST_IDENTITY_CRITICAL=0`` registers the contributor as non-critical.
kb-poc-trust: as the issuer it ends refresh sessions on an emergency block
(answer #16, R2): a durable consumer of ``core.auth.user_blocked.v1``
(``POC_TEST_IDENTITY_NO_BLOCK_CONSUMER=1`` leaves it out).
"""

import os
from typing import Any

from sandloom.sdk import define_module

from . import state


async def contribute(user_id: object) -> dict[str, list[str]]:
    if state.DOWN["on"]:
        raise RuntimeError("test_identity claims source is down")
    admins = {u for u in os.environ.get("POC_ADMINS", "").split(",") if u}
    if str(user_id) in admins:
        return {"roles": ["admin"], "permissions": ["perm.poc.write"]}
    return {"roles": [], "permissions": []}


async def end_sessions_on_block(session: Any, envelope: dict[str, Any]) -> None:
    state.BLOCKED.append(str((envelope.get("payload") or {}).get("user_id")))


def _setup(api: Any) -> None:
    critical = os.environ.get("POC_TEST_IDENTITY_CRITICAL", "1") == "1"
    api.add_claims_contributor(contribute, critical=critical)
    if os.environ.get("POC_TEST_IDENTITY_NO_BLOCK_CONSUMER") != "1":
        api.register_event_consumer(
            "core.auth.user_blocked.v1",
            end_sessions_on_block,
            name="sessions@user_blocked",
            dead_letter=False,
        )


register = define_module(__name__, version="0.1.0", issues_tokens=True, setup=_setup)
