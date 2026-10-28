"""Context variables of the running contract call (foundation layer).

The provider session, the planned-reads handle, the test-seam override, the
call target and the request read scope of a contract invocation, and the
caller's explicit RPC idempotency key. The contract
runtime (:mod:`sandloom.core.module_api`, which re-exports them) sets and reads
them; the request-authority boundary — the Unit of Work's
``_isolated_request_authority_scope`` and the HTTP
``RequestAuthorityMiddleware`` — resets them for every new HTTP request. They
live at the bottom of the kernel so that boundary never imports the contract
layer to reach them (ADR-0007).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.module_api._read_scope import ContractReadScope


# Session of the contract invocation currently running (see contract_session).
_contract_session_var: ContextVar[Any] = ContextVar(
    "sandloom_contract_session", default=None
)
# Planner reads handle of the PLANNED read contract currently running.
_contract_reads_var: ContextVar[Any] = ContextVar(
    "sandloom_contract_reads", default=None
)
# Test seam: when set, contract calls run on THIS session (the test owns the
# transaction) instead of opening the provider's own UoW. Bound by the
# get_db dependency-override path and by bind_contract_session().
_contract_session_override: ContextVar[Any] = ContextVar(
    "sandloom_contract_session_override", default=None
)
# (core, database, contract name) the next decorated call runs against; set by
# the ContractProvider for exactly one call and consumed by the decorator.
_contract_target: ContextVar[tuple[Any, str, str] | None] = ContextVar(
    "sandloom_contract_target", default=None
)
# Explicit caller idempotency key of the remote calls made in the current
# block (``sandloom.core.rpc.context.rpc_idempotency_key``); reset for every new
# HTTP request by the request-authority boundary.
_rpc_idempotency: ContextVar[str | None] = ContextVar(
    "sandloom_rpc_idempotency_key", default=None
)
# The request-scoped read scope (shared provider connection + memo).
_read_scope_var: ContextVar[ContractReadScope | None] = ContextVar(
    "sandloom_contract_read_scope", default=None
)


@contextmanager
def _isolated_contract_request_scope() -> Iterator[None]:
    """A new HTTP request cannot borrow the caller's provider session or memo."""
    variables: tuple[ContextVar[Any], ...] = (
        _contract_session_var,
        _contract_session_override,
        _contract_target,
        _contract_reads_var,
        _read_scope_var,
    )
    tokens = [(variable, variable.set(None)) for variable in variables]
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
