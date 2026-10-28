"""Risk declared at its source, and what it triggers (ADR-0009 items 6-12).

**Sources** (static tags; each sets :data:`RISK_ATTR` on the callable, so the
route policy and the route snapshot see a tagged dependency):

* the framework's admin gates (``require_platform_admin``, ``require(Role(
  "admin"))``, ``require(OrgRole("admin" | "owner"))``, tenancy's
  ``require_org_role("admin" | "owner")``): ``admin``;
* :func:`sensitive_effect` on an effect service (``money`` for operation
  intents, ``security`` with a step-up for credential changes);
* ``contract_method(command=True, risk="money")`` and the ``MoneyLedger``
  model mixin (:mod:`sandloom.core.sql_alchemy.money_ledger`), detected when the
  effect runs in a request;
* the ``sensitive(risk)`` route marker (the escape hatch).

**Triggers** (applied by the framework, in a request only: consumers, tasks
and saga steps have no HTTP subject and keep their effect-level dedupe):

* **Step-up** (:class:`StepUpRequiredError`, SANDLOOM-R011): an effect declaring
  ``step_up=N`` (``security`` defaults to 300 s) needs the token's
  ``auth_time`` (never ``iat``) at most N s old, or ``mark_reauthenticated()``
  in this request. Checked when the effect is entered; when it fails there,
  the effect's own body may still re-authenticate (identity's current
  password), so it is checked again when the effect returns and before the
  COMMIT of the request's writing unit: an unsatisfied step-up never commits.
  ``401 WWW-Authenticate: Bearer error="insufficient_user_authentication",
  max_age=N`` (RFC 9470). A public route has no session to step up: the
  route's own proof applies (password-reset token).
* **Money dedupe**: a ``sensitive_effect("money")`` call needs a dedupe
  record (``operation_key=`` passed to it, or the request's
  ``request_operation_key()``); a client that sent no ``Idempotency-Key``
  gets ``428`` (:class:`IdempotencyKeyRequiredError`, SANDLOOM-R010), a route that
  has no dedupe for the key it got raises
  :class:`MoneyEffectWithoutDedupeError` (SANDLOOM-R009). A command carries its
  own key and a ``MoneyLedger`` row its unique operation key, so they are
  deduped by construction. A route tagged ``sensitive("money")`` that commits
  a write with no deduped money effect and no request key gets the same
  ``428`` / SANDLOOM-R009 before its COMMIT.
* **Audit**: one automatic audit row (``sandloom.sensitive_write``) per sensitive
  write, in the request's writing unit, ``required``: actor, route template,
  method, path parameters, risks and their sources, the route's status. It
  never records bodies. A risky command commits in its own unit: the
  request's unit that commits next records the row even if it wrote
  nothing, and a request with no unit gets one opened for it by the route
  runtime (:func:`needs_audit_unit`).
"""

from __future__ import annotations

import functools
import inspect
import time
import uuid
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from typing import Any, Literal, Self, TypeVar

from fastapi import HTTPException

import sandloom.core.unit_of_work.effects as _effects
from sandloom.core.unit_of_work.request import (
    SELF_REVOKED_KEY,
    RequestScope,
    admission_get,
    current_request_scope,
)
from sandloom.core.unit_of_work.savepoints import token_survived
from sandloom.core.unit_of_work.scope import _audit_store

F = TypeVar("F", bound=Callable[..., Any])
Risk = Literal["admin", "money", "security"]

#: Set on a callable (a gate dependency, an effect service, a contract
#: method) that carries a framework risk tag (ADR-0009 items 6-8).
RISK_ATTR = "__sandloom_risk__"
#: Set by :func:`sensitive_effect` next to :data:`RISK_ATTR`: the step-up
#: max age in seconds, or ``None``.
STEP_UP_ATTR = "__sandloom_step_up__"
#: The risks a route or an effect can carry.
RISKS: tuple[str, ...] = ("admin", "money", "security")
#: The derived step-up per risk (ADR-0009 decision 3: ``security`` only).
STEP_UP_DEFAULTS: dict[str, int] = {"security": 300}
#: The automatic audit action of a sensitive write.
AUDIT_ACTION = "sandloom.sensitive_write"


class MoneyEffectWithoutDedupeError(RuntimeError):
    """A money effect ran in a request without a dedupe record (SANDLOOM-R009)."""

    sandloom_code = "SANDLOOM-R009"


class IdempotencyKeyRequiredError(HTTPException):
    """A money effect needs the client's ``Idempotency-Key``
    (``428``, SANDLOOM-R010).
    """

    sandloom_code = "SANDLOOM-R010"

    def __init__(self) -> None:
        super().__init__(
            status_code=428,
            detail="Idempotency-Key required for this operation",
        )


class StepUpRequiredError(HTTPException):
    """A step-up is required (``401``, RFC 9470, SANDLOOM-R011)."""

    sandloom_code = "SANDLOOM-R011"

    def __init__(self, max_age: int) -> None:
        super().__init__(
            status_code=401,
            detail="Recent authentication required",
            headers={
                "WWW-Authenticate": (
                    'Bearer error="insufficient_user_authentication", '
                    f"max_age={int(max_age)}"
                )
            },
        )


class RiskState:
    """The risk bookkeeping of one request (``RequestScope.risk``)."""

    __slots__ = (
        "audited",
        "client_key",
        "elsewhere",
        "money_deduped",
        "operation_key",
        "reauthenticated",
        "risks",
        "step_up",
    )

    def __init__(self) -> None:
        #: ``{risk: {source, ...}}`` of the effects that ran
        self.risks: dict[str, set[str]] = {}
        #: a money effect with its own dedupe record ran
        self.money_deduped = False
        #: ``mark_reauthenticated()`` was called in this request
        self.reauthenticated = False
        #: the automatic audit row was recorded
        self.audited = False
        #: an effect committed in its own unit (a command, local or remote):
        #: the request owes its audit row even if its own units wrote nothing
        self.elsewhere = False
        #: the request's operation key (set by its idempotency guard)
        self.operation_key: str | None = None
        #: step-ups entered unsatisfied (max ages), re-checked before COMMIT
        self.step_up: list[int] = []
        #: the client of the request a REMOTE call serves sent an
        #: ``Idempotency-Key`` (the caller facts; locally: the headers)
        self.client_key = False


def risk_state(scope: RequestScope) -> RiskState:
    """The scope's :class:`RiskState` (created on first use)."""
    state: RiskState | None = scope.risk
    if state is None:
        state = scope.risk = RiskState()
    return state


def _tag(fn: Any, risk: str, step_up: int | None) -> None:
    setattr(fn, RISK_ATTR, risk)
    setattr(fn, STEP_UP_ATTR, step_up)


def tag_risk(fn: F, risk: Risk) -> F:
    """Tag a framework gate with the risk it implies (ADR-0009 item 6)."""
    if risk not in RISKS:
        raise ValueError(f"risk must be one of {', '.join(RISKS)}, not {risk!r}")
    _tag(fn, risk, None)
    return fn


#: The observer of effects that ran in a request (G-ROUTE-05): ``None`` in
#: production; the test harness sets one (:func:`set_effect_observer`) so the
#: module's own tests record the money / security effects each route ran,
#: written to the route snapshot as INFORMATIONAL ``observed_effects``.
effect_observer: Callable[[str, str, dict[str, set[str]]], None] | None = None


def set_effect_observer(
    observer: Callable[[str, str, dict[str, set[str]]], None] | None,
) -> None:
    """Install (or clear) the observer the route runtime calls at the end of
    every request whose effects carried a risk: ``observer(method, path
    template, {risk: {source, ...}})``. Informational only: it never gates."""
    global effect_observer
    effect_observer = observer


#: The observer of what runs in the current block (``None`` outside one):
#: the RPC dispatch observes a served call, whose effects it reports to the
#: caller (``fx``), so the caller's request is marked exactly as if the call,
#: nested calls included, had run locally (G-SENS-04, local == remote); a
#: durable command observes its body, whose effects its dedupe record keeps
#: for a replay (:class:`EffectRecorder`).
_effects_sink: ContextVar[ObservedEffects | None] = ContextVar(
    "sandloom_effects_sink", default=None
)


class ObservedEffects:
    """Collect what runs in the block, in a request or not: ``sink``
    (``{risk: {source, ...}}`` of every risky effect) and ``in_doubt`` (the
    remote calls an attempt of which its provider never answered: their
    provider unit may still run). A class, not a generator: it brackets every
    served RPC call.

    ``nested``: the observer of a part of an observed block (a command's
    body): what it saw is reported to the enclosing observer too. A served
    RPC call is a process boundary (not nested): its effects travel back in
    the response only."""

    __slots__ = ("_nested", "_parent", "_token", "in_doubt", "sink")

    def __init__(self, *, nested: bool = False) -> None:
        self.sink: dict[str, set[str]] = {}
        self.in_doubt: set[str] = set()
        self._nested = nested
        self._parent: ObservedEffects | None = None
        self._token: Any = None

    def __enter__(self) -> Self:
        if self._nested:
            self._parent = _effects_sink.get()
        self._token = _effects_sink.set(self)
        return self

    def __exit__(self, *_exc: object) -> None:
        _effects_sink.reset(self._token)
        parent = self._parent
        if parent is not None:
            for risk, sources in self.sink.items():
                parent.sink.setdefault(risk, set()).update(sources)
            parent.in_doubt.update(self.in_doubt)


def _observe(risk: str, source: str) -> None:
    observed = _effects_sink.get()
    if observed is not None:
        observed.sink.setdefault(risk, set()).add(source)


def note_in_doubt(call: str) -> None:
    """A remote call's attempt got no answer from its provider: its provider
    unit may still run. Recorded on the request's intent state (its windows
    refuse, G-UNIT-02) and on the observer of a served call (reported to its
    caller, hop after hop) or of a command (kept with its dedupe record)."""
    observed = _effects_sink.get()
    if observed is not None:
        observed.in_doubt.add(call)
    scope = current_request_scope()
    intent = getattr(scope, "intent", None) if scope is not None else None
    if intent is not None:
        intent.note_in_doubt(call)


def note_effect(
    risk: str, source: str, *, deduped: bool = False, own_unit: bool = False
) -> None:
    """Record that an effect of ``risk`` ran in the current request (kernel use:
    money commands and ledger rows, which carry their own dedupe record).
    ``own_unit``: the effect commits in its own unit (a command), so the
    request writes its audit row even when none of its units wrote."""
    _observe(risk, source)
    scope = current_request_scope()
    if scope is None:
        return
    state = risk_state(scope)
    state.risks.setdefault(risk, set()).add(source)
    if risk == "money" and deduped:
        state.money_deduped = True
    if own_unit:
        state.elsewhere = True


def mark_reauthenticated() -> None:
    """Record a successful re-authentication in this request (the caller just
    verified the current password, or a just-completed sign-in): it satisfies
    every step-up of the request (ADR-0009 item 8)."""
    scope = current_request_scope()
    if scope is not None:
        risk_state(scope).reauthenticated = True


def request_operation_key() -> str | None:
    """The request's operation key: a stable digest of its route, caller,
    tenant and ``Idempotency-Key`` as claimed by the route's idempotency guard,
    or ``None`` (no key, or no guard). Pass it as a money effect's
    ``operation_key`` (ADR-0009 item 11)."""
    scope = current_request_scope()
    state = scope.risk if scope is not None else None
    return state.operation_key if state is not None else None


def bind_operation_key(key: str) -> None:
    """Record the request's operation key (the idempotency guards)."""
    scope = current_request_scope()
    if scope is not None:
        risk_state(scope).operation_key = key


def _step_up_satisfied(scope: RequestScope, state: RiskState, max_age: int) -> bool:
    if state.reauthenticated:
        return True
    auth_time = (scope.claims or {}).get("auth_time")
    return (
        isinstance(auth_time, (int, float))
        and not isinstance(auth_time, bool)
        and 0 <= time.time() - float(auth_time) <= max_age
    )


def client_sent_key(scope: RequestScope) -> bool:
    """The request's client sent an ``Idempotency-Key`` (for a served RPC
    call: the caller's client did, from the caller facts)."""
    state = scope.risk
    if state is not None and state.client_key:
        return True
    request = scope.request
    headers = getattr(request, "headers", None)
    return bool(headers is not None and headers.get("idempotency-key"))


def _money_without_dedupe(scope: RequestScope, what: str) -> Exception:
    if not client_sent_key(scope):
        return IdempotencyKeyRequiredError()
    return MoneyEffectWithoutDedupeError(
        f"{what}: a money effect without a dedupe record — pass "
        "operation_key=request_operation_key() (the route needs an idempotency "
        "guard for the key the client sent), or write the ledger row's "
        "operation key (SANDLOOM-R009)"
    )


def _enter(
    scope: RequestScope, risk: str, step_up: int | None, name: str, kwargs: Any
) -> int | None:
    state = risk_state(scope)
    state.risks.setdefault(risk, set()).add(name)
    if admission_get() is not scope.admission:
        # in a contract unit (a fresh admission scope): it commits on its
        # own, so the request owes its audit row even if its own units write
        # nothing — as for the same effect run over RPC (local == remote)
        state.elsewhere = True
    pending: int | None = None
    if (
        step_up is not None
        and scope.claims is not None
        and not _step_up_satisfied(scope, state, step_up)
    ):
        pending = step_up
        state.step_up.append(step_up)
    if risk == "money":
        if kwargs.get("operation_key") or state.operation_key:
            state.money_deduped = True
        else:
            raise _money_without_dedupe(scope, name)
    return pending


def _leave(scope: RequestScope, pending: int | None) -> None:
    if pending is None:
        return
    state = risk_state(scope)
    if not _step_up_satisfied(scope, state, pending):
        raise StepUpRequiredError(pending)
    if pending in state.step_up:
        state.step_up.remove(pending)


def require_step_up(scope: RequestScope, max_age: int) -> None:
    """Register a static step-up of the route (``sensitive("security")``):
    satisfied now, by ``mark_reauthenticated()`` in the handler, or refused
    before the COMMIT and before the response (route runtime)."""
    if scope.claims is None:
        return
    state = risk_state(scope)
    if not _step_up_satisfied(scope, state, max_age):
        state.step_up.append(max_age)


def settle_step_up(scope: RequestScope) -> None:
    """Refuse the request if a registered step-up is still unsatisfied."""
    state = scope.risk
    if state is None or not state.step_up:
        return
    max_age = min(state.step_up)
    if not _step_up_satisfied(scope, state, max_age):
        raise StepUpRequiredError(max_age)
    state.step_up.clear()


def sensitive_effect(risk: Risk, *, step_up: int | None = None) -> Callable[[F], F]:
    """Declare the risk of an effect service, once, at its source (ADR-0009
    items 7-8). Stack it UNDER ``@classmethod`` / ``@staticmethod``.

    ``step_up`` (seconds) defaults to 300 for ``security`` and to none for
    ``admin`` / ``money``. See the module docstring for what the risk triggers.
    """
    if risk not in RISKS:
        raise ValueError(
            f"sensitive_effect() risk must be one of {', '.join(RISKS)}, not {risk!r}"
        )
    if step_up is not None and (isinstance(step_up, bool) or step_up <= 0):
        raise ValueError("sensitive_effect(step_up=) is a positive number of seconds")
    max_age = step_up if step_up is not None else STEP_UP_DEFAULTS.get(risk)

    def decorate(fn: F) -> F:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(
                f"@sensitive_effect({risk!r}) decorates an async function, not {fn!r}"
            )
        name = f"{fn.__module__}.{fn.__qualname__}"

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            _observe(risk, name)
            scope = current_request_scope()
            if scope is None:
                return await fn(*args, **kwargs)
            pending = _enter(scope, risk, max_age, name, kwargs)
            result = await fn(*args, **kwargs)
            _leave(scope, pending)
            return result

        try:
            # resolved annotations: a FastAPI dependency's signature is read
            # off the wrapper, whose globals are not the function's
            wrapper.__signature__ = inspect.signature(fn, eval_str=True)  # type: ignore[attr-defined]
        except (NameError, TypeError, AttributeError, SyntaxError):  # unresolvable
            wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
        _tag(wrapper, risk, max_age)
        return wrapper  # type: ignore[return-value]

    return decorate


# --------------------------------------------------------------------------- #
# The before-COMMIT stage (called by sandloom.core.unit_of_work.hooks)
# --------------------------------------------------------------------------- #
def _static_risk(scope: RequestScope) -> dict[str, Any]:
    policy = scope.policy
    risk = getattr(policy, "risk", None)
    return risk if isinstance(risk, dict) else {}


def before_commit(session: Any, scope: RequestScope, wrote: bool) -> None:
    """What risk triggers before the COMMIT of the request's writing unit:
    the pending step-ups, the static money dedupe, the automatic audit row."""
    state = scope.risk
    static = _static_risk(scope)
    if state is None and not static:
        return
    if state is not None and state.step_up:
        settle_step_up(scope)
    if not wrote and not (state is not None and state.elsewhere):
        return
    policy = scope.policy
    if not getattr(policy, "is_write", False):
        return
    if state is None:
        state = risk_state(scope)
    if "money" in static and not (state.money_deduped or state.operation_key):
        raise _money_without_dedupe(scope, f"{policy.method} {policy.path}")
    if not state.audited:
        _record_audit(session, scope, state, static)
        state.audited = True


def needs_audit_unit(scope: RequestScope) -> bool:
    """The request ran a risky command (committed in its own unit) on a write
    route and still owes its audit row: the route runtime opens the request
    unit for it when the handler opened none (G-SENS-04)."""
    state = scope.risk
    return bool(
        state is not None
        and state.elsewhere
        and not state.audited
        and getattr(scope.policy, "is_write", False)
    )


def _record_audit(
    session: Any, scope: RequestScope, state: RiskState, static: dict[str, Any]
) -> None:
    risks: dict[str, list[str]] = {
        risk: sorted(set(sources)) for risk, sources in static.items()
    }
    for risk, sources in state.risks.items():
        risks.setdefault(risk, [])
        risks[risk] = sorted(set(risks[risk]) | sources)
    store = _audit_store(session)
    if store is None:
        raise _effects.AuditUnavailableError(
            f"{AUDIT_ACTION}: the database backend has no audit store; a "
            "sensitive write needs its mandatory audit record"
        )
    request = scope.request
    route = request.scope.get("route") if request is not None else None
    policy = scope.policy
    data = {
        "route": getattr(policy, "path", None),
        "method": getattr(policy, "method", None),
        "path_params": dict(getattr(request, "path_params", None) or {}),
        "risks": risks,
        "status": int(getattr(route, "status_code", None) or 200),
    }
    store.record(
        session,
        action=AUDIT_ACTION,
        actor_id=_actor_uuid(scope.subject),
        actor_type="user" if scope.subject is not None else "anonymous",
        resource_type="route",
        resource_id=getattr(policy, "path", None),
        data=data,
        request_id=_request_id(),
        trace_id=None,
    )


def _request_id() -> str | None:
    return _effects._current_request_id()


def _actor_uuid(subject: str | None) -> uuid.UUID | None:
    if subject is None:
        return None
    try:
        return uuid.UUID(str(subject))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Call effects: what a contract call leaves on its caller's request
# --------------------------------------------------------------------------- #
#: Bounds of applied effects: sources per risk, their length, step-ups,
#: subjects, calls in doubt.
_RK_MAX_SOURCES = 64
_RK_MAX_SOURCE_LEN = 256
_SU_MAX = 16
_ID_MAX = 16


def _names(value: Any, most: int) -> bool:
    """A list of at most ``most`` non-empty bounded strings."""
    return (
        isinstance(value, list)
        and len(value) <= most
        and all(
            isinstance(item, str) and 0 < len(item) <= _RK_MAX_SOURCE_LEN
            for item in value
        )
    )


def _well_formed_risks(reported: Any) -> bool:
    return isinstance(reported, dict) and all(
        risk in RISKS and _names(sources, _RK_MAX_SOURCES)
        for risk, sources in reported.items()
    )


def _well_formed_step_ups(pending: Any) -> bool:
    return (
        isinstance(pending, list)
        and len(pending) <= _SU_MAX
        and all(
            isinstance(max_age, int) and not isinstance(max_age, bool) and max_age > 0
            for max_age in pending
        )
    )


#: Each call-effects field and the check of its value.
_FX_FIELDS: dict[str, Callable[[Any], bool]] = {
    "rk": _well_formed_risks,
    "id": lambda value: _names(value, _ID_MAX),
    "sr": lambda value: _names(value, _SU_MAX),
    # exactly the int 1: ``1.0`` and ``True`` compare equal to it
    "ra": lambda value: type(value) is int and value == 1,
    "su": _well_formed_step_ups,
}


def well_formed_call_effects(fx: Any) -> bool:
    """``fx`` (the wire form) is exactly what :func:`apply_call_effects`
    applies in full: a dict of known fields, each entry of the right type
    and within its bound. Anything else -- an unknown field, one malformed
    or oversized entry -- means what the call left is not fully known, so a
    live answer carrying it counts as in doubt (G-UNIT-02)."""
    return isinstance(fx, dict) and all(
        isinstance(name, str) and name in _FX_FIELDS and _FX_FIELDS[name](value)
        for name, value in fx.items()
    )


def apply_call_effects(fx: Any) -> None:
    """Mark the current request with call effects ``fx`` (the wire form, of
    an untrusted shape: checked and bounded) exactly as the call made locally
    would have: its risks (``rk``; each ran in a unit of its own that
    committed, so the request owes its audit row even if its own units write
    nothing, G-SENS-04), its remote calls still in doubt (``id``; no intent
    window of the request may open, G-UNIT-02), its committed
    self-revocation of the request's subject (``sr``, spec §4.5 item 3) and
    the step-ups it left pending (``su``, re-checked before the request's
    COMMIT).

    Used for a live RPC answer and for a command replay (its dedupe record
    keeps the first execution's effects). A re-authentication (``ra``) is
    never applied here: a live answer's is the caller's to merge
    (:func:`sandloom.core.rpc.context._merge_call_effects`); a replay proves
    none."""
    if not fx or not isinstance(fx, dict):
        return
    _merge_risks(fx.get("rk"))
    doubt = fx.get("id")
    if isinstance(doubt, list):
        for call in doubt[:_ID_MAX]:
            if isinstance(call, str) and 0 < len(call) <= _RK_MAX_SOURCE_LEN:
                note_in_doubt(call)
    scope = current_request_scope()
    if scope is None:
        return
    revoked = fx.get("sr")
    subject = scope.subject
    if (
        isinstance(revoked, list)
        and subject is not None
        and subject in {str(s) for s in revoked[:_SU_MAX]}
    ):
        scope.self_revoked = scope.self_revoked | {subject}
    pending = fx.get("su")
    if isinstance(pending, list) and pending:
        risk_state(scope).step_up.extend(
            max_age
            for max_age in pending[:_SU_MAX]
            if isinstance(max_age, int)
            and not isinstance(max_age, bool)
            and max_age > 0
        )


def _merge_risks(reported: Any) -> None:
    """``rk``: ``{risk: [source, ...]}`` of every risky effect that ran."""
    if not isinstance(reported, dict):
        return
    for risk, sources in reported.items():
        if risk not in RISKS or not isinstance(sources, list):
            continue
        for source in sources[:_RK_MAX_SOURCES]:
            if isinstance(source, str) and 0 < len(source) <= _RK_MAX_SOURCE_LEN:
                note_effect(risk, source, deduped=True, own_unit=True)


def call_effects(observed: ObservedEffects) -> dict[str, Any]:
    """The observed part of the call effects (``rk``, ``id``) in wire form."""
    fx: dict[str, Any] = {}
    if observed.sink:
        fx["rk"] = {risk: sorted(found) for risk, found in observed.sink.items()}
    if observed.in_doubt:
        fx["id"] = sorted(observed.in_doubt)
    return fx


def _self_revoked_in(session: Any, subject: str) -> bool:
    """``subject`` was self-revoked in ``session``'s unit, in a savepoint
    that survived (fixed again right before its COMMIT, hooks)."""
    info = getattr(session, "info", None)
    marks = info.get(SELF_REVOKED_KEY) if isinstance(info, dict) else None
    if not marks:
        return False
    return any(
        marked == subject and token_survived(session, token) for marked, token in marks
    )


class EffectRecorder(ObservedEffects):
    """What ONE command execution leaves on its caller's request, kept with
    its dedupe record and re-applied by a replay (:func:`apply_call_effects`)
    so a replay marks the caller exactly like the first execution: the risks
    it observed (and with them the audit obligation), the request subject's
    self-revocation it committed and the step-ups it left pending -- its
    body's and its ``on_commit`` callbacks' alike (they run after the
    commit, in the caller's request). The dedupe record is scoped to the end
    user (G-BILLING-20): only the same principal's retry replays it.

    The durable record commits with the command, BEFORE its callbacks run:
    it is written with what the body left plus what each queued callback
    DECLARES (:meth:`effects` ``callbacks=``: a ``@sensitive_effect``
    callback's risk and step-up, fail closed -- it may run), then corrected
    after the callbacks with what they actually left (the contract runtime).

    Not kept (replay-unsafe):

    * a re-authentication: that credential proof belongs to the request that
      presented it, and a replay presents none (its later step-ups fail
      closed: the client re-authenticates);
    * remote calls left in doubt: a fact about a unit that was RUNNING, not
      an effect of the committed command; kept, it would refuse every retry
      of an intent route until the record expired (the monolith's replay
      marks none either). It still reaches the live caller (the observer
      reports it up, and a served call's ``fx`` carries it)."""

    __slots__ = ("_acknowledged", "_scope", "_step_up")

    def __init__(self) -> None:
        super().__init__(nested=True)
        scope = self._scope = current_request_scope()
        self._acknowledged = True
        self._step_up: tuple[int, ...] = ()
        if scope is not None:
            subject = scope.subject
            self._acknowledged = subject is None or subject in scope.self_revoked
            state = scope.risk
            if state is not None and state.step_up:
                self._step_up = tuple(state.step_up)

    def effects(
        self, session: Any = None, callbacks: Iterable[Any] = ()
    ) -> dict[str, Any] | None:
        """The effects so far (``session``: the command's own unit, whose
        self-revocations commit with it), with what the ``on_commit``
        ``callbacks`` still to run declare (:func:`declared_effects`)."""
        fx: dict[str, Any] = {}
        sink = self.sink
        declared_step_up: list[int] = []
        if callbacks:
            sink, declared_step_up = _with_declared(sink, callbacks, self._scope)
        if sink:
            fx["rk"] = {risk: sorted(found) for risk, found in sink.items()}
        scope = self._scope
        if scope is not None:
            subject = scope.subject
            if (
                not self._acknowledged
                and subject is not None
                and (
                    subject in scope.self_revoked
                    or (session is not None and _self_revoked_in(session, subject))
                )
            ):
                fx["sr"] = [subject]
            state = scope.risk
            if state is not None and state.step_up:
                pending = list(state.step_up)
                for max_age in self._step_up:
                    if max_age in pending:
                        pending.remove(max_age)
                if pending or declared_step_up:
                    fx["su"] = sorted({*pending, *declared_step_up})
            elif declared_step_up:
                fx["su"] = sorted(set(declared_step_up))
        return fx or None


def _with_declared(
    sink: dict[str, set[str]], callbacks: Iterable[Any], scope: RequestScope | None
) -> tuple[dict[str, set[str]], list[int]]:
    """``sink`` with the risks ``callbacks`` declare (a copy when they add
    any), and the step-ups they declare."""
    declared, step_up = declared_effects(callbacks, scope)
    if declared:
        sink = {risk: set(found) for risk, found in sink.items()}
        for risk, sources in declared.items():
            sink.setdefault(risk, set()).update(sources)
    return sink, step_up


def declared_effects(
    callbacks: Iterable[Any], scope: RequestScope | None
) -> tuple[dict[str, set[str]], list[int]]:
    """What ``callbacks`` (an effect's ``on_commit`` callables, not yet run)
    declare they would mark on the request ``scope``: the risk of each
    ``@sensitive_effect`` one (itself, a ``functools.partial`` of it or a
    bound method), under the source its run records, and its step-up when
    ``scope``'s authentication does not satisfy it. A closure declares
    nothing: only its run shows what it marks."""
    risks: dict[str, set[str]] = {}
    step_up: list[int] = []
    for callback in callbacks:
        target = callback
        while isinstance(target, functools.partial):
            target = target.func
        risk = getattr(target, RISK_ATTR, None)
        if risk not in RISKS:
            continue
        name = f"{target.__module__}.{target.__qualname__}"
        risks.setdefault(risk, set()).add(name)
        max_age = getattr(target, STEP_UP_ATTR, None)
        if (
            isinstance(max_age, int)
            and scope is not None
            and scope.claims is not None
            and not _step_up_satisfied(scope, risk_state(scope), max_age)
        ):
            step_up.append(max_age)
    return risks, step_up


__all__ = [
    "AUDIT_ACTION",
    "IdempotencyKeyRequiredError",
    "MoneyEffectWithoutDedupeError",
    "EffectRecorder",
    "ObservedEffects",
    "RISKS",
    "RISK_ATTR",
    "STEP_UP_ATTR",
    "STEP_UP_DEFAULTS",
    "RiskState",
    "StepUpRequiredError",
    "before_commit",
    "bind_operation_key",
    "client_sent_key",
    "apply_call_effects",
    "call_effects",
    "declared_effects",
    "mark_reauthenticated",
    "needs_audit_unit",
    "note_effect",
    "note_in_doubt",
    "request_operation_key",
    "require_step_up",
    "risk_state",
    "sensitive_effect",
    "settle_step_up",
    "tag_risk",
    "well_formed_call_effects",
]
