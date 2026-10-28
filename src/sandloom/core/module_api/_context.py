"""Context of the running contract call: the provider session, the planned
reads handle, the test-seam override, the call target and the read scope."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import sandloom.core.contract_context as _cc
import sandloom.core.orm as _orm
import sandloom.core.unit_of_work.request as _request

# The variables are defined in the foundation layer (sandloom.core.contract_context)
# so the request-authority boundary resets them without importing this
# package. Bound by assignment, not ``from ... import``: CPython compiles
# ``name.get()`` without the method-call fast path for an imported name.
_contract_session_var = _cc._contract_session_var
_contract_reads_var = _cc._contract_reads_var
_contract_session_override = _cc._contract_session_override
_contract_target = _cc._contract_target
_read_scope_var = _cc._read_scope_var
_isolated_contract_request_scope = _cc._isolated_contract_request_scope


#: ``session.info`` key of a contract unit: the end user the call runs for
#: (:func:`caller_user_id` when the unit was bound), read by providers through
#: ``contract_session().info`` (billing's personal-spend check, spec §7.4).
CALLER_USER_INFO_KEY = "sandloom_contract_caller_user"


def stamp_caller(session: Any) -> None:
    """Record the call's end user on a contract unit's session."""
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        info[CALLER_USER_INFO_KEY] = caller_user_id()


def caller_user_id() -> UUID | None:
    """The end user on whose behalf the running contract call executes, or
    ``None`` (a system / service caller, a task, a consumer).

    Locally the authenticated subject of the calling request; over RPC the
    same subject, carried hop after hop in the signed caller facts of each
    call's context (the provider binds them as its call's request scope). A
    provider holds a personal resource to it (billing's personal spend,
    Public API 1.0 spec §7.4).
    """
    scope = _request.current_request_scope()
    if scope is None or scope.claims is None or not scope.subject:
        return None
    try:
        return UUID(str(scope.subject))
    except ValueError:
        return None


def contract_session() -> Any:
    """The provider-owned session of the contract call currently running.

    Only valid inside a :func:`contract_method` (or a claims contributor)
    invocation; raises ``RuntimeError`` elsewhere — a contract never borrows
    its caller's session.
    """
    session = _contract_session_var.get()
    if session is None:
        if _contract_reads_var.get() is not None:
            raise RuntimeError(
                "contract_session() inside a planned read contract "
                "(@contract_method(planned=True)): it has no Unit of Work; run "
                "declared operations through contract_reads()"
            )
        raise RuntimeError(
            "contract_session() called outside a contract invocation; resolve "
            "the contract through core.get_service(...) or decorate the method "
            "with @contract_method"
        )
    return session


def contract_reads() -> Any:
    """The declared-operations handle (``sandloom.core.orm.Reads``) of the running
    contract call.

    In a PLANNED read contract (``@contract_method(planned=True)``) the reads
    run in the call's read-only scope on the provider's contract pool (one
    shared read-only transaction for general-executor reads; the optimized
    executor when enabled and eligible) — never in the caller's transaction,
    locally and over RPC alike. In any other contract call they run on
    :func:`contract_session`. Under the test seam
    (:func:`bind_contract_session`) they run on the bound test session.
    """
    reads = _contract_reads_var.get()
    if reads is not None:
        return reads

    return _orm.session_reads(contract_session())


def contract_session_bound() -> bool:
    """Whether a test seam session (:func:`bind_contract_session`) is bound.

    Contract calls then all run on that ONE session, so callers must not run
    them concurrently.
    """
    return _contract_session_override.get() is not None


@contextmanager
def bind_contract_session(session: Any) -> Iterator[Any]:
    """Run contract calls in this block on ``session`` (TEST SEAM).

    The contract runtime normally opens the provider's own Unit of Work. A test
    that owns an uncommitted transaction (the ``get_db`` override pattern)
    binds its session here so contracts read the test's data; the framework
    binds it automatically on the dependency-override path. Never use it in
    production code: it reintroduces the shared-transaction semantics the
    runtime exists to remove.
    """
    token = _contract_session_override.set(session)
    try:
        yield session
    finally:
        _contract_session_override.reset(token)
