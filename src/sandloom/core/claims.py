"""Token claims contributors.

A module declares what it wants embedded in a freshly minted access token by
registering a *claims contributor* — ``async (user_id) -> dict`` — via
``core.add_claims_contributor``. The contributor reads its module's data
through ``contract_session()`` (the module's own Unit of Work). At mint time
(login / refresh, the cold path) the kernel runs every contributor in its
owning module's own Unit of Work (remote ones over RPC as
``<module>.claims.v1``) and merges the results into the JWT payload. This
keeps module isolation intact: ``identity`` mints the token but never imports
``billing``; ``billing`` simply contributes its own ``subscription_status`` /
``plan_code`` claims.

Contributors run on the cold path only, so the per-request authorization path
stays pure claim reads (zero I/O). A failing non-critical contributor is logged
and recorded in ``claims_sources_failed`` (its claims are absent and the
matching claim atoms escalate or fail closed); a failing *critical* one aborts
minting with ``ClaimsUnavailable`` (503). See ``CoreFacade.collect_claims``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

#: The current contributor signature: ``async (user_id) -> dict`` of
#: JSON-serializable claim fields; data is read via ``contract_session()``.
ClaimsContributor = Callable[[UUID], Awaitable[dict[str, Any]]]
