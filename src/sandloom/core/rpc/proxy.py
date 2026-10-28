"""The caller side: :class:`RemoteServiceProxy`, the generated proxy of one
remote contract, and the describe cache it decodes results with.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from dataclasses import dataclass, field
from functools import cached_property
from time import perf_counter as _perf_counter
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import status
from pydantic import ValidationError

import sandloom.core.unit_of_work.request as _request
import sandloom.core.unit_of_work.risk as _risk
from sandloom.core import (
    contract_cache_shadow,
    contract_wire,
    rpc_identity,
    rpc_pair_mac,
)
from sandloom.core.config.settings import settings as _global_settings
from sandloom.core.module_api import (
    contract_error,
    current_contract_read_scope,
    invalidate_contract_read_memo,
)
from sandloom.core.module_api._read_scope import _MEMO_MISS, _memo_key
from sandloom.core.observability import metrics
from sandloom.core.rpc.binding import _bound_get, _check_binding, _new_nonce
from sandloom.core.rpc.client import _httpx, _rpc_client, _transport
from sandloom.core.rpc.context import (
    _context_payload,
    _encode_context,
    _merge_call_effects,
)
from sandloom.core.rpc.envelope import _envelope_result, _open_envelope
from sandloom.core.rpc.errors import (
    RemoteServiceBadResponse,
    RemoteServiceUnavailable,
    _RpcRefused,
)
from sandloom.core.rpc.outbound import (
    _get_auth,
    _MacSigner,
    _outbound_mac,
    _rpc_headers,
)
from sandloom.core.rpc.wire import (
    _ACCEPT_CODEC_HEADER,
    _CODEC_HEADER,
    _CONTRACT_HASH_HEADER,
    _CTX_HEADER,
    _NONCE_HEADER,
    _pack,
    _parse_codec,
    _unpack,
)

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger("sandloom.core.rpc")


# Transport-level attempts per logical call (connect errors / timeouts). Safe:
# reads have no effect, commands are deduped on their own idempotency key.
_RPC_TRANSPORT_ATTEMPTS = 2


#: (peer, outcome) -> resolved child of sandloom_rpc_client_request_duration_seconds.
_RPC_LATENCY: dict[tuple[str, str], Any] = {}


def _observe_rpc_latency(peer: str, outcome: str, seconds: float) -> None:
    child = _RPC_LATENCY.get((peer, outcome))
    if child is None:
        child = metrics.rpc_client_request_duration_seconds.labels(peer, outcome)
        if len(_RPC_LATENCY) < 4096:  # peers are bounded by SERVICE_BINDINGS
            _RPC_LATENCY[(peer, outcome)] = child
    child.observe(seconds)


_RPC_RETRY_DELAY_SECONDS = 0.1


@dataclass(frozen=True)
class _RemoteParam:
    name: str
    kind: str
    annotation: Any

    @cached_property
    def plan(self) -> contract_wire.WirePlan:
        return contract_wire.wire_plan(self.annotation)


@dataclass(frozen=True)
class _RemoteMethod:
    """One method of a described remote contract, as the caller codes it."""

    kind: str | None
    params: tuple[_RemoteParam, ...]
    returns: Any
    #: the command's declared risk (``contract_method(risk=...)``), from the
    #: provider's describe document: marks the caller's request like a local
    #: call does (ADR-0009 item 7; local == remote)
    risk: str | None = None

    @cached_property
    def result_plan(self) -> contract_wire.WirePlan:
        return contract_wire.wire_plan(self.returns)

    @cached_property
    def params_typed(self) -> bool:
        return all(p.plan.typed for p in self.params)

    @cached_property
    def by_name(self) -> dict[str, _RemoteParam]:
        return {p.name: p for p in self.params}

    @cached_property
    def positional(self) -> tuple[_RemoteParam, ...]:
        return tuple(p for p in self.params if p.kind != "keyword_only")

    def encode_typed(
        self, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[list[Any], dict[str, Any]] | None:
        """Codec-v2 arguments, or ``None`` (use the tagged codec instead)."""
        if not self.params_typed or len(args) > len(self.positional):
            return None
        try:
            wire_args = [
                self.positional[i].plan.dump_typed(value)
                for i, value in enumerate(args)
            ]
            wire_kwargs: dict[str, Any] = {}
            for key, value in kwargs.items():
                param = self.by_name.get(key)
                if param is None or param.kind == "positional_only":
                    return None  # the provider rejects it: keep the v1 path
                wire_kwargs[key] = param.plan.dump_typed(value)
        except contract_wire.ContractWireError:
            return None
        return wire_args, wire_kwargs


@dataclass(frozen=True)
class _RemoteContract:
    """A provider's published contract schema, as the caller decodes with it."""

    hash: str
    returns: dict[str, Any]
    methods: dict[str, _RemoteMethod] = field(default_factory=dict)


#: (base_url, service) -> the provider's last described contract.
_remote_contracts: dict[tuple[str, str], _RemoteContract] = {}


_describe_locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


_describe_failed: dict[tuple[str, str], tuple[str, float]] = {}


_DESCRIBE_FAILURE_TTL_SECONDS = 5.0


def _parse_remote_contract(data: Any) -> _RemoteContract | None:
    contract = data.get("contract") if isinstance(data, dict) else None
    if not isinstance(contract, dict) or not isinstance(contract.get("hash"), str):
        return None
    methods = contract.get("methods")
    returns: dict[str, Any] = {}
    parsed: dict[str, _RemoteMethod] = {}
    if isinstance(methods, dict):
        for name, schema in methods.items():
            if not isinstance(schema, dict):
                returns[str(name)] = Any
                continue
            descriptor = schema.get("returns")
            ret = (
                contract_wire.annotation_from_descriptor(descriptor)
                if isinstance(descriptor, dict)
                else Any
            )
            returns[str(name)] = ret
            params: list[_RemoteParam] = []
            for param in schema.get("params") or ():
                if not isinstance(param, dict):
                    continue
                type_desc = param.get("type")
                params.append(
                    _RemoteParam(
                        name=str(param.get("name")),
                        kind=str(param.get("kind") or "positional_or_keyword"),
                        annotation=(
                            contract_wire.annotation_from_descriptor(type_desc)
                            if isinstance(type_desc, dict)
                            else Any
                        ),
                    )
                )
            kind = schema.get("kind")
            risk = schema.get("risk")
            parsed[str(name)] = _RemoteMethod(
                kind=str(kind) if isinstance(kind, str) else None,
                params=tuple(params),
                returns=ret,
                risk=risk if isinstance(risk, str) else None,
            )
    return _RemoteContract(hash=contract["hash"], returns=returns, methods=parsed)


class RemoteServiceProxy:
    """Runtime-generated async proxy for one service-registry contract.

    Wire codec: until the provider has been described the proxy speaks codec
    1 (the tagged tree, also used for any signature that is not fully typed).
    Once described, arguments of a fully typed method are sent typed
    (``"codec": 2``) and the proxy accepts a typed result for the described
    contract hash; the provider answers typed only when that hash matches its
    own (a provider of another contract version answers tagged), and marks
    the result's codec in ``X-SANDLOOM-RPC-Codec``.
    """

    def __init__(
        self,
        *,
        service_name: str,
        binding: dict[str, object],
        default_timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
        settings: Any = None,
    ) -> None:
        self._service_name = service_name
        #: This caller's settings (keys, signature policy); ``None`` = the
        #: process settings (the conformance harness passes a keyed copy).
        self._cfg = settings if settings is not None else _global_settings
        # Test / in-process seam (e.g. an ASGI transport); default: the shared
        # pooled client.
        self._client = client
        self._base_url = str(
            binding.get("base_url") or binding.get("url") or ""
        ).rstrip("/")
        timeout = binding.get("timeout_seconds")
        self._timeout_seconds = (
            float(timeout)
            if isinstance(timeout, (str, int, float))
            else default_timeout_seconds
        )
        provider = binding.get("service_id")
        #: The provider's service id: every call's pair key derives from it
        #: and its response must be keyed by it.
        self._provider_service = str(provider) if provider else None
        #: method -> its call URL (built once; the aiohttp client keys its
        #: parsed URL on it).
        self._urls: dict[str, str] = {}

    def __getattr__(self, method_name: str) -> Any:
        if method_name.startswith("_"):
            raise AttributeError(method_name)
        call = self._call

        async def _remote_method(*args: Any, **kwargs: Any) -> Any:
            return await call(method_name, args=args, kwargs=kwargs)

        _remote_method.__name__ = method_name
        # Cached on the instance: later lookups never reach __getattr__.
        self.__dict__[method_name] = _remote_method
        return _remote_method

    def _described(self, method_name: str) -> tuple[_RemoteContract | None, Any]:
        contract = _remote_contracts.get((self._base_url, self._service_name))
        if contract is None:
            return None, None
        return contract, contract.methods.get(method_name)

    async def _call(
        self, method_name: str, *, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        # Per-read inactivity timeouts alone let a slow trickling response run
        # forever. Bound the WHOLE call, including schema discovery, retries
        # and their delay, by their existing finite timeout budget.
        deadline = self._timeout_seconds * (_RPC_TRANSPORT_ATTEMPTS + 1)
        deadline += _RPC_RETRY_DELAY_SECONDS * (_RPC_TRANSPORT_ATTEMPTS - 1)
        try:
            async with asyncio.timeout(deadline):
                return await self._call_with_budget(
                    method_name, args=args, kwargs=kwargs
                )
        except TimeoutError as exc:
            raise RemoteServiceUnavailable(
                self._service_name,
                reason=(
                    "overall RPC deadline exceeded (the call may have committed; "
                    "retry with the same idempotency key)"
                ),
                code="rpc_timeout",
            ) from exc

    async def _call_with_budget(
        self, method_name: str, *, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        if not self._base_url:
            raise RemoteServiceUnavailable(self._service_name, reason="missing URL")

        url = self._url(method_name)
        described, method = self._described(method_name)

        scope = current_contract_read_scope()
        if scope is not None and scope.memo_enabled and described is None:
            await self._remote_contract("")
            described, method = self._described(method_name)
        generation = scope.memo_generation if scope is not None else 0
        memo_key, hit = self._memo_lookup(scope, method_name, method, args, kwargs)
        if hit is not _MEMO_MISS:
            return hit
        request = _request.request_scope_get()
        intent = request.intent if request is not None else None
        if intent is not None and described is None:
            # the window rules tell a read from a command (release_check's
            # authority may run reads): describe before deciding
            await self._remote_contract("")
            described, method = self._described(method_name)
        typed = described is not None and method is not None
        # A call that may write (a command, or a method not yet described)
        # invalidates memoised local reads of the current read scope.
        writes = method is None or method.kind != "read"
        # An operation-intent request: the remote call obeys the local rules
        # (refused inside a window, counted while it runs so a window entered
        # meanwhile waits for it; G-UNIT-02 / G-BILLING-19, local == remote).
        _note_command_risk(self._service_name, method)
        intent_token = intent.enter_contract(not writes) if intent is not None else None
        try:
            body = _pack(
                self._payload(method_name, method if typed else None, args, kwargs)
            )
            headers, signer = self._post_headers(
                method_name, method, body, described if typed else None
            )
            shadow, started = self._before_send(writes)
            try:
                response = await self._post(url, body, headers, signer)
            finally:
                if writes:
                    invalidate_contract_read_memo()
        finally:
            if intent is not None:
                intent.leave_contract(intent_token)
        data = self._settle(response)
        annotation: Any = Any
        contract_hash = response.headers.get(_CONTRACT_HASH_HEADER)
        if contract_hash:
            contract = await self._remote_contract(contract_hash)
            if contract is not None:
                annotation = contract.returns.get(method_name, Any)
                method = contract.methods.get(method_name)
        result = self._decode_result(
            method_name,
            data,
            annotation,
            _parse_codec(response.headers.get(_CODEC_HEADER)),
            read=method is not None and method.kind == "read",
            memo=(scope, memo_key, generation),
        )
        if shadow is not None and started is not None:
            # ADR-0003 shadow measurement (OFF by default): never alters it.
            contract_cache_shadow.observe_read(
                shadow, self._service_name, method_name, args, kwargs, result, started
            )
        return result

    def _url(self, method_name: str) -> str:
        url = self._urls.get(method_name)
        if url is None:
            url = (
                f"{self._base_url}/_sandloom/rpc/"
                f"{quote(self._service_name, safe='.')}/{quote(method_name)}"
            )
            self._urls[method_name] = url
        return url

    def _before_send(self, writes: bool) -> tuple[Any, Any]:
        """A write invalidates the read memo; a read gets its (ADR-0003)
        shadow measurement, if on: ``(shadow, started)``."""
        if writes:
            invalidate_contract_read_memo()
            return None, None
        shadow = contract_cache_shadow.for_contract(self._service_name)
        return shadow, shadow.start() if shadow is not None else None

    def _memo_lookup(
        self,
        scope: Any,
        method_name: str,
        method: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> tuple[Any, Any]:
        """(this call's read-memo key or ``None``, its memoised result or
        ``_MEMO_MISS``) in the current read scope."""
        if scope is None or not scope.memo_enabled:
            return None, _MEMO_MISS
        key = _memo_key(self._service_name, method_name, args, kwargs)
        if key is None:
            return None, _MEMO_MISS
        memo_key = (
            "rpc",
            self._base_url,
            self._provider_service,
            rpc_identity.service_id(self._cfg),
            id(self._client) if self._client is not None else None,
            key,
        )
        if method is not None and method.kind == "read":
            hit = scope.memo_get(memo_key)
            if hit is not _MEMO_MISS:
                return memo_key, method.result_plan.local(hit)
        return memo_key, _MEMO_MISS

    def _payload(
        self,
        method_name: str,
        typed_method: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """The request body: codec-v2 arguments when ``typed_method`` (the
        described method, the provider speaking codec 2) can encode them, else
        the tagged tree every Sandloom version understands."""
        if typed_method is not None:
            encoded = typed_method.encode_typed(args, kwargs)
            if encoded is not None:
                return {
                    "args": encoded[0],
                    "kwargs": encoded[1],
                    "codec": contract_wire.CODEC_TYPED,
                }
        try:
            return {
                "args": [contract_wire.encode(arg) for arg in args],
                "kwargs": {k: contract_wire.encode(v) for k, v in kwargs.items()},
            }
        except contract_wire.ContractWireError as exc:
            raise contract_error(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{self._service_name}.{method_name}: {exc}",
                code="contract_argument_invalid",
            ) from exc

    def _post_headers(
        self,
        method_name: str,
        method: Any,
        body: bytes,
        typed_contract: _RemoteContract | None,
    ) -> tuple[dict[str, str], _MacSigner]:
        """Headers of the call (+ its per-attempt pair-MAC signer): the
        context, the accepted codec (``typed_contract``: the described contract
        when the call is typed)."""
        cfg = self._cfg
        mac_key = _outbound_mac(cfg, self._provider_service, self._service_name)
        identity = None
        if (
            method is None
            or method.kind != "read"
            or rpc_pair_mac.all_calls_require_signature(cfg)
        ):
            # A call that may have effects is also Ed25519-signed: a leaked
            # PROVIDER X25519 key must never forge a command (ADR-0004, KCI).
            identity = rpc_identity.signing_identity(cfg)
        target = f"{self._service_name}/{method_name}"
        payload = _context_payload(identity, self._provider_service)
        context = _encode_context(payload)
        headers = _rpc_headers()
        if typed_contract is not None and method.result_plan.typed:
            headers[_ACCEPT_CODEC_HEADER] = (
                f"{contract_wire.CODEC_TYPED};{typed_contract.hash}"
            )
        headers[_CTX_HEADER] = context
        # The signer MACs (and signs) per attempt: the nonce is per attempt.
        signer = _MacSigner(mac_key, "POST", target, context, body, identity, payload)
        return headers, signer

    def _settle(self, response: Any) -> dict[str, Any]:
        """The bound ``response``'s success envelope, or raise its error,
        after merging what the remote execution left on the caller's request
        (``fx``, success or error: G-SENS-04, spec §4.5, local == remote).

        "Answered" means "authenticated effects merged": an answer whose
        effects could not be merged (no envelope, a malformed one or its
        malformed ``fx``) says nothing about the provider unit or the calls
        it left in doubt, so the call is in doubt like one that got no
        answer (G-UNIT-02 / G-BILLING-19)."""
        try:
            data = _open_envelope(self._service_name, response)
            if not _merge_call_effects(data.get("fx")):
                raise RemoteServiceBadResponse(
                    self._service_name, reason="malformed call effects"
                )
        except Exception:
            _risk.note_in_doubt(self._service_name)
            raise
        return _envelope_result(self._service_name, response, data)

    def _decode_result(
        self,
        method_name: str,
        data: dict[str, Any],
        annotation: Any,
        codec: int,
        *,
        read: bool,
        memo: tuple[Any, Any, int],
    ) -> Any:
        """The call's result decoded against its return ``annotation`` (and
        memoised for a read when the read scope did not move on meanwhile)."""
        scope, memo_key, generation = memo
        try:
            plan = contract_wire.wire_plan(annotation)
            result = plan.decode(data.get("result"), codec)
            if (
                memo_key is not None
                and scope is not None
                and scope.memo_generation == generation
                and read
            ):
                scope.memo_set(memo_key, plan.local(result))
        except (ValidationError, ValueError, TypeError) as exc:
            raise RemoteServiceBadResponse(
                self._service_name,
                reason=f"result of {method_name!r} does not match its annotation",
            ) from exc
        return result

    async def _post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        signer: _MacSigner,
    ) -> Any:
        """POST the call, retrying a transport failure once.

        An attempt that may have reached the provider but got no
        authenticated answer (timeout, transport failure, cancellation, a
        response bound to another request) may still be running there — the
        provider never aborts a unit whose client went away — so the call is
        recorded in doubt (:func:`sandloom.core.unit_of_work.risk.note_in_doubt`):
        no intent window may open in this request (G-UNIT-02), and a call
        this process serves reports it to its caller (``fx``), hop after hop,
        so the root's windows refuse too. So is an attempt whose answer is
        not authenticated: too large to read (its ``fx`` lost with it) or an
        unkeyed refusal. A bound answer ran to its end (the provider answers
        after the call's unit closed, or refused it before opening one); its
        effects decide the rest (:meth:`_settle`)."""
        response: Any = None
        for attempt in range(1, _RPC_TRANSPORT_ATTEMPTS + 1):
            # A fresh nonce per attempt: a late answer to attempt 1 can never
            # be accepted as attempt 2's (and a retry is never a replay: it
            # carries a new nonce under a new request MAC).
            nonce = headers[_NONCE_HEADER] = _new_nonce()
            signer.apply(headers, nonce)
            # Per attempt: ok | timeout | error (the retry delay excluded).
            started = _perf_counter()
            outcome = "error"
            elapsed: float | None = None
            bound = False
            try:
                client = self._http()
                response = await client.post(
                    url,
                    content=body,
                    headers=headers,
                    timeout=self._timeout_seconds,
                )
                _check_binding(
                    client,
                    response,
                    nonce=nonce,
                    sent=headers,
                    peer=self._base_url,
                    expect_service=self._provider_service,
                    cfg=self._cfg,
                )
                bound = True
                outcome = "ok"
                break
            except _RpcRefused as exc:
                # Unauthenticated (unkeyed): nothing in it is the provider's
                # word, so the call stays in doubt. The provider could not
                # authenticate this call: a deployment problem (keys /
                # allowlist / clocks), not the end user's.
                raise RemoteServiceUnavailable(
                    self._service_name,
                    reason=f"the provider refused the call ({exc.status_code})",
                    code=exc.code,
                ) from exc
            except _transport().RpcResponseTooLarge as exc:
                # Never read, so never authenticated: its effects (``fx``,
                # the calls it left in doubt included) are lost with it and
                # the call stays in doubt. Not retried: the same call would
                # return the same body.
                raise RemoteServiceBadResponse(
                    self._service_name, reason=str(exc)
                ) from exc
            except (_httpx().TimeoutException, _httpx().TransportError) as exc:
                # Reads have no effect and commands are deduped on their own
                # idempotency argument, so retrying is safe.
                if isinstance(exc, _httpx().TimeoutException):
                    outcome = "timeout"
                elapsed = _perf_counter() - started
                if attempt < _RPC_TRANSPORT_ATTEMPTS:
                    await asyncio.sleep(_RPC_RETRY_DELAY_SECONDS)
                    continue
                if isinstance(exc, _httpx().TimeoutException):
                    raise RemoteServiceUnavailable(
                        self._service_name,
                        reason=(
                            "timeout (the call may have committed; retrying "
                            "with the same idempotency key is safe)"
                        ),
                        code="rpc_timeout",
                    ) from exc
                raise RemoteServiceUnavailable(
                    self._service_name, reason=exc.__class__.__name__
                ) from exc
            except _httpx().HTTPError as exc:
                raise RemoteServiceUnavailable(
                    self._service_name, reason=exc.__class__.__name__
                ) from exc
            finally:
                if not bound:
                    _risk.note_in_doubt(self._service_name)
                _observe_rpc_latency(
                    self._base_url,
                    outcome,
                    _perf_counter() - started if elapsed is None else elapsed,
                )
        assert response is not None  # noqa: S101 - type narrowing of an invariant
        return response

    def _http(self) -> Any:
        return self._client if self._client is not None else _rpc_client()

    async def _remote_contract(self, contract_hash: str) -> _RemoteContract | None:
        """The provider's contract schema (cached; re-described on a new hash)."""
        key = (self._base_url, self._service_name)
        cached = _remote_contracts.get(key)
        if cached is not None and (not contract_hash or cached.hash == contract_hash):
            return cached
        lock = _describe_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _describe_locks[key] = lock
        async with lock:
            cached = _remote_contracts.get(key)
            if cached is not None and (
                not contract_hash or cached.hash == contract_hash
            ):
                return cached
            failed = _describe_failed.get(key)
            if (
                failed is not None
                and failed[0] == contract_hash
                and failed[1] > time.monotonic()
            ):
                return cached
            return await self._fetch_remote_contract(key, cached, contract_hash)

    async def _fetch_remote_contract(
        self,
        key: tuple[str, str],
        cached: _RemoteContract | None,
        contract_hash: str,
    ) -> _RemoteContract | None:
        try:
            mac_key = _outbound_mac(
                self._cfg, self._provider_service, self._service_name
            )
            headers, signer = _get_auth(
                self._service_name, self._cfg, mac_key, self._provider_service
            )
            response = await _bound_get(
                self._http(),
                f"{self._base_url}/_sandloom/rpc/{quote(self._service_name, safe='.')}",
                headers,
                self._timeout_seconds,
                self._base_url,
                self._provider_service,
                self._cfg,
                signer=signer,
            )
            described = (
                _parse_remote_contract(_unpack(response.content))
                if response.status_code == status.HTTP_200_OK
                else None
            )
        except Exception:  # noqa: BLE001 - describe is best-effort; decode untyped
            described = None
        if described is None:
            if len(_describe_failed) >= 512:
                _describe_failed.pop(next(iter(_describe_failed)))
            _describe_failed[key] = (
                contract_hash,
                time.monotonic() + _DESCRIBE_FAILURE_TTL_SECONDS,
            )

            counter = getattr(metrics, "rpc_describe_failures_total", None)
            if counter is not None:
                counter.labels(self._base_url).inc()
            logger.warning(
                "Could not describe remote contract %s at %s; decoding its "
                "results without their annotations",
                self._service_name,
                self._base_url,
            )
            return cached
        _describe_failed.pop(key, None)
        _remote_contracts[key] = described
        return described


def remote_service_proxy(
    service_name: str,
    binding: dict[str, object],
    *,
    default_timeout_seconds: float,
    settings: Any = None,
) -> RemoteServiceProxy:
    """Factory used by :class:`CoreFacade` to build a remote fallback (keyed
    with the core's ``settings``)."""
    return RemoteServiceProxy(
        service_name=service_name,
        binding=binding,
        default_timeout_seconds=default_timeout_seconds,
        settings=settings,
    )


def _note_command_risk(service: str, method: Any) -> None:
    """Mark the caller's request with a described command's risk BEFORE the
    call, exactly like the local contract runtime (``command:<service>``)."""
    risk = getattr(method, "risk", None)
    if method is None or getattr(method, "kind", None) != "command" or not risk:
        return
    _risk.note_effect(str(risk), f"command:{service}", deduped=True, own_unit=True)
