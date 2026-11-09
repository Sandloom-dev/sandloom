"""The kernel's part of ``sandloom security revoke-sessions``, against PostgreSQL.

B-R5 as a platform guarantee: whatever the issuer, the command revokes every
Sandloom access token of its targets itself, in ONE unit of work of the
process it runs in: a durable claim of the batch, an audit row per user, the
revocation mark written before commit (``require_immediate``) and the
``core.auth.tokens_revoked.v1`` record in the same outbox. That record is
what reaches the other services of a split: each one's revocation applier
(``sandloom:auth@tokens_revoked@<service>``) writes it into its own cache.
Checked here with a CLI process that does not load the issuer (the issuer
answers over in-process RPC), a second service's cache, a cache outage
(refused, nothing committed) and a password reset the issuer cannot do
(refused before anything: no audit row, no mark, no outbox record).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import sandloom.core.cli.security as security_cli
import sandloom.core.infra.access as access
import sandloom.core.rpc.proxy as rpc_proxy
from sandloom.core.app_factory.claim_authorities import (
    build_claim_authorities,
    filter_to_composition,
)
from sandloom.core.app_factory.revocation_applier import apply_token_revocation
from sandloom.core.auth import marks, tokens, verification
from sandloom.core.claims import Authority
from sandloom.core.cli._operator import OperatorIntent
from sandloom.core.cli.operator_owner import OperatorCapabilityUnavailable
from sandloom.core.cli.security import (
    AUDIT_TOKENS_REVOKED_ACTION,
    Targets,
    revoke_access_tokens,
    run_bulk,
    summary_line,
)
from sandloom.core.config.settings import settings
from sandloom.core.contract_testing import (
    in_process_rpc_client,
    rpc_proxy_for,
    rpc_settings,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_method
from sandloom.core.testing import InMemoryCache, committing_uow
from sandloom.core.testing.fixtures import *  # noqa: F403

IDP = "oidcapp.modules.acme_idp"
SVC = "svc-idp"
CONTRACT = "acme_idp.security.v1"
APPLY = OperatorIntent(apply=True, operator="al", reason="token leak SEC-11")
DRY = OperatorIntent(apply=False, operator="al", reason="")
OIDC_MAP: dict[str, str | Authority] = {"issuer": Authority("acme_idp", module=IDP)}


class _AcmeSecurity:
    """A stateless issuer: sessions, no passwords."""

    @classmethod
    @contract_method
    async def end_sessions(
        cls, user_ids: list[uuid.UUID], reason: str, operator: str, operation_key: str
    ) -> dict[str, Any]:
        """Its sessions are the access tokens it minted."""
        return {"affected": len(user_ids), "ended": ["access_tokens"]}


class _FakeUoW:
    def __init__(self) -> None:
        self.session: Any = None

    async def __aenter__(self) -> _FakeUoW:
        self.session = SimpleNamespace(info={})
        return self

    async def __aexit__(self, *_: Any) -> None:
        self.session = None


class _NoDatabase:
    """The issuer's read unit (it writes nothing)."""

    def uow_factory(self) -> Any:
        return _FakeUoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


@pytest.fixture
def sessions(db_engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
def cli_cache(monkeypatch: pytest.MonkeyPatch) -> InMemoryCache:
    """The CLI process's security cache."""
    fake = InMemoryCache()
    monkeypatch.setattr(access, "cache", lambda name="default": fake)
    return fake


@pytest.fixture
def cli_unit(
    monkeypatch: pytest.MonkeyPatch, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The CLI's ``db_session()``: a real committing unit on the test database."""
    monkeypatch.setattr(
        security_cli, "db_session", lambda name="default": committing_uow(sessions)
    )


def _claims(sub: Any) -> dict[str, Any]:
    claims = tokens.decode_token(
        tokens.create_access_token({"sub": str(sub), "token_type": "access"})
    )
    assert claims is not None
    return claims


async def _rows(
    sessions: async_sessionmaker[AsyncSession], ids: list[uuid.UUID]
) -> tuple[list[Any], list[dict[str, Any]]]:
    """``(audit rows, outbox envelopes)`` of the kernel's revocation of ``ids``."""
    wanted = [str(u) for u in ids]
    async with sessions() as db:
        audit = (
            await db.execute(
                text(
                    "SELECT resource_id, actor_type, data FROM audit_log "
                    "WHERE action = :a ORDER BY resource_id"
                ),
                {"a": AUDIT_TOKENS_REVOKED_ACTION},
            )
        ).all()
        outbox = (
            await db.execute(
                text(
                    "SELECT id, payload FROM outbox_messages WHERE channel = :c "
                    "ORDER BY emitted_at"
                ),
                {"c": marks.TOKENS_REVOKED_CHANNEL},
            )
        ).all()
    return (
        [r for r in audit if r.resource_id in wanted],
        [
            {"id": str(r.id), "payload": r.payload}
            for r in outbox
            if r.payload.get("user_id") in wanted
        ],
    )


async def test_the_kernel_part_is_one_durable_unit_every_service_applies(
    sessions: async_sessionmaker[AsyncSession],
    cli_cache: InMemoryCache,
    cli_unit: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = sorted(uuid.uuid4() for _ in range(2))
    old = [_claims(u) for u in ids]
    key = f"sandloom-security:revoke-sessions:op-{uuid.uuid4().hex}:batch"
    assert await revoke_access_tokens(
        ids, command="revoke-sessions", intent=APPLY, key=key
    )
    # This process: the mark was written before commit.
    assert all([await verification.is_token_revoked(c) for c in old])
    audit, outbox = await _rows(sessions, ids)
    assert [(r.resource_id, r.actor_type) for r in audit] == [
        (str(u), "operator") for u in ids
    ]
    assert audit[0].data["operation_key"] == key
    assert audit[0].data["reason"] == "token leak SEC-11"
    assert sorted(e["payload"]["user_id"] for e in outbox) == [str(u) for u in ids]

    # Another service of the split, with its own cache: the relayed record,
    # applied by its revocation applier (at-least-once: twice), revokes there.
    other = InMemoryCache()
    monkeypatch.setattr(access, "cache", lambda name="default": other)
    assert not await verification.is_token_revoked(old[0])
    for envelope in outbox:
        await apply_token_revocation(None, envelope)
        await apply_token_revocation(None, envelope)
    assert all([await verification.is_token_revoked(c) for c in old])

    # A re-run of the operation replays: claimed once, nothing written again.
    monkeypatch.setattr(access, "cache", lambda name="default": cli_cache)
    assert not await revoke_access_tokens(
        ids, command="revoke-sessions", intent=APPLY, key=key
    )
    assert [len(x) for x in await _rows(sessions, ids)] == [2, 2]


async def test_a_cache_outage_refuses_and_commits_nothing(
    sessions: async_sessionmaker[AsyncSession],
    cli_cache: InMemoryCache,
    cli_unit: None,
) -> None:
    ids = [uuid.uuid4()]
    key = f"sandloom-security:revoke-sessions:op-{uuid.uuid4().hex}:batch"
    cli_cache.broken = True
    with pytest.raises(marks.RevocationUnavailableError):
        await revoke_access_tokens(
            ids, command="revoke-sessions", intent=APPLY, key=key
        )
    assert await _rows(sessions, ids) == ([], [])
    # Nothing was claimed either: the re-run acts.
    cli_cache.broken = False
    assert await revoke_access_tokens(
        ids, command="revoke-sessions", intent=APPLY, key=key
    )


def _provider() -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _NoDatabase()  # type: ignore[method-assign]
    with core.module_registration(IDP) as api:
        api.register_service(CONTRACT, _AcmeSecurity)
    core._claim_authorities = build_claim_authorities(OIDC_MAP)
    return core


@pytest.fixture
def keyed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    allowed = {SVC: {CONTRACT: ["end_sessions", "require_password_reset"]}}
    for name, value in rpc_settings(SVC, allowed_calls=allowed).items():
        monkeypatch.setattr(settings, name, value)
    pinned = {CONTRACT: {"base_url": "http://sandloom-conformance", "service_id": SVC}}
    monkeypatch.setattr(settings, "service_bindings", pinned)
    rpc_proxy._remote_contracts.clear()
    yield
    rpc_proxy._remote_contracts.clear()


def _cli_without_issuer(client: Any, provider: CoreFacade) -> Any:
    return SimpleNamespace(
        claim_authorities=filter_to_composition(
            build_claim_authorities(OIDC_MAP),
            loaded={},
            bindings=settings.service_bindings,
        ),
        module_descriptors=[],
        service_registry=SimpleNamespace(try_get=lambda name: None),
        settings=settings,
        get_service=lambda name: rpc_proxy_for(provider, name, client),
    )


async def test_a_cli_process_without_the_issuer_revokes_durably(
    sessions: async_sessionmaker[AsyncSession],
    cli_cache: InMemoryCache,
    cli_unit: None,
    keyed: None,
) -> None:
    ids = sorted(uuid.uuid4() for _ in range(3))
    old = [_claims(u) for u in ids]
    provider = _provider()
    async with in_process_rpc_client(provider) as client:
        result = await run_bulk(
            _cli_without_issuer(client, provider),
            "revoke-sessions",
            Targets(users=tuple(ids)),
            APPLY,
            operation_id=uuid.uuid4().hex,
        )
    audit, outbox = await _rows(sessions, ids)
    assert len(audit) == len(outbox) == 3
    assert all([await verification.is_token_revoked(c) for c in old])
    assert summary_line(result).startswith("revoke-sessions: 3 user(s) affected")
    assert "  refresh sessions not applicable: acme_idp holds none" in result.lines()


@pytest.mark.parametrize("intent", [DRY, APPLY], ids=["dry-run", "apply"])
async def test_a_reset_the_issuer_cannot_do_leaves_no_trace(
    sessions: async_sessionmaker[AsyncSession],
    cli_cache: InMemoryCache,
    cli_unit: None,
    keyed: None,
    intent: OperatorIntent,
) -> None:
    ids = [uuid.uuid4()]
    provider = _provider()
    async with in_process_rpc_client(provider) as client:
        with pytest.raises(OperatorCapabilityUnavailable, match="no require_password"):
            await run_bulk(
                _cli_without_issuer(client, provider),
                "force-password-reset",
                Targets(users=tuple(ids)),
                intent,
                operation_id=uuid.uuid4().hex,
            )
    assert await _rows(sessions, ids) == ([], [])
    assert marks.subject_key(ids[0]) not in cli_cache.store
