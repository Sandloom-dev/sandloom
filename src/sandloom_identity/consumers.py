"""Identity's event consumers on kernel channels (registered in ``setup``).

``core.auth.user_blocked.v1`` and ``core.auth.tokens_revoked.v1`` are kernel
channels: identity consumes them with the L2 handler signature
``(session, envelope)`` because the handlers work in the delivery's own unit.
The tokens-revoked applier lives in :mod:`sandloom_identity.sessions`.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sandloom_identity.services.token_service import TokenService

logger = logging.getLogger("sandloom_identity.sessions")


async def revoke_refresh_tokens_on_block(
    session: Any, envelope: dict[str, Any]
) -> None:
    """``core.auth.user_blocked.v1`` consumer: revoke every refresh token.

    ``block_user`` already revoked the access tokens (kernel mark) and blocks
    every request and issuance while the block lasts; this makes the block
    END the user's sessions, so unblocking requires a fresh login.
    """
    payload = envelope.get("payload") or {}
    raw = payload.get("user_id")
    try:
        user_id = UUID(str(raw))
    except (TypeError, ValueError):
        logger.warning("user_blocked event without a valid user_id: %r", raw)
        return
    await TokenService.revoke_all_user_tokens(user_id, session)


__all__ = ["revoke_refresh_tokens_on_block"]
