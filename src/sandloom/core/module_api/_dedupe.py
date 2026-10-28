"""Command dedupe: the durable command log claim (default) and the cache-only
fallback, plus the idempotency-key / argument-digest helpers."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

import orjson

import sandloom.core.infra.contracts as _contracts
import sandloom.core.module_api._context as _context
import sandloom.core.tenant_context as _tenant_context
import sandloom.core.unit_of_work.risk as _risk
from sandloom.core import contract_wire, effects
from sandloom.core.idempotency import fingerprint as idempotency_fingerprint
from sandloom.core.idempotency import reservation as idempotency_reservation
from sandloom.core.module_api._errors import (
    ContractCallInProgress,
    ContractIdempotencyConflict,
    ContractIdempotencyKeyMissing,
    ContractIdempotencyUnavailable,
    ContractResultNotReplayable,
)
from sandloom.core.module_api._spec import (
    SESSION_PARAM_NAMES,
    ContractMethodSpec,
    _return_annotation_of,
    _signature_of,
)


def _require_idempotency_key(
    func: Callable[..., Any],
    spec: ContractMethodSpec,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any] | None:
    """Check a command's idempotency key; return its bound arguments.

    ``None`` for a method without a key. Otherwise the call is bound ONCE
    and the result is the command's dedupe arguments (defaults applied, no
    ``cls``/``self``/session — see :func:`_command_arguments`).
    """
    key_name = spec.idempotency_key
    if key_name is None:
        return None
    try:
        bound = _signature_of(func).bind_partial(*args, **kwargs)
    except TypeError as exc:
        raise ContractIdempotencyKeyMissing(str(exc)) from exc
    value = bound.arguments.get(key_name)
    if not isinstance(value, str) or not value.strip():
        raise ContractIdempotencyKeyMissing(
            f"command contract {func.__qualname__!r} requires a non-empty "
            f"{key_name!r} (idempotency/operation key)"
        )
    return _filtered_arguments(bound)


#: Default for how long a completed command's result is replayable for its
#: idempotency key (``CONTRACT_COMMAND_DEDUPE_TTL_SECONDS`` overrides it).
COMMAND_DEDUPE_TTL_SECONDS = effects.RECORD_TTL_SECONDS
#: Largest encoded command result kept for replay; a bigger one is stored as a
#: non-replayable marker (the retry gets :class:`ContractResultNotReplayable`,
#: the effect is never re-executed) — same cap as the HTTP idempotency guard.
COMMAND_RESULT_REPLAY_MAX_BYTES = effects.RESULT_MAX_BYTES


def _command_dedupe_ttl(core: Any) -> int:
    raw = getattr(
        getattr(core, "settings", None), "contract_command_dedupe_ttl_seconds", None
    )
    return effects.ttl_or_default(raw, COMMAND_DEDUPE_TTL_SECONDS)


def _command_arguments(
    func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """The call's bound arguments (defaults applied; no ``cls``/``self``/session)."""
    return _filtered_arguments(_signature_of(func).bind_partial(*args, **kwargs))


def _filtered_arguments(bound: inspect.BoundArguments) -> dict[str, Any]:
    bound.apply_defaults()
    return {
        name: value
        for name, value in bound.arguments.items()
        if name not in {"cls", "self"} and name not in SESSION_PARAM_NAMES
    }


def _args_digest(arguments: dict[str, Any]) -> str:
    """Stable digest of a command's arguments (same key + other args -> 409).

    The dedupe record's args digest (:func:`sandloom.core.effects.args_digest`):
    the canonical wire form (sets / non-string-keyed maps sorted), identical
    across processes regardless of hash randomization.
    """
    return effects.args_digest(arguments)


def _method_label(contract: str, func: Callable[..., Any]) -> str:
    return f"{contract}.{getattr(func, '__name__', '?')}"


def _command_scope_tag(spec: ContractMethodSpec, arguments: dict[str, Any]) -> str:
    """The dedupe scope beyond (method, tenant, key): the end user the call
    runs for, then the declared ``idempotency_scope`` arguments.

    The caller is part of the scope because a stored result was produced
    under ITS authority: a command authorizes its caller in its body
    (billing's personal spend), so a replay keyed without the caller would
    hand that result to anyone who learned the key, skipping the body and
    with it the authorization (G-BILLING-20). Keyed by the caller, another
    caller's retry runs the body and gets its own decision; the same caller's
    retry, local or remote, still replays. A system caller (no end user) is
    one principal, as before."""
    caller = _context.caller_user_id()
    tag = "" if caller is None else f":caller:{caller}"
    if not spec.idempotency_scope:
        return tag
    return (
        tag
        + ":scope:"
        + _args_digest({name: arguments[name] for name in spec.idempotency_scope})
    )


class _CommandDedupe:
    """Cache-only command dedupe — the FALLBACK for a database without a durable
    command log (see :class:`_DurableCommandClaim`, the default). Not
    exactly-once across a crash between the commit and the cache write."""

    def __init__(self, reservation: Any) -> None:
        self.reservation = reservation

    @staticmethod
    async def begin(
        func: Callable[..., Any],
        spec: ContractMethodSpec,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        contract: str,
        ttl_seconds: int,
        arguments: dict[str, Any] | None = None,
    ) -> tuple[_CommandDedupe | None, bool, Any]:
        """Reserve the key: ``(dedupe, replayed, replayed_result)``."""

        if arguments is None:
            arguments = _command_arguments(func, args, kwargs)
        key = str(arguments.get(spec.idempotency_key or "", ""))
        tenant = _tenant_context.current_tenant_id_optional()
        key_tag = effects.tag(key, 40)
        label = _method_label(contract, func)
        scope_tag = _command_scope_tag(spec, arguments)
        full_key = f"idem:contract:{label}:{tenant or '-'}{scope_tag}:{key_tag}"
        try:
            outcome, value = await idempotency_reservation.reserve_key(
                full_key, _args_digest(arguments), ttl_seconds
            )
        except idempotency_fingerprint._CacheUnavailable as exc:
            raise ContractIdempotencyUnavailable(
                f"command contract {label} refused: the idempotency store is "
                "unavailable (fail-closed)"
            ) from exc
        if outcome == "replay":
            stored = value if isinstance(value, dict) else {}
            # the first execution's effects mark this caller alike
            _risk.apply_call_effects(stored.get("fx"))
            if stored.get("truncated"):
                raise ContractResultNotReplayable(
                    f"{label} with this idempotency key already completed; its "
                    "result was too large to keep for replay"
                )
            return None, True, _decode_result(func, stored.get("result"))
        if outcome == "mismatch":
            raise ContractIdempotencyConflict(
                f"idempotency key {key!r} was already used for {label} with "
                "different arguments"
            )
        if outcome != "reserved":
            raise ContractCallInProgress(
                f"a {label} call with this idempotency key is still in progress; "
                "retry later"
            )
        return _CommandDedupe(value), False, None

    async def succeeded(self, result: Any, fx: dict[str, Any] | None = None) -> None:
        """Persist the result and the call effects ``fx`` it left on its
        caller (re-applied by a replay)."""
        response: dict[str, Any]
        status, encoded = effects.replay_outcome(result)
        if status != effects.SUCCEEDED or encoded is None:
            response = {"truncated": True}
        else:
            response = {"result": orjson.loads(encoded)}
        if fx:
            response["fx"] = fx
        record = {
            "state": "succeeded",
            "fp": self.reservation.fingerprint,
            "response": response,
        }
        await idempotency_reservation._persist_terminal(self.reservation, record)

    async def failed(self) -> None:
        await idempotency_reservation._release_key(self.reservation)


_no_durable_log_warned: set[str] = set()


def _command_log_store(core: Any, database: str) -> Any:
    """The provider database's durable command log, or ``None``.

    ``None`` (cache-only dedupe) only for a database backend that does not
    implement :meth:`DatabaseProvider.command_log` — never for the built-in
    relational driver.
    """
    getter = getattr(core, "database", None)
    if not callable(getter):
        return None
    provider = getter(database)
    command_log = getattr(provider, "command_log", None)
    store = command_log() if callable(command_log) else None
    if store is None and database not in _no_durable_log_warned:
        _no_durable_log_warned.add(database)
        logging.getLogger("sandloom.core.contracts").warning(
            "database %r has no durable command log (feature "
            "'command_idempotency'): command contracts on it are deduped by the "
            "cache only, which is NOT exactly-once across a crash",
            database,
        )
    return store


class _DurableCommandClaim:
    """Command dedupe recorded IN the command's own Unit of Work (source of truth).

    ``(contract.method, bound tenant, idempotency key)`` is claimed in the
    provider database before the body runs and completed with the encoded
    result before the commit, so the record commits iff the command's effect
    commits. A crash anywhere after the commit leaves a record a retry replays;
    a failed command leaves none, so a retry runs afresh.

    The record also keeps the call effects the execution left on its
    caller's request (:class:`sandloom.core.unit_of_work.risk.EffectRecorder`): a
    replay re-applies them, so it marks the caller exactly like the first
    execution (risks and their audit obligation, the subject's committed
    self-revocation, pending step-ups; never a re-authentication nor a call
    left in doubt).
    """

    __slots__ = ("store", "dedupe_key", "label", "tenant", "digest", "key", "ttl")

    def __init__(
        self,
        store: Any,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        spec: ContractMethodSpec,
        contract: str,
        ttl_seconds: int,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        if arguments is None:
            arguments = _command_arguments(func, args, kwargs)
        self.store = store
        self.key = str(arguments.get(spec.idempotency_key or "", ""))
        bound = _tenant_context.current_tenant_id_optional()
        self.tenant = str(bound) if bound is not None else None
        self.label = _method_label(contract, func)
        self.digest = _args_digest(arguments)
        self.ttl = ttl_seconds
        self.dedupe_key = effects.key_digest(
            self.label,
            (self.tenant or "-") + _command_scope_tag(spec, arguments),
            self.key,
        )

    async def claim(self, session: Any, func: Callable[..., Any]) -> Any:
        """Claim the key; ``_NOT_REPLAYED`` to run the body, else the replay."""

        try:
            outcome = await self.store.claim(
                session,
                dedupe_key=self.dedupe_key,
                contract=self.label,
                tenant=self.tenant,
                args_digest=self.digest,
                ttl_seconds=self.ttl,
            )
        except _contracts.CommandLogBusy as exc:
            raise ContractCallInProgress(
                f"a {self.label} call with this idempotency key is still in "
                "progress; retry later"
            ) from exc
        if outcome.outcome == "claimed":
            return _NOT_REPLAYED
        if outcome.outcome == "mismatch":
            raise ContractIdempotencyConflict(
                f"idempotency key {self.key!r} was already used for {self.label} "
                "with different arguments"
            )
        if outcome.effects:
            # the first execution's effects mark this caller alike
            _risk.apply_call_effects(_loads_effects(outcome.effects))
        if outcome.status != effects.SUCCEEDED:
            raise ContractResultNotReplayable(
                f"{self.label} with this idempotency key already completed; its "
                "result was too large to keep for replay"
            )
        wire = orjson.loads(outcome.result) if outcome.result is not None else None
        return _decode_result(func, wire)

    async def complete(
        self, session: Any, result: Any, fx: dict[str, Any] | None = None
    ) -> None:
        """Record the result and the call effects ``fx`` it left on its
        caller on the claimed row (same transaction as the effect)."""
        status, encoded = effects.replay_outcome(result)
        payload = encoded.decode() if encoded is not None else None
        await self.store.complete(
            session,
            dedupe_key=self.dedupe_key,
            status=status,
            result=payload,
            effects=orjson.dumps(fx).decode() if fx else None,
        )

    async def amend(
        self,
        uow_factory: Callable[[], Any],
        recorded: dict[str, Any] | None,
        fx: dict[str, Any] | None,
    ) -> None:
        """After the command's ``on_commit`` callbacks ran: replace the
        ``recorded`` call effects (committed with the command: the body's
        plus what its callbacks declare) with ``fx``, what the execution
        actually left, when they differ (a closure's effects; a declared one
        that did not run). Never raises: the command committed, and the
        record keeps ``recorded`` (fail closed for declared callbacks)."""
        if fx == recorded:
            return
        try:
            await self.store.amend_effects(
                uow_factory,
                dedupe_key=self.dedupe_key,
                effects=orjson.dumps(fx).decode() if fx else None,
            )
        except Exception:  # committed: the record keeps its effects
            logging.getLogger("sandloom.core.contracts").error(
                "could not record the effects of %s's on_commit callbacks on its "
                "dedupe record; a replay marks the caller with the effects "
                "recorded at its commit",
                self.label,
                exc_info=True,
            )


def _loads_effects(raw: str) -> Any:
    """Stored call effects (an unreadable value applies nothing)."""
    try:
        return orjson.loads(raw)
    except orjson.JSONDecodeError:
        return None


#: ``_DurableCommandClaim.claim`` result meaning "run the body".
_NOT_REPLAYED: Any = object()


class _ReplayRollback(Exception):
    """Internal: a durable replay — roll the provider UoW back, return ``value``."""

    def __init__(self, value: Any) -> None:
        super().__init__("command replay")
        self.value = value


def _decode_result(func: Callable[..., Any], wire: Any) -> Any:
    """Rebuild a replayed result (wire form) as the method's return type."""
    annotation = _return_annotation_of(func)
    try:
        return contract_wire.decode(wire, annotation)
    except Exception:  # noqa: BLE001 - un-coercible: the untagged value is still right
        return contract_wire.decode_untyped(wire)
