"""Bounded, sanitized platform views. No system role or internal-token relay.

One router per loaded module ("source"), mounted at
``/api/v1/platform/<source>/``: in a split deployment the gateway routes each
prefix to the service hosting that module, so every view reports on — and
every command acts on — that service's own database and process. Reads are
operation class ``RS``; commands are class ``A`` with a fail-closed
``Idempotency-Key``, a required audit row in the same transaction as the
effect (or written before a non-transactional effect, which then runs last),
and a commit-time re-check of the operator's session.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from sandloom.core.auth.bearer import bearer_claims
from sandloom.core.auth.verification import (
    assert_token_valid_now,
    ensure_token_not_revoked,
)
from sandloom.core.descriptors.module_names import module_name as _module_name
from sandloom.core.guard_order import authenticates
from sandloom.core.idempotency import idempotency_guard
from sandloom.core.lifecycle import aggregate_health
from sandloom.core.rpc import check_remote_service_bindings
from sandloom.core.tasks.event_ops import (
    EventOperationUnavailable,
    event_transport,
    outbox_target,
    streams_dlq_list,
    streams_dlq_replay,
)
from sandloom.core.unit_of_work import db_session
from sandloom.core.unit_of_work.effects import record_audit
from sandloom.core.unit_of_work.hooks import before_commit
from sandloom.core.unit_of_work.risk import tag_risk
from sandloom.core.unit_of_work.scope import _audit_store


@authenticates
async def platform_operator(request: Request) -> None:
    claims = bearer_claims(request)
    if claims is None or claims.get("iat") is None:
        raise HTTPException(401, "Valid access token required")
    try:
        UUID(str(claims.get("sub")))
    except ValueError as exc:
        raise HTTPException(401, "Invalid token subject") from exc
    await ensure_token_not_revoked(request, claims)
    sources, roles = claims.get("claims_sources"), claims.get("roles")
    if (
        not isinstance(sources, list)
        or "identity" not in sources
        or not isinstance(roles, list)
        or "admin" not in roles
        or "identity" in (claims.get("claims_sources_failed") or [])
    ):
        raise HTTPException(403, "Platform administrator role required")


# ADR-0009 item 6: the platform console's operator gate carries the admin risk.
tag_risk(platform_operator, "admin")


def _session_still_valid(request: Request) -> Callable[[], Awaitable[None]]:
    """Before-commit re-check of the operator's revocation / block marks."""
    claims = bearer_claims(request) or {}

    async def _check() -> None:
        await assert_token_valid_now(claims)

    return _check


def _operator_id(request: Request) -> UUID:
    claims = bearer_claims(request) or {}
    return UUID(str(claims.get("sub")))


_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_SECRETISH = re.compile(
    r"(password|passwd|secret|token|key)\s*[=:]\s*\S+", re.IGNORECASE
)


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def safe_error(text: str | None, *, limit: int = 240) -> str | None:
    """Operator-safe error text: no URLs/DSNs, no key=value secrets, bounded."""
    if not text:
        return None
    cleaned = _SECRETISH.sub(r"\1=<redacted>", _URL.sub("<redacted-url>", text))
    cleaned = " ".join(cleaned.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


# --------------------------------------------------------------------- views


class AuditView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    occurred_at: datetime
    actor_id: UUID | None
    actor_type: str
    action: str
    resource_type: str | None
    resource_id: str | None
    request_id: str | None
    trace_id: str | None
    # Free-form audit data is deliberately not published in the operator API.
    # Existing rows can predate today's redaction policy.


class ServiceView(BaseModel):
    id: str
    status: str
    category: str


class DeadLetterView(BaseModel):
    id: UUID
    channel: str
    attempts: int
    last_error: str | None
    dead_at: datetime | None
    emitted_at: datetime | None


class ConsumerView(BaseModel):
    name: str
    channel: str
    group: str | None
    kind: str
    module: str | None
    required: bool
    state: str
    attempts: int
    lag: int | None
    pending: int | None
    last_connected_at: str | None
    last_error: str | None


class StreamDeadLetterView(BaseModel):
    id: str
    group: str | None
    event_id: str | None
    attempts: int | None
    reason: str | None
    replayable: bool


class ProjectionConsumerView(BaseModel):
    name: str
    source_channel: str
    durable: bool


class ProjectionView(BaseModel):
    name: str
    owner_module: str
    version: int | str | None
    storage: str | None
    rebuildable: bool
    has_rebuilder: bool
    consumers: list[ProjectionConsumerView]


class ConsumedContractView(BaseModel):
    module: str
    contract: str
    required: bool
    resolution: str


class BindingView(BaseModel):
    contract: str
    required: bool
    provided_locally: bool
    status: Literal["ok", "unreachable", "incompatible", "unverified", "not_probed"]
    detail: str | None


class ContractsView(BaseModel):
    provided: list[str]
    consumed: list[ConsumedContractView]
    bindings: list[BindingView]
    compat_check: str


class ModuleView(BaseModel):
    name: str
    version: str | None
    status: Literal["loaded", "failed", "skipped"]
    required: bool
    detail: str | None


class SettingView(BaseModel):
    key: str
    value: str


class InfraInstanceView(BaseModel):
    capability: str
    name: str
    driver: str | None
    status: str


class SettingsView(BaseModel):
    settings: list[SettingView]
    infrastructure: list[InfraInstanceView]
    feature_flags: Literal["not_configured"] = "not_configured"


class RequeueIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[UUID] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=3, max_length=500)


class ReplayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: str = Field(min_length=1, max_length=200, pattern=r"^[a-z0-9_.-]+$")
    group: str = Field(min_length=1, max_length=200)
    ids: list[str] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=3, max_length=500)


class CommandResult(BaseModel):
    requested: int
    applied: int


# ------------------------------------------------------------------- install


@dataclass(frozen=True)
class _Source:
    name: str
    database: str


def install(application: FastAPI, core: Any) -> None:
    for descriptor in application.state.module_summary.loaded:
        source = _module_name(descriptor.name)
        # Public paths are fixed at composition time, never supplied by a caller.
        if not source.replace("_", "").isalnum():
            continue
        database = next(iter(descriptor.database_instances()))
        router = APIRouter(
            prefix=f"/api/v1/platform/{source}",
            tags=["platform-operations"],
            dependencies=[Depends(platform_operator)],
        )
        target = _Source(source, database)
        _audit_routes(router, target)
        _service_routes(router, core)
        _outbox_routes(router, core, target)
        _stream_routes(router, core, target)
        _runtime_routes(router, core)
        application.include_router(router)


_READ: list[Any] = []


def _command(scope: str) -> list[Any]:
    return [Depends(idempotency_guard(scope=scope, fail_closed=True))]


# --------------------------------------------------------------------- audit


def _encode_cursor(row: dict[str, Any]) -> str:
    raw = json.dumps([row["occurred_at"].isoformat(), str(row["id"])])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        at, row_id = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return datetime.fromisoformat(at), UUID(row_id)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise HTTPException(400, "Invalid cursor") from exc


def _audit_routes(router: APIRouter, source: _Source) -> None:
    @router.get("/audit", response_model=list[AuditView], dependencies=_READ)
    async def audit(
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = Query(None, max_length=200),
        since: datetime | None = None,
        until: datetime | None = None,
        action: str | None = Query(None, max_length=200),
        action_prefix: str | None = Query(None, min_length=1, max_length=200),
        actor_id: UUID | None = None,
        resource_type: str | None = Query(None, max_length=100),
        resource_id: str | None = Query(None, max_length=200),
    ) -> JSONResponse:
        """Newest-first audit metadata, keyset-paged (``X-Next-Cursor``); no totals."""
        before = _decode_cursor(cursor) if cursor else None
        async with db_session(source.database) as db:
            store = _audit_store(db)
            if store is None:
                raise HTTPException(
                    501, "Audit queries are unsupported by this database driver"
                )
            try:
                rows = await store.query_page(
                    db,
                    limit=limit + 1,
                    before=before,
                    since=since,
                    until=until,
                    action=action,
                    action_prefix=action_prefix,
                    actor_id=actor_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                )
            except NotImplementedError as exc:
                raise HTTPException(
                    501, "Audit queries are unsupported by this database driver"
                ) from exc
        page = rows[:limit]
        more = len(rows) > limit
        headers = {"X-Next-Cursor": _encode_cursor(page[-1])} if more else {}
        body = [AuditView.model_validate(row).model_dump(mode="json") for row in page]
        return JSONResponse(body, headers=headers)


# -------------------------------------------------------------------- health


def _service_routes(router: APIRouter, core: Any) -> None:
    @router.get("/services", response_model=list[ServiceView], dependencies=_READ)
    async def services(request: Request) -> list[ServiceView]:
        results = await aggregate_health(core)
        rows = [
            ServiceView(id=name, status=result.status, category="infrastructure")
            for name, result in sorted(results.items())
        ]
        peers = await check_remote_service_bindings(core, only_required=False)
        rows.append(
            ServiceView(
                id="configured-peer-contracts",
                status=peers.status,
                category="contracts",
            )
        )
        runtime = getattr(request.app.state, "consumer_runtime", None)
        if runtime is not None:
            snapshot = runtime.snapshot()
            rows.append(
                ServiceView(
                    id="required-event-consumers",
                    status="degraded" if snapshot.required_unavailable else "ok",
                    category="eventing",
                )
            )
        return rows


# -------------------------------------------------------------------- outbox


class _BoundUow:
    """Hands a store the caller's open session instead of a new transaction."""

    def __init__(self, session: Any) -> None:
        self.session = session

    async def __aenter__(self) -> _BoundUow:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _within(session: Any) -> Callable[[], _BoundUow]:
    return lambda: _BoundUow(session)


@asynccontextmanager
async def _unavailable_as_501() -> AsyncIterator[None]:
    try:
        yield
    except EventOperationUnavailable as exc:
        raise HTTPException(501, str(exc.code).removeprefix("error: ")) from exc


def _outbox_routes(router: APIRouter, core: Any, source: _Source) -> None:
    @router.get("/outbox/dead", response_model=list[DeadLetterView], dependencies=_READ)
    async def dead_letters(
        channel: str | None = Query(None, max_length=200),
        limit: int = Query(100, ge=1, le=200),
        cursor: str | None = Query(None, max_length=200),
    ) -> JSONResponse:
        """Inspect dead rows using a stable (dead_at, id) continuation."""
        after = _decode_cursor(cursor) if cursor else None
        async with _unavailable_as_501():
            factory, store = outbox_target(core, source.database)
        rows = await store.list_dead(
            factory, channel=channel, limit=limit + 1, after=after
        )
        page = rows[:limit]
        headers = (
            {
                "X-Next-Cursor": _encode_cursor(
                    {"occurred_at": page[-1]["dead_at"], "id": page[-1]["id"]}
                )
            }
            if len(rows) > limit
            else {}
        )
        body = [
            DeadLetterView(
                id=row["id"],
                channel=row["channel"],
                attempts=row["attempts"],
                last_error=safe_error(row["last_error"]),
                dead_at=row["dead_at"],
                emitted_at=row["emitted_at"],
            ).model_dump(mode="json")
            for row in page
        ]
        return JSONResponse(body, headers=headers)

    @router.post(
        "/outbox/requeue",
        response_model=CommandResult,
        dependencies=_command(f"core.{source.name}.outbox.requeue"),
    )
    async def requeue(request: Request, data: RequeueIn) -> CommandResult:
        """Requeue chosen dead rows (``sandloom outbox requeue --id``); atomic audit."""
        async with _unavailable_as_501():
            _factory, store = outbox_target(core, source.database)
        async with db_session(source.database) as db:
            await before_commit(_session_still_valid(request))
            applied = await store.requeue_dead(_within(db), ids=list(data.ids))
            record_audit(
                "core.outbox.requeued",
                actor_id=_operator_id(request),
                resource_type="outbox",
                resource_id=source.database,
                data={
                    "reason": data.reason,
                    "ids": [str(i) for i in data.ids],
                    "applied": applied,
                },
                required=True,
                database=source.database,
            )
        return CommandResult(requested=len(data.ids), applied=applied)


# ------------------------------------------------------------------- streams


def _local_channels(request: Request) -> set[str]:
    runtime = getattr(request.app.state, "consumer_runtime", None)
    if runtime is None:
        return set()
    return {item.channel for item in runtime.snapshot().consumers}


def _stream_routes(router: APIRouter, core: Any, source: _Source) -> None:
    @router.get(
        "/streams/dlq",
        response_model=list[StreamDeadLetterView],
        dependencies=_READ,
    )
    async def stream_dead_letters(
        request: Request,
        channel: str = Query(max_length=200, pattern=r"^[a-z0-9_.-]+$"),
        limit: int = Query(100, ge=1, le=200),
        cursor: str | None = Query(None, max_length=80, pattern=r"^[0-9]+-[0-9]+$"),
    ) -> JSONResponse:
        """Inspect this service's DLQ, continuing after the last Redis entry ID."""
        if channel not in _local_channels(request):
            raise HTTPException(404, "No consumer of this channel runs in this service")
        async with _unavailable_as_501():
            entries = await streams_dlq_list(
                core, channel, limit=limit + 1, after=cursor
            )
        page = entries[:limit]
        headers = {"X-Next-Cursor": str(page[-1]["id"])} if len(entries) > limit else {}
        body = [
            StreamDeadLetterView(
                id=str(entry.get("id")),
                group=entry.get("group"),
                event_id=_text(entry.get("event_id")),
                attempts=entry.get("attempts"),
                reason=safe_error(entry.get("reason")),
                replayable=bool(entry.get("replayable", True)),
            ).model_dump(mode="json")
            for entry in page
        ]
        return JSONResponse(body, headers=headers)

    @router.post(
        "/streams/replay",
        response_model=CommandResult,
        dependencies=_command(f"core.{source.name}.streams.replay"),
    )
    async def replay(request: Request, data: ReplayIn) -> CommandResult:
        """Targeted replay to the group that dead-lettered (``sandloom streams
        dlq-replay``).

        Streams are not transactional: the audit row is written first and the
        replay is the last step, so a failed replay rolls the audit back.
        """
        if data.channel not in _local_channels(request):
            raise HTTPException(404, "No consumer of this channel runs in this service")
        async with _unavailable_as_501():
            event_transport(core)
        async with db_session(source.database) as db:
            await before_commit(_session_still_valid(request))
            record_audit(
                "core.streams.dlq_replayed",
                actor_id=_operator_id(request),
                resource_type="stream",
                resource_id=data.channel,
                data={"reason": data.reason, "group": data.group, "ids": data.ids},
                required=True,
                database=source.database,
            )
            await db.flush()
            applied = await streams_dlq_replay(
                core, data.channel, ids=data.ids, limit=len(data.ids), group=data.group
            )
        return CommandResult(requested=len(data.ids), applied=applied)


# ------------------------------------------------- runtime: consumers, etc.


def _bindings(core: Any, details: dict[str, Any]) -> list[BindingView]:
    failures = details.get("remote_services") or {}
    unverified = details.get("contracts_unverified") or {}
    rows = []
    for name, binding in sorted(core.settings.service_bindings.items()):
        local = core.service_registry.contains(name)
        failure = failures.get(name) if isinstance(failures, dict) else None
        status: Literal["ok", "unreachable", "incompatible", "unverified", "not_probed"]
        detail: str | None = None
        if local:
            status = "not_probed"
            detail = "Provided locally; the binding is not used"
        elif isinstance(failure, dict) and failure.get("contract_incompatible"):
            status = "incompatible"
            detail = safe_error("; ".join(map(str, failure["contract_incompatible"])))
        elif failure is not None:
            status = "unreachable"
            detail = safe_error(str(failure.get("error", "unreachable")))
        elif name in unverified:
            status = "unverified"
            detail = safe_error("; ".join(map(str, unverified[name])))
        else:
            status = "ok"
        rows.append(
            BindingView(
                contract=name,
                required=binding.get("required") is True,
                provided_locally=local,
                status=status,
                detail=detail,
            )
        )
    return rows


_SAFE_SETTINGS = (
    "environment",
    "production_profile",
    "tenancy_mode",
    "event_bus_backend",
    "outbox_relay_enabled",
    "contract_compat_check",
    "rpc_transport",
    "jwt_algorithm",
    "access_token_expire_minutes",
    "refresh_token_expire_days",
    "auth_revocation_fail_closed",
    "password_hash_profile",
    "rate_limit_enabled",
    "trust_proxy_headers",
    "billing_provider",
    "notifier_backend",
    "docs_enabled",
    "log_format",
    "log_level",
)


def _setting_text(value: Any) -> str:
    value = getattr(value, "value", value)
    return "unset" if value is None else str(value)


def _runtime_routes(router: APIRouter, core: Any) -> None:
    @router.get("/consumers", response_model=list[ConsumerView], dependencies=_READ)
    async def consumers(request: Request) -> list[ConsumerView]:
        """Event consumers of this process: state, lag, pending and last error."""
        runtime = getattr(request.app.state, "consumer_runtime", None)
        if runtime is None:
            return []
        return [
            ConsumerView(
                name=item.name,
                channel=item.channel,
                group=item.group,
                kind=getattr(item.kind, "value", str(item.kind)),
                module=item.module,
                required=item.required,
                state=item.state,
                attempts=item.attempts,
                lag=item.lag,
                pending=item.pending,
                last_connected_at=item.last_connected_at,
                last_error=safe_error(item.last_error),
            )
            for item in runtime.snapshot().consumers
        ]

    @router.get("/projections", response_model=list[ProjectionView], dependencies=_READ)
    async def projections() -> list[ProjectionView]:
        registry = core.projection_registry
        return [
            ProjectionView(
                name=item.name,
                owner_module=item.owner_module,
                version=item.version,
                storage=getattr(item, "storage", None),
                rebuildable=bool(getattr(item, "rebuildable", False)),
                has_rebuilder=registry.get_rebuilder(item.name) is not None,
                consumers=[
                    ProjectionConsumerView(
                        name=consumer.name,
                        source_channel=consumer.source_channel,
                        durable=bool(consumer.durable),
                    )
                    for consumer in registry.consumers_for(item.name)
                ],
            )
            for item in registry.list_projections()
        ]

    @router.get("/contracts", response_model=ContractsView, dependencies=_READ)
    async def contracts(request: Request) -> ContractsView:
        """Provided and consumed sync contracts; bindings with reachability/compat."""
        summary = request.app.state.module_summary
        probe = await check_remote_service_bindings(core, only_required=False)
        return ContractsView(
            provided=sorted(core.service_registry.list()),
            consumed=[
                ConsumedContractView(
                    module=module,
                    contract=str(entry.get("contract")),
                    required=bool(entry.get("required")),
                    resolution=str(entry.get("resolution")),
                )
                for module, entries in sorted(summary.consumes.items())
                for entry in entries
            ],
            bindings=_bindings(core, dict(probe.details or {})),
            compat_check=str(core.settings.contract_compat_check),
        )

    @router.get("/modules", response_model=list[ModuleView], dependencies=_READ)
    async def modules(request: Request) -> list[ModuleView]:
        summary = request.app.state.module_summary
        explicit = set(summary.explicit)
        rows = [
            ModuleView(
                name=item.name,
                version=getattr(item, "version", None),
                status="loaded",
                required=item.name in explicit,
                detail=None,
            )
            for item in summary.loaded
        ]
        rows += [
            ModuleView(
                name=item.name,
                version=None,
                status="failed",
                required=item.required,
                detail=safe_error(item.error),
            )
            for item in summary.failed
        ]
        rows += [
            ModuleView(
                name=item.name,
                version=None,
                status="skipped",
                required=False,
                detail=safe_error(item.reason),
            )
            for item in summary.skipped
        ]
        return rows

    @router.get("/settings", response_model=SettingsView, dependencies=_READ)
    async def runtime_settings() -> SettingsView:
        """Redacted runtime configuration: an allowlist of non-secret settings."""
        cfg = core.settings
        health = await core.infra.health_all()
        configured = core.infra.describe().get("instances", {})
        instances = []
        for key, result in sorted(health.items()):
            capability, _, name = str(key).partition(":")
            driver = configured.get(capability, {}).get(name, {}).get("driver")
            instances.append(
                InfraInstanceView(
                    capability=capability,
                    name=name or "default",
                    driver=driver,
                    status=result.status,
                )
            )
        return SettingsView(
            settings=[
                SettingView(key=key, value=_setting_text(getattr(cfg, key, None)))
                for key in _SAFE_SETTINGS
                if hasattr(cfg, key)
            ],
            infrastructure=instances,
        )


__all__ = ["install", "platform_operator", "safe_error"]
