"""Risk declared at its source, and what it triggers (ADR-0009 items 6-12;
ledger G-SENS-01..06, C-ROUTE-02), over HTTP with real PostgreSQL and Redis.

Independent oracles (spec §10.1): the HTTP status and headers, the rows in
the probe tables and in ``audit_log`` (read with plain SQL, not through the
kernel), never the kernel's own bookkeeping.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from sqlalchemy import Integer, String, UniqueConstraint, insert, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.core import di
from sandloom.core.http import route_runtime
from sandloom.core.module_api import contract_method, contract_session
from sandloom.core.module_api._provider import contract_schema
from sandloom.core.orm import MoneyLedger
from sandloom.core.route_policy import resolve_route_policy
from sandloom.core.sql_alchemy.money_ledger import MoneyLedgerError
from sandloom.core.testing import make_user_headers
from sandloom.core.testing.fixtures import *  # noqa: F403
from sandloom.shared.authz import OrgRole, Role, current_context, require
from sandloom.shared.guards.platform import require_platform_admin

pytestmark = pytest.mark.asyncio

PREFIX = "/__wsa_risk"
PROBE = "wsa_risk_probe"
LEDGER = "wsa_risk_ledger"


class _Base(DeclarativeBase):
    pass


class Probe(_Base):
    __tablename__ = PROBE

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    v: Mapped[str] = mapped_column(String(50))


class Ledger(MoneyLedger, _Base):
    __tablename__ = LEDGER
    __table_args__ = (UniqueConstraint("account", "operation_key"),)
    __operation_key__ = "operation_key"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String(50))
    operation_key: Mapped[str] = mapped_column(String(100), nullable=False)


@pytest.fixture
async def tables(db_engine: Any) -> AsyncIterator[None]:
    async with db_engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
        await conn.execute(text(f"DELETE FROM {PROBE}"))
        await conn.execute(text(f"DELETE FROM {LEDGER}"))
    yield


async def _rows(db_engine: Any, table: str) -> list[Any]:
    async with db_engine.connect() as conn:
        return list(await conn.execute(text(f"SELECT * FROM {table} ORDER BY id")))


async def _audits(db_engine: Any, actor: uuid.UUID) -> list[Any]:
    async with db_engine.connect() as conn:
        return list(
            await conn.execute(
                text(
                    "SELECT action, data FROM audit_log WHERE actor_id = :a ORDER BY id"
                ),
                {"a": actor},
            )
        )


@pytest.fixture
def mount(sandloom_app: FastAPI) -> Iterator[Callable[[APIRouter], None]]:
    before = list(sandloom_app.router.routes)

    def _mount(router: APIRouter) -> None:
        sandloom_app.include_router(router)
        new = [r for r in sandloom_app.router.routes if r not in before]
        route_runtime.install_route_runtime(new, sandloom_app.state.core)

    yield _mount
    sandloom_app.router.routes[:] = before


USER = [Depends(current_context)]


def _headers(*, auth_age: float | None = 0.0, **claims: Any) -> tuple[uuid.UUID, Any]:
    extra = dict(claims)
    if auth_age is not None:
        extra["auth_time"] = int(time.time() - auth_age)
    return make_user_headers(uuid.uuid4(), **extra)


# --------------------------------------------------------------------------- #
# G-SENS-01: the framework's admin gates carry the admin risk (static)
# --------------------------------------------------------------------------- #
def _policy_of(dependency: Any) -> Any:
    app = FastAPI()

    async def endpoint() -> None:
        return None

    app.add_api_route("/x", endpoint, methods=["POST"], dependencies=[dependency])
    (route,) = [r for r in app.routes if getattr(r, "path", "") == "/x"]
    return resolve_route_policy(route)


async def test_admin_gates_carry_the_admin_risk_and_member_gates_do_not() -> None:
    from sandloom_tenancy.access import require_org_role

    tagged = {
        "platform admin": Depends(require_platform_admin),
        "Role(admin)": Depends(require(Role("admin"))),
        "OrgRole(admin)": Depends(require(OrgRole("admin"))),
        "OrgRole(owner)": Depends(require(OrgRole("owner"))),
        "Role(admin) | Role(x)": Depends(require(Role("admin") | Role("x"))),
        "require_org_role(admin)": Depends(require_org_role("admin")),
    }
    for label, dependency in tagged.items():
        assert "admin" in _policy_of(dependency).risk, label
    untagged = {
        "OrgRole(member)": Depends(require(OrgRole("member"))),
        "Role(x)": Depends(require(Role("x"))),
        "~Role(admin)": Depends(require(~Role("admin"))),
        "require_org_role(member)": Depends(require_org_role("member")),
    }
    for label, dependency in untagged.items():
        assert _policy_of(dependency).risk == {}, label


async def test_every_platform_route_behind_an_admin_gate_resolves_the_admin_risk(
    sandloom_app: FastAPI,
) -> None:
    from sandloom.core.guard_order import resolution_order
    from sandloom.core.route_policy import api_routes

    gated = 0
    for route in api_routes(sandloom_app.routes):
        calls = list(resolution_order(route.dependant))
        if any(
            getattr(c, "__wrapped__", None) is not None
            and getattr(c, "__sandloom_risk__", None) == "admin"
            for c in calls
        ):
            gated += 1
            assert "admin" in resolve_route_policy(route).risk, route.path
    assert gated > 0


async def test_sensitive_marker_and_effect_tags_resolve_with_their_source() -> None:
    policy = _policy_of(di.sensitive("security"))
    assert policy.risk == {"security": ("sensitive()",)}
    assert policy.step_up_max_age_s == 300
    assert _policy_of(di.sensitive("money")).step_up_max_age_s is None
    with pytest.raises(Exception, match="risk must be one of"):
        di.sensitive("data")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="risk must be one of"):
        di.sensitive_effect("data")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="async"):
        di.sensitive_effect("money")(lambda: None)


# --------------------------------------------------------------------------- #
# G-SENS-02: money sources (command risk=, MoneyLedger)
# --------------------------------------------------------------------------- #
async def test_a_money_command_records_its_risk_in_the_contract_schema() -> None:
    class _Credits:
        @classmethod
        @contract_method(command=True, idempotency_key="operation_key", risk="money")
        async def spend(cls, amount: int, operation_key: str) -> int:
            return amount

    described = contract_schema(_Credits).describe()["methods"]["spend"]
    assert described["risk"] == "money"
    with pytest.raises(ValueError, match="command contracts only"):
        contract_method(risk="money")
    with pytest.raises(ValueError, match="risk must be one of"):
        contract_method(command=True, idempotency_key="k", risk="x")  # type: ignore[arg-type]


async def test_a_money_ledger_must_name_a_not_null_unique_operation_key() -> None:
    class _B(DeclarativeBase):
        pass

    with pytest.raises(MoneyLedgerError, match="names its operation-key"):

        class _NoKey(MoneyLedger, _B):
            __tablename__ = "wsa_l1"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)

    with pytest.raises(MoneyLedgerError, match="NOT NULL"):

        class _Nullable(MoneyLedger, _B):
            __tablename__ = "wsa_l2"
            __operation_key__ = "op"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            op: Mapped[str | None] = mapped_column(
                String(10), nullable=True, unique=True
            )

    with pytest.raises(MoneyLedgerError, match="UNIQUE"):

        class _NotUnique(MoneyLedger, _B):
            __tablename__ = "wsa_l3"
            __operation_key__ = "op"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            op: Mapped[str] = mapped_column(String(10), nullable=False)

    class _EitherOrder(_B, MoneyLedger):
        __tablename__ = "wsa_l4"
        __operation_key__ = "op"
        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        op: Mapped[str] = mapped_column(String(10), nullable=False, unique=True)

    from sandloom.core.sql_alchemy.money_ledger import MONEY_TABLES

    assert MONEY_TABLES["wsa_l4"] == "op"


# --------------------------------------------------------------------------- #
# G-SENS-04: one automatic audit row per sensitive write
# --------------------------------------------------------------------------- #
async def test_a_sensitive_write_gets_one_audit_row_and_a_read_or_failure_none(
    real_uow_client: Any, db_engine: Any, tables: None, mount: Any
) -> None:
    router = APIRouter(prefix=PREFIX, dependencies=USER)

    @router.post(
        "/items/{item_id}", status_code=201, dependencies=[di.sensitive("admin")]
    )
    async def write(item_id: str, fail: bool = False) -> dict[str, bool]:
        await di.db().execute(insert(Probe).values(v="secret-body-value"))
        await di.db().execute(insert(Probe).values(v="second"))
        if fail:
            raise HTTPException(409, "conflict")
        return {"ok": True}

    @router.get("/items/{item_id}", dependencies=[di.sensitive("admin")])
    async def read(item_id: str) -> dict[str, bool]:
        await di.db().execute(text("SELECT 1"))
        return {"ok": True}

    @router.post("/plain")
    async def plain() -> dict[str, bool]:
        await di.db().execute(insert(Probe).values(v="plain"))
        return {"ok": True}

    mount(router)
    uid, headers = _headers()
    resp = await real_uow_client.post(f"{PREFIX}/items/i-7", headers=headers)
    assert resp.status_code == 201
    audits = await _audits(db_engine, uid)
    assert len(audits) == 1
    action, data = audits[0]
    assert action == "sandloom.sensitive_write"
    assert data == {
        "route": f"{PREFIX}/items/{{item_id}}",
        "method": "POST",
        "path_params": {"item_id": "i-7"},
        "risks": {"admin": ["sensitive()"]},
        "status": 201,
    }
    assert "secret-body-value" not in str(data)

    assert (
        await real_uow_client.get(f"{PREFIX}/items/i-7", headers=headers)
    ).status_code == 200
    failed = await real_uow_client.post(
        f"{PREFIX}/items/i-8?fail=true", headers=headers
    )
    assert failed.status_code == 409
    assert (
        await real_uow_client.post(f"{PREFIX}/plain", headers=headers)
    ).status_code == 200
    assert len(await _audits(db_engine, uid)) == 1


@pytest.mark.parametrize("risk", ["admin", "money"])
async def test_injected_session_keeps_risk_bound_through_dependency_commit(
    real_uow_client: Any, db_engine: Any, tables: None, mount: Any, risk: str
) -> None:
    """DbSession commits on dependency teardown, before the response. It must
    still see the request's admission: audit admin writes, refuse unkeyed money."""
    router = APIRouter(prefix=PREFIX, dependencies=USER)

    @router.post("/injected", status_code=201, dependencies=[di.sensitive(risk)])
    async def write(db: di.DbSession) -> dict[str, bool]:
        await db.execute(insert(Probe).values(v="injected"))
        return {"ok": True}

    mount(router)
    uid, headers = _headers()
    response = await real_uow_client.post(f"{PREFIX}/injected", headers=headers)
    audits = await _audits(db_engine, uid)
    rows = await _rows(db_engine, PROBE)
    if risk == "admin":
        assert response.status_code == 201, response.text
        assert len(rows) == len(audits) == 1
        assert audits[0][0] == "sandloom.sensitive_write"
        assert set(audits[0][1]["risks"]) == {"admin"}
    else:
        assert response.status_code == 428, response.text
        assert rows == audits == []


class _WsaCredits:
    @classmethod
    @contract_method(command=True, idempotency_key="operation_key", risk="money")
    async def spend(cls, amount: int, operation_key: str) -> int:
        await contract_session().execute(insert(Probe).values(v=f"spend:{amount}"))
        return amount


async def test_a_command_only_money_route_gets_its_audit_row(
    real_uow_client: Any,
    db_engine: Any,
    tables: None,
    mount: Any,
    sandloom_app: FastAPI,
) -> None:
    """A POST that only invokes a risk="money" command (no request unit, no
    Idempotency-Key) commits the command and writes its one mandatory audit
    row (the runtime opens the request unit for it); a request unit that only
    read gets it too."""
    core = sandloom_app.state.core
    contract = "wsa.credits.v1"
    if not core.service_registry.contains(contract):
        core.register_service(contract, _WsaCredits)
    router = APIRouter(prefix=PREFIX, dependencies=USER)

    @router.post("/spend/{order}", dependencies=[di.no_idempotency(reason="t")])
    async def spend(order: str) -> dict[str, int]:
        credits = core.get_service(contract)
        return {"spent": await credits.spend(amount=3, operation_key=order)}

    @router.post(
        "/spend-after-read/{order}", dependencies=[di.no_idempotency(reason="t")]
    )
    async def spend_after_read(order: str) -> dict[str, int]:
        await di.db().execute(text("SELECT 1"))
        credits = core.get_service(contract)
        return {"spent": await credits.spend(amount=4, operation_key=order)}

    mount(router)
    uid, headers = _headers()
    resp = await real_uow_client.post(f"{PREFIX}/spend/o-1", headers=headers)
    assert resp.status_code == 200, resp.text
    assert [r.v for r in await _rows(db_engine, PROBE)] == ["spend:3"]
    audits = await _audits(db_engine, uid)
    assert len(audits) == 1
    action, data = audits[0]
    assert action == "sandloom.sensitive_write"
    assert data["route"] == f"{PREFIX}/spend/{{order}}"
    assert data["path_params"] == {"order": "o-1"}
    assert data["risks"] == {"money": [f"command:{contract}"]}

    resp = await real_uow_client.post(f"{PREFIX}/spend-after-read/o-2", headers=headers)
    assert resp.status_code == 200, resp.text
    assert len(await _audits(db_engine, uid)) == 2


# --------------------------------------------------------------------------- #
# G-SENS-05: money dedupe is mandatory; a client without a key gets 428
# --------------------------------------------------------------------------- #
@di.sensitive_effect("money")
async def _charge(amount: int, *, operation_key: str | None = None) -> None:
    await di.db().execute(insert(Probe).values(v=f"charge:{amount}"))


async def test_a_money_effect_needs_a_dedupe_record_or_answers_428(
    real_uow_client: Any, db_engine: Any, tables: None, mount: Any
) -> None:
    router = APIRouter(prefix=PREFIX, dependencies=USER)
    guard = Depends(di.idempotency_guard(scope="wsa.charge", fail_closed=True))

    @router.post("/charge-guarded", dependencies=[guard])
    async def guarded() -> dict[str, str | None]:
        await _charge(5)
        return {"key": di.request_operation_key()}

    @router.post("/charge-unguarded", dependencies=[di.no_idempotency(reason="t")])
    async def unguarded() -> dict[str, bool]:
        await _charge(6)
        return {"ok": True}

    @router.post("/charge-own-key", dependencies=[di.no_idempotency(reason="t")])
    async def own_key() -> dict[str, bool]:
        await _charge(7, operation_key="order-42")
        return {"ok": True}

    mount(router)
    uid, headers = _headers()
    missing = await real_uow_client.post(f"{PREFIX}/charge-guarded", headers=headers)
    assert missing.status_code in (400, 428)  # the guard's own key check, or 428
    no_key = await real_uow_client.post(f"{PREFIX}/charge-unguarded", headers=headers)
    assert no_key.status_code == 428
    assert no_key.json()["detail"] == "Idempotency-Key required for this operation"
    ok = await real_uow_client.post(
        f"{PREFIX}/charge-guarded", headers={**headers, "Idempotency-Key": "k-1"}
    )
    assert ok.status_code == 200 and ok.json()["key"]
    assert (
        await real_uow_client.post(f"{PREFIX}/charge-own-key", headers=headers)
    ).status_code == 200
    assert [r.v for r in await _rows(db_engine, PROBE)] == ["charge:5", "charge:7"]
    audits = await _audits(db_engine, uid)
    assert len(audits) == 2
    assert all(list(d["risks"]) == ["money"] for _, d in audits)


@pytest.mark.sandloom_allow_server_errors
async def test_a_key_the_route_cannot_dedupe_is_a_developer_error(
    real_uow_client: Any, db_engine: Any, tables: None, mount: Any
) -> None:
    router = APIRouter(prefix=PREFIX, dependencies=USER)

    @router.post("/charge-opted-out", dependencies=[di.no_idempotency(reason="t")])
    async def opted_out() -> dict[str, bool]:
        await _charge(8)
        return {"ok": True}

    mount(router)
    _uid, headers = _headers()
    resp = await real_uow_client.post(
        f"{PREFIX}/charge-opted-out", headers={**headers, "Idempotency-Key": "k-2"}
    )
    assert resp.status_code == 500  # SANDLOOM-R009, nothing committed
    assert await _rows(db_engine, PROBE) == []


async def test_a_money_route_commits_only_with_a_deduped_money_effect_or_a_key(
    real_uow_client: Any, db_engine: Any, tables: None, mount: Any
) -> None:
    router = APIRouter(
        prefix=PREFIX,
        dependencies=[*USER, di.sensitive("money"), di.no_idempotency(reason="t")],
    )

    @router.post("/adjust")
    async def adjust(ledger: bool = False) -> dict[str, bool]:
        if ledger:
            await di.db().execute(
                insert(Ledger).values(account="a", operation_key=str(uuid.uuid4()))
            )
        else:
            await di.db().execute(insert(Probe).values(v="no dedupe"))
        return {"ok": True}

    mount(router)
    uid, headers = _headers()
    refused = await real_uow_client.post(f"{PREFIX}/adjust", headers=headers)
    assert refused.status_code == 428
    assert await _rows(db_engine, PROBE) == []
    ok = await real_uow_client.post(f"{PREFIX}/adjust?ledger=true", headers=headers)
    assert ok.status_code == 200
    assert len(await _rows(db_engine, LEDGER)) == 1
    ((_, data),) = await _audits(db_engine, uid)
    assert data["risks"] == {"money": ["ledger:wsa_risk_ledger", "sensitive()"]}


# --------------------------------------------------------------------------- #
# G-SENS-06: RFC 9470 step-up on auth_time
# --------------------------------------------------------------------------- #
@di.sensitive_effect("security")
async def _change_credential(password_ok: bool) -> None:
    if password_ok:
        di.mark_reauthenticated()  # the effect's own body re-authenticated
    await di.db().execute(insert(Probe).values(v="credential changed"))


async def test_a_security_effect_needs_a_recent_auth_time_or_a_reauthentication(
    real_uow_client: Any, db_engine: Any, tables: None, mount: Any
) -> None:
    router = APIRouter(prefix=PREFIX, dependencies=USER)

    @router.post("/credential")
    async def credential(password_ok: bool = False) -> dict[str, bool]:
        await _change_credential(password_ok)
        return {"ok": True}

    mount(router)
    for age in (None, 301.0):
        _uid, headers = _headers(auth_age=age)
        resp = await real_uow_client.post(f"{PREFIX}/credential", headers=headers)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == (
            'Bearer error="insufficient_user_authentication", max_age=300'
        )
    assert await _rows(db_engine, PROBE) == []
    _uid, fresh = _headers(auth_age=10.0)
    assert (
        await real_uow_client.post(f"{PREFIX}/credential", headers=fresh)
    ).status_code == 200
    _uid, old = _headers(auth_age=3600.0)
    assert (
        await real_uow_client.post(f"{PREFIX}/credential?password_ok=true", headers=old)
    ).status_code == 200
    assert len(await _rows(db_engine, PROBE)) == 2


async def test_a_security_route_refuses_before_the_response_even_on_a_read(
    real_uow_client: Any, mount: Any
) -> None:
    router = APIRouter(prefix=PREFIX, dependencies=[*USER, di.sensitive("security")])

    @router.get("/recovery-codes")
    async def codes(password_ok: bool = False) -> dict[str, str]:
        if password_ok:
            di.mark_reauthenticated()
        return {"codes": "secret"}

    mount(router)
    _uid, old = _headers(auth_age=3600.0)
    refused = await real_uow_client.get(f"{PREFIX}/recovery-codes", headers=old)
    assert refused.status_code == 401 and "secret" not in refused.text
    assert "insufficient_user_authentication" in refused.headers["www-authenticate"]
    ok = await real_uow_client.get(
        f"{PREFIX}/recovery-codes?password_ok=true", headers=old
    )
    assert ok.status_code == 200
    _uid, fresh = _headers(auth_age=1.0)
    assert (
        await real_uow_client.get(f"{PREFIX}/recovery-codes", headers=fresh)
    ).status_code == 200
