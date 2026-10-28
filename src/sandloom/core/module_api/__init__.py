"""ModuleAPI — the narrow surface handed to a module's ``register(core)``.

A business module must not see the whole :class:`CoreFacade` (infra registry,
UoW factory, event bus, the raw FastAPI app, metrics internals). It needs only
what it uses to register itself: routers, sync service contracts, event
schemas, lifecycle hooks, health checks, logging, and read-only settings.

``ModuleAPI`` exposes exactly that, delegating to the facade. Narrowing the
contract keeps modules isolated and lets the kernel evolve its internals
without touching every ``register`` function — the first concrete step of the
facade decomposition.

Service contracts (the configurable-monolith promise)
-----------------------------------------------------
This module also owns the **contract runtime** — the single code path every
sync service-contract call goes through, whether the caller is in-process
(``core.get_service(name).method(...)`` in a monolith) or remote (the internal
RPC server invoking the registered provider for a generated proxy):

* A contract method NEVER takes the caller's database session. The provider
  opens its OWN Unit of Work on ITS OWN database (the one its module declares)
  for every call, so the call has identical semantics locally and over RPC.
  Inside the method, :func:`contract_session` returns that session.
* :func:`contract_method` marks a method as a contract entry point. A *read*
  contract (the default) runs in a transaction that is always rolled back (it
  may not publish events). A *command* contract (``command=True``) commits its
  own UoW, must name an ``idempotency_key`` argument the caller supplies, and
  is **not atomic with the caller's transaction** — saga semantics: if the
  caller later rolls back, it must compensate (or drive the effect from an
  event instead).
* **Commands are deduplicated by the framework — durably.** The runtime
  records ``(contract, method, bound tenant, idempotency key) -> (args
  digest, status, encoded result)`` in the PROVIDER's database
  (``contract_command_log``, core chain), claimed in the command's OWN Unit of
  Work before the body runs and completed before the commit — so the record
  commits iff the business change commits (no crash window). A repeat with the
  same key and arguments REPLAYS the stored result without running the method
  again; the same key with different arguments is rejected
  (:class:`ContractIdempotencyConflict`, 409); a repeat while the first call
  still runs WAITS for it (then replays, or runs if it failed) up to the
  contract pool's ``lock_timeout`` (``CONTRACT_DB_LOCK_TIMEOUT_MS``), beyond
  which it gets :class:`ContractCallInProgress` (503, retry later); a failed
  call rolls its record back, so a retry runs afresh. Records live
  ``CONTRACT_COMMAND_DEDUPE_TTL_SECONDS`` (then the key runs afresh; the relay
  housekeeping purges them). Local and RPC-served calls share this path. (The
  test seam :func:`bind_contract_session` bypasses the log — the test owns the
  transaction.) Only a database backend WITHOUT a durable command log
  (``command_idempotency`` feature) falls back to the fail-closed cache dedupe
  (:class:`ContractIdempotencyUnavailable` when the cache is down) — that
  fallback is not exactly-once across a crash.
* A contract method never takes a session (a session argument is an unknown
  argument: ``422`` :class:`ContractArgumentError`, like over RPC); it reads
  the provider's own via :func:`contract_session`.
* **Local == remote.** A provider is validated at registration
  (:func:`contract_schema`): every public member must be a contract method
  with a wire-portable signature (:mod:`sandloom.core.contract_wire`; no
  ``*args``/``**kwargs``), anything else must be private. A local call is bound
  with Python's rules, its arguments and result round-trip through the SAME
  wire codec as RPC and are validated against the annotations, and provider
  ``HTTPException`` / ``http_status`` errors surface as
  :class:`ContractCallError` (same status, detail, headers, code) — locally
  exactly as over RPC.

Provider pattern::

    from sandloom.core.module_api import contract_method, contract_session

    class MembershipReadContract:
        @classmethod
        @contract_method
        async def get_role(cls, organization_id: UUID, user_id: UUID) -> str | None:
            return await MembershipService.get_role(
                organization_id, user_id, contract_session()
            )

    class CreditsContract:
        @classmethod
        @contract_method(command=True, idempotency_key="operation_key")
        async def spend(cls, tenant_type: str, tenant_id: UUID, amount: int,
                        operation_key: str) -> int: ...

Package layout (internal — import only from ``sandloom.core.module_api``)
--------------------------------------------------------------------
``_registration``  ``ModuleAPI`` + the transactional registration draft
``_define``        ``define_module`` (declarative ``register``)
``_spec``          ``ContractMethodSpec``, session-parameter helpers
``_method``        the ``@contract_method`` decorator
``_context``       context variables of the running call, ``contract_session``
``_bootstrap``     contract-session bootstrap (contract UoW factory, isolated
                   UoW scope, active core)
``_call``          ``run_contract_call`` — own UoW, dedupe, planned reads
``_dedupe``        durable command log claim + cache fallback
``_read_scope``    ``ContractReadScope`` (shared read connection + memo)
``_errors``        ``ContractCallError`` and the framework error classes
``_schema``        ``ContractMethodRuntime`` / ``ContractSchema`` (describe)
``_provider``      ``contract_schema``, DTO checks, ``ContractProvider``
"""

from sandloom.core.descriptors import (
    BACKWARD,
    FORWARD,
    Saga,
    SagaContext,
    SagaDefinitionError,
    StepKind,
    local_step,
    operation_key,
    step,
)
from sandloom.core.module_api._call import run_contract_call
from sandloom.core.module_api._context import (
    bind_contract_session,
    contract_reads,
    contract_session,
    contract_session_bound,
)
from sandloom.core.module_api._context import (
    caller_user_id as caller_user_id,
)
from sandloom.core.module_api._define import AUTO, define_module
from sandloom.core.module_api._errors import (
    ContractArgumentError,
    ContractCallError,
    ContractCallInProgress,
    ContractIdempotencyConflict,
    ContractIdempotencyKeyMissing,
    ContractIdempotencyUnavailable,
    ContractResultNotReplayable,
    ReadContractWriteError,
    contract_error,
    normalize_contract_error,
)
from sandloom.core.module_api._method import contract_method
from sandloom.core.module_api._provider import (
    DEFAULT_CONTRACT_DTO_PACKAGES,
    ContractProvider,
    bind_contract,
    contract_dto_problems,
    contract_schema,
)
from sandloom.core.module_api._read_scope import (
    ContractReadScope,
    ContractReadScopeMiddleware,
    contract_read_scope,
    current_contract_read_scope,
    invalidate_contract_read_memo,
)
from sandloom.core.module_api._registration import (
    ModuleAPI,
    ModuleRegistrationConflict,
    ModuleRegistrationDraft,
)
from sandloom.core.module_api._schema import ContractMethodRuntime, ContractSchema
from sandloom.core.module_api._spec import CONTRACT_SPEC_ATTR as CONTRACT_SPEC_ATTR
from sandloom.core.module_api._spec import SESSION_PARAM_NAMES as SESSION_PARAM_NAMES
from sandloom.core.module_api._spec import (
    ContractMethodSpec,
    contract_kind,
    contract_spec,
)
from sandloom.core.module_api._spec import session_parameters as session_parameters

__all__ = [
    "DEFAULT_CONTRACT_DTO_PACKAGES",
    "contract_dto_problems",
    "AUTO",
    "define_module",
    "ModuleAPI",
    "ModuleRegistrationDraft",
    "ModuleRegistrationConflict",
    "ContractArgumentError",
    "ContractCallError",
    "ContractMethodRuntime",
    "ContractMethodSpec",
    "ContractIdempotencyKeyMissing",
    "ContractIdempotencyConflict",
    "ContractCallInProgress",
    "ContractIdempotencyUnavailable",
    "ContractProvider",
    "ContractReadScope",
    "ContractReadScopeMiddleware",
    "ContractResultNotReplayable",
    "ContractSchema",
    "ReadContractWriteError",
    "bind_contract",
    "bind_contract_session",
    "contract_reads",
    "contract_error",
    "contract_method",
    "contract_read_scope",
    "contract_session_bound",
    "current_contract_read_scope",
    "invalidate_contract_read_memo",
    "contract_schema",
    "contract_session",
    "contract_kind",
    "contract_spec",
    "normalize_contract_error",
    "run_contract_call",
    # Sagas (ADR-0008): the definition DSL, re-exported from sandloom.core.descriptors
    "BACKWARD",
    "FORWARD",
    "Saga",
    "SagaContext",
    "SagaDefinitionError",
    "StepKind",
    "local_step",
    "operation_key",
    "step",
]
