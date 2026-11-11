"""Shared PoC mint helper: works on the base kernel (create_access_token takes
any dict) and on the TA-1 kernel (protected claims only through the sealed
``collect_claims`` result, ``claims=``). Not a module (no ``register``)."""

import inspect
from typing import Any
from uuid import UUID

from sandloom.sdk.security import create_access_token

SEALED = "claims" in inspect.signature(create_access_token).parameters


async def mint(core: Any, user_id: UUID) -> tuple[str, dict[str, Any]]:
    claims = await core.collect_claims(user_id)
    system = {"sub": str(user_id), "token_type": "access"}
    if SEALED:
        token = create_access_token(data=system, claims=claims)
    else:
        token = create_access_token(data={**claims, **system})
    return token, dict(claims)
