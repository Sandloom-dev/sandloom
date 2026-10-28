"""The contract call runtime: one code path for local and RPC-served calls
(own Unit of Work, command dedupe, read scope, planned reads)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from functools import partial
from typing import Any

import sandloom.core.module_api._context as _context
import sandloom.core.module_api._dedupe as _dedupe
import sandloom.core.orm as _orm
import sandloom.core.planner as _planner
import sandloom.core.tenant_context as _tenant_context
import sandloom.core.unit_of_work.hooks as _hooks
import sandloom.core.unit_of_work.request as _request
import sandloom.core.unit_of_work.risk as _risk
import sandloom.core.unit_of_work.scope as _scope
from sandloom.core import unit_of_work as uow
from sandloom.core.module_api._bootstrap import (
    _contract_uow_factory,
    _isolated_uow_scope,
    _require_core,
)
from sandloom.core.module_api._dedupe import (
    _NOT_REPLAYED,
    _command_dedupe_ttl,
    _command_log_store,
    _DurableCommandClaim,
    _ReplayRollback,
    _require_idempotency_key,
)
from sandloom.core.module_api._errors import (
    _ReadOnlyRollback,
    read_contract_write_error,
)
from sandloom.core.module_api._read_guard import (
    _guard_read_contract,
    _read_contract_writes,
)
from sandloom.core.module_api._read_scope import _SCOPE_FALLBACK
from sandloom.core.module_api._spec import ContractMethodSpec

# Hot receivers are bound by ASSIGNMENT, not by ``from ... import``: CPython
# (3.12+) compiles ``name.method()`` on an imported name without the
# method-call fast path (it assumes a module), so ``_var.get()`` would build a
# bound method on every call.
_contract_reads_var = _context._contract_reads_var
_contract_session_override = _context._contract_session_override
_contract_session_var = _context._contract_session_var
_read_scope_var = _context._read_scope_var
_CommandDedupe = _dedupe._CommandDedupe


async def run_contract_call(
    func: Callable[..., Awaitable[Any]],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    spec: ContractMethodSpec,
    core: Any = None,
    database: str = "default",
    contract: str | None = None,
) -> Any:
    """Run one contract call in the provider's own Unit of Work.

    The single code path for local and RPC-served contract calls: enforces a
    command's idempotency key and
    deduplicates commands through the provider database's durable command log
    (claimed and completed inside the command's own UoW), opens
    the provider's UoW on ``database`` (rolled back for a read, committed +
    outbox dispatched for a command) and exposes it via
    :func:`contract_session`. ``contract`` is the registered contract name (the
    dedupe namespace; defaults to the method's qualified name).
    """
    return await _run_contract_call(
        func,
        args,
        kwargs,
        spec=spec,
        core=core,
        database=database,
        contract=contract,
    )


async def _run_contract_call(  # noqa: C901
    func: Callable[..., Awaitable[Any]],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    spec: ContractMethodSpec,
    core: Any = None,
    database: str = "default",
    contract: str | None = None,
) -> Any:
    """:func:`run_contract_call`."""
    qualname = getattr(func, "__qualname__", repr(func))
    arguments = _require_idempotency_key(func, spec, args, kwargs)
    if spec.risk is not None:
        # a risk declared at its source (ADR-0009 item 7); the command's own
        # key is its dedupe record
        _risk.note_effect(
            spec.risk, f"command:{contract or qualname}", deduped=True, own_unit=True
        )

    override = _contract_session_override.get()
    if override is not None:
        # Test seam: the test owns the transaction; no own UoW, no dedupe store
        # (a test transaction that rolls back must not leave a replayable
        # result behind).
        token = _contract_session_var.set(override)
        # db() inside the contract is contract_session(): the bound session
        # is the innermost (and only visible) unit, as the provider's own
        # unit is on the normal path (_isolated_uow_scope)
        cur = _scope._current_uow_session.set(override)
        stack = _scope._active_uow_sessions.set((override,))
        reads_token = None
        if spec.planned:
            reads_token = _contract_reads_var.set(_orm.session_reads(override))
        try:
            return await func(*args, **kwargs)
        finally:
            if reads_token is not None:
                _contract_reads_var.reset(reads_token)
            _scope._active_uow_sessions.reset(stack)
            _scope._current_uow_session.reset(cur)
            _contract_session_var.reset(token)

    dedupe: _CommandDedupe | None = None
    if spec.kind == "command" and spec.idempotency_key:
        core = _require_core(core, qualname)
        store = _command_log_store(core, database)
        if store is not None:
            # The default: the dedupe record commits with the command's effect.
            with _tenant_context.configured_tenant_scope(
                getattr(core, "settings", None)
            ):
                durable = _DurableCommandClaim(
                    store,
                    func,
                    args,
                    kwargs,
                    spec=spec,
                    contract=contract or qualname,
                    ttl_seconds=_command_dedupe_ttl(core),
                    arguments=arguments,
                )
            return await _run_in_own_uow(
                func,
                args,
                kwargs,
                spec=spec,
                core=core,
                database=database,
                durable=durable,
            )
        with _tenant_context.configured_tenant_scope(getattr(core, "settings", None)):
            dedupe, replayed, replayed_result = await _CommandDedupe.begin(
                func,
                spec,
                args,
                kwargs,
                contract=contract or qualname,
                ttl_seconds=_command_dedupe_ttl(core),
                arguments=arguments,
            )
        if replayed:
            return replayed_result
    committed = False
    # what the execution leaves on its caller, kept with the dedupe record:
    # written after its on_commit callbacks ran (G-SENS-04)
    recorder = _risk.EffectRecorder() if dedupe is not None else None

    async def _committed(result: Any, pending: list[Any]) -> None:
        nonlocal committed
        committed = True  # before any await: a cancellation never releases it
        if dedupe is not None and recorder is not None:
            fx = recorder.effects(callbacks=pending)
            await _finish_record(
                partial(dedupe.succeeded, result, fx), getattr(func, "__qualname__", "")
            )

    try:
        if recorder is None:
            result = await _run_in_own_uow(
                func, args, kwargs, spec=spec, core=core, database=database
            )
        else:
            with recorder:
                result = await _run_in_own_uow(
                    func,
                    args,
                    kwargs,
                    spec=spec,
                    core=core,
                    database=database,
                    after_commit=_committed,
                )
    except BaseException:
        if dedupe is not None and not committed:
            await dedupe.failed()
        raise
    return result


async def _run_in_own_uow(
    func: Callable[..., Awaitable[Any]],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    spec: ContractMethodSpec,
    core: Any,
    database: str,
    durable: _DurableCommandClaim | None = None,
    after_commit: Callable[[Any, list[Any]], Awaitable[None]] | None = None,
) -> Any:
    """A contract call's own unit, in a FRESH write-admission scope (spec
    §4.4 rule 3): the provider unit is its own writer, never the caller's
    second one."""
    scope = _request.request_scope_get()
    intent = scope.intent if scope is not None else None
    if intent is None:
        with _request.fresh_admission_scope(_request.WriteAdmission()):
            return await _run_contract_unit(
                func,
                args,
                kwargs,
                spec=spec,
                core=core,
                database=database,
                durable=durable,
                after_commit=after_commit,
            )
    # An operation-intent request: the call is refused inside a window and
    # counted while it runs, so a window entered meanwhile waits for its
    # provider unit (G-UNIT-02 / G-BILLING-19).
    token = intent.enter_contract(spec.readonly)
    try:
        with _request.fresh_admission_scope(_request.WriteAdmission()):
            return await _run_contract_unit(
                func,
                args,
                kwargs,
                spec=spec,
                core=core,
                database=database,
                durable=durable,
                after_commit=after_commit,
            )
    finally:
        intent.leave_contract(token)


async def _run_contract_unit(  # noqa: C901
    func: Callable[..., Awaitable[Any]],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    spec: ContractMethodSpec,
    core: Any,
    database: str,
    durable: _DurableCommandClaim | None = None,
    after_commit: Callable[[Any, list[Any]], Awaitable[None]] | None = None,
) -> Any:
    qualname = getattr(func, "__qualname__", repr(func))
    core = _require_core(core, qualname)
    if spec.planned:
        return await _run_planned(func, args, kwargs, core=core, qualname=qualname)
    read_scope = _read_scope_var.get() if spec.readonly else None
    if read_scope is not None:
        outcome = await read_scope.run(
            func, args, kwargs, core=core, database=database, qualname=qualname
        )
        if outcome is not _SCOPE_FALLBACK:
            return outcome
    factory = _contract_uow_factory(core, database)
    unit = factory()
    result: Any = None
    session: Any = None
    #: a durable command's recorder, the effects its record committed with
    #: and the on_commit callbacks queued when it did
    recorder: _risk.EffectRecorder | None = None
    recorded: dict[str, Any] | None = None
    callbacks: list[Any] = []
    try:
        async with unit:
            session = uow._bind_uow_session(unit.session, core, factory, database)
            _context.stamp_caller(session)
            token = _contract_session_var.set(session)
            try:
                with (
                    _isolated_uow_scope(session, database),
                    _tenant_context.configured_tenant_scope(
                        getattr(core, "settings", None)
                    ),
                ):
                    if durable is not None:
                        replay = await durable.claim(session, func)
                        if replay is not _NOT_REPLAYED:
                            raise _ReplayRollback(replay)
                        # what the execution leaves on its caller is kept
                        # with the record: a replay marks the caller alike.
                        # Its on_commit callbacks run after the COMMIT: the
                        # record commits with what they declare, corrected
                        # below once they ran (G-SENS-04)
                        recorder = _risk.EffectRecorder()
                        with recorder:
                            result = await func(*args, **kwargs)
                        callbacks = _hooks.queued_on_commit(session)
                        recorded = recorder.effects(session, callbacks)
                        await durable.complete(session, result, recorded)
                    elif spec.readonly:
                        with _guard_read_contract(
                            session, read_contract_write_error(qualname)
                        ):
                            result = await func(*args, **kwargs)
                    else:
                        result = await func(*args, **kwargs)
                        if after_commit is not None:
                            callbacks = _hooks.queued_on_commit(session)
            finally:
                _contract_session_var.reset(token)
            if spec.readonly:
                wrote = _read_contract_writes(session)
                if wrote is not None:
                    raise read_contract_write_error(qualname)(wrote)
                raise _ReadOnlyRollback
    except _ReadOnlyRollback:
        return result
    except _ReplayRollback as replayed:
        return replayed.value
    if callbacks and durable is not None and recorder is not None:
        # the durable record's effects, corrected with what the callbacks
        # actually left (a closure's; a declared one that did not run), on
        # EVERY exit after the COMMIT (G-SENS-04, round 6): a callback that
        # completed a money effect and was then cancelled (a request
        # timeout) leaves it on the record. When the callbacks did not all
        # finish, the declared effects of the queued ones stay (fail closed:
        # they may have run). Only a process crash skips this and keeps the
        # committed record (the declared callbacks', nothing for a closure)
        finished = False
        try:
            with recorder:
                await uow._dispatch_outbox_after_commit(session)
            finished = True
        finally:
            fx = recorder.effects(callbacks=() if finished else callbacks)
            if recorded is not None and "sr" in recorded:
                fx = {**(fx or {}), "sr": recorded["sr"]}
            if fx != recorded:
                await _finish_record(
                    partial(_amend, durable, factory, recorded, fx), qualname
                )
        return result
    if after_commit is not None:
        # the cache record is written AFTER the on_commit callbacks: what
        # they leave on the caller is part of the replayed effects; even a
        # cancelled dispatch leaves the record (the command committed), with
        # the declared effects of the callbacks that may not have finished
        finished = False
        try:
            await uow._dispatch_outbox_after_commit(session)
            finished = True
        finally:
            await after_commit(result, [] if finished else callbacks)
        return result
    await uow._dispatch_outbox_after_commit(session)
    return result


#: Post-commit dedupe-record writes still running: a strong reference, since
#: a cancelled caller stops waiting for them (``_finish_record``).
_FINISHING: set[asyncio.Task[None]] = set()
#: The bound of one post-commit dedupe-record write, in seconds.
_FINISH_TIMEOUT_S = 5.0


async def _finish_record(write: Callable[[], Awaitable[None]], label: str) -> None:
    """Run ``write`` -- a committed command's dedupe-record write after its
    ``on_commit`` callbacks (G-SENS-04) -- to its end, whatever happens to
    the caller meanwhile: in a task of its own, shielded from the caller's
    cancellation (a request timeout; anyio cancels again at every await,
    so an unshielded ``finally`` would be cut short) and bounded by
    :data:`_FINISH_TIMEOUT_S`. It never raises an ``Exception`` into the
    caller (the command committed): a failure or a timeout is logged and
    the record keeps what it committed with. The caller's own cancellation
    still propagates; it no longer waits for the write then."""

    async def bounded() -> None:
        try:
            async with asyncio.timeout(_FINISH_TIMEOUT_S):
                await write()
        except Exception:  # committed: the record keeps its effects
            logging.getLogger("sandloom.core.contracts").error(
                "could not record what %s's on_commit callbacks left on its "
                "dedupe record; a replay marks the caller with the effects "
                "recorded at its commit",
                label,
                exc_info=True,
            )

    task = asyncio.ensure_future(bounded())
    _FINISHING.add(task)
    task.add_done_callback(_FINISHING.discard)
    await asyncio.shield(task)


async def _amend(
    durable: _DurableCommandClaim,
    factory: Callable[[], Any],
    recorded: dict[str, Any] | None,
    fx: dict[str, Any] | None,
) -> None:
    """The durable record's effects amended, in a unit of its own."""
    with _request.fresh_admission_scope(_request.WriteAdmission()):
        await durable.amend(factory, recorded, fx)


async def _run_planned(
    func: Callable[..., Awaitable[Any]],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    core: Any,
    qualname: str,
) -> Any:
    """A PLANNED read contract: a read-only scope, reads on the contract pool.

    No Unit of Work and no session: the body may only run declared
    operations (:func:`contract_reads`). The scope's general-executor reads
    share one read-only transaction (released when the call returns); the
    optimized executor is used when enabled and eligible. ``publish_event`` /
    ``record_audit`` / ``on_commit`` raise (``ReadOnlyScopeError``). The
    caller's session is never visible: the contract session is unset.
    """

    view = core.runtime_view() if hasattr(core, "runtime_view") else core
    reads = _planner.Reads(view, shared=True, contract=True)
    reads_token = _contract_reads_var.set(reads)
    session_token = _contract_session_var.set(None)
    try:
        with (
            _scope.read_only_scope(f"contract:{qualname}"),
            _tenant_context.configured_tenant_scope(getattr(core, "settings", None)),
        ):
            try:
                result = await func(*args, **kwargs)
            except BaseException:
                with suppress(Exception):
                    await reads.aclose()
                raise
            await reads.aclose()
        return result
    finally:
        _contract_session_var.reset(session_token)
        _contract_reads_var.reset(reads_token)
