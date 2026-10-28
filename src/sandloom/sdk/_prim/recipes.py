"""Task recipes shipped with the framework: ``sandloom docs recipes`` lists them,
``sandloom docs recipe <name>`` prints one.

An application has no copy of the framework's ``docs/``; these are the
how-tos an agent in an app needs and would otherwise search the installed
package for. A recipe is verified by running it in a generated application
(``tools/smoke/recipes_check.py``, CI job ``app-recipes``): its ``uv run sandloom
add|migrate new`` lines run in order and every ``# file: <path>`` block is
written (``recipe_parts``), then ``sandloom test`` must pass. Placeholders:
``<app>``, ``<module>`` / ``<Module>`` (the module and its CamelCase name,
whose scaffolded table is ``<Module>Item``), ``<consumer>`` / ``<Consumer>``.
"""

from __future__ import annotations

import re

_RECIPES: dict[str, tuple[str, str]] = {
    "org-gate": (
        "an organization route for paid admins, and its test",
        """The gate runs before the handler. In tests, fake the membership
contract and the subscription fact; no tenancy or billing data is needed.

    from fastapi import Depends
    from sandloom.sdk import CurrentUser, OrgRole, SubscriptionActive, require
    from sandloom.sdk.authz import Account  # sandloom: advanced billing account

    ORG = Account.org(param="org_id")
    PAID_ADMIN = require(OrgRole("admin") & SubscriptionActive(of=ORG))

    @router.get("/orgs/{org_id}/report", dependencies=[Depends(PAID_ADMIN)])
    async def report(org_id: UUID, user: CurrentUser) -> ReportOut: ...

Test (200 for an admin of a paid org, 403 for a member, 402 when unpaid):

    from sandloom.sdk.testing import as_user, fake_access_fact, fake_contract
    from sandloom.shared.contracts.tenancy_membership_v1 import TENANCY_MEMBERSHIP_V1

    RANK = {"member": 1, "admin": 2, "owner": 3}

    class Roles:  # the membership contract, answering from a table
        def __init__(self, roles): self.roles = roles  # {(org, user): role}
        async def has_role_at_least(self, organization_id, user_id, minimum):
            role = self.roles.get((organization_id, user_id))
            return role is not None and RANK[role] >= RANK[minimum]

    async def test_report(client):
        org = uuid4()
        admin, admin_h = as_user(tenant_id=org)
        member, member_h = as_user(tenant_id=org)
        url = f"/api/v1/<module>/orgs/{org}/report"
        roles = Roles({(org, admin): "admin", (org, member): "member"})
        with fake_contract(TENANCY_MEMBERSHIP_V1, roles):
            with fake_access_fact("organization.subscription.active", True):
                assert (await client.get(url, headers=admin_h)).status_code == 200
                assert (await client.get(url, headers=member_h)).status_code == 403
            with fake_access_fact("organization.subscription.active", False):
                assert (await client.get(url, headers=admin_h)).status_code == 402

A personal gate is `Subscribed(of=Account.user())`, faked with
`fake_access_fact("subscription.active", ...)`.""",
    ),
    "retries": (
        "what a retried write returns, and how to test it",
        """A write with an `Idempotency-Key` header runs once. A repeat with the
same key answers the first status with an EMPTY body and
`Idempotent-Replay: true`. `Idempotent-Replay-Truncated: true` only says
that the body is not replayed, by design (a replayed body could leak data
whose visibility changed). Test the effect, not the body:

    keyed = {**headers, "Idempotency-Key": uuid4().hex}
    first = await client.post(url, json=body, headers=keyed)
    again = await client.post(url, json=body, headers=keyed)
    assert again.status_code == first.status_code
    assert again.headers["Idempotent-Replay"] == "true"
    assert len((await client.get(list_url, headers=headers)).json()) == 1

A route whose body is safe to replay (only the caller's own fields and the
new id) declares `dependencies=[Depends(replay_safe(reason="..."))]`
(`sandloom.sdk.web`).""",
    ),
    "list-order": (
        "ids, timestamps and a list order with no tie (newest first)",
        """Map ids and times with the framework's idiom, as `sandloom add module` does:

    import uuid

    from sqlalchemy.orm import Mapped, mapped_column

    from sandloom.sdk import Base, CreatedAt, TenantScoped, new_id
    from sandloom.sdk.data import UpdatedAt  # sandloom: advanced last-write time

    class Note(TenantScoped, Base):
        __tablename__ = "<module>_notes"

        id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
        created_at: Mapped[CreatedAt]
        updated_at: Mapped[UpdatedAt]  # only on a row that changes

and list newest first by the database clock, then the id:

    query = sa.select(Note).order_by(Note.created_at.desc(), Note.id.desc())

Why: `server_default=func.now()` stamps every row of one transaction with
the transaction's start, so they tie and come back in any order (a request
that inserts several rows, and every test: the `client` fixture runs a test
in one transaction). `CreatedAt` / `UpdatedAt` default to
`clock_timestamp()`, the time of the statement; `sandloom migrate new` renders
`server_default=sa.text("clock_timestamp()")`. `new_id()` is a UUIDv7:
time-ordered, and ids one process makes in the same millisecond still
increase, so the `id` breaks the rare tie of one microsecond. Keep
`uuid.uuid4` for an id that must not reveal its creation time (a credential,
an unguessable link).

Test the order inside one transaction, the way it fails otherwise:

    for title in ("a", "b", "c"):
        await client.post(url, json={"title": title}, headers=headers)
    listed = await client.get(url, headers=headers)
    assert [n["title"] for n in listed.json()] == ["c", "b", "a"]

An existing `func.now()` column: change the model, then `uv run sandloom migrate
new <module> "..."` and write the default change yourself (autogenerate does
not compare defaults): `op.alter_column("<table>", "created_at",
server_default=sa.text("clock_timestamp()"))`.""",
    ),
    "projection-rebuild": (
        "a read model across modules must be rebuildable",
        """A projection (`sandloom add projection`) is a cache of other modules'
events, so it must be rebuildable from their public read contracts: give the
`Projection` a `rebuild_from=` naming the source contract method that lists
the current state, then `sandloom projection rebuild <name>` refills it. Each
source group orders by its own version field; a source without a version
cannot feed a projection (refused at definition, with the alternative).
A worked example, with its tests: `sandloom docs recipe
projection-from-platform-events`.""",
    ),
    "split": (
        "run the app as microservices: no code change",
        """`sandloom new` writes only the monolith profile. `sandloom add deploy` creates
the microservices profile (`deploy/compose/microservices.yml`), its
gateway (`deploy/gateway/nginx.conf`) and the database init script from the
framework's templates. Never hand-write them. One command per service:

    uv run sandloom add deploy identity                  # service "identity"
    uv run sandloom add deploy tenancy
    uv run sandloom add deploy billing
    uv run sandloom add deploy catalog orders --service shop

Each service gets its own database, a migrate one-shot, its RPC keys, a
worker for `tasks.py` and one beat for periodic tasks. Every run re-derives,
from what the modules provide and consume:

- each service's SERVICE_BINDINGS, plus the token issuer's remote
  `<m>.claims.v1`;
- RPC_ALLOWED_CALLS;
- the gateway routes (never /_sandloom, /diagnostics or /metrics).

The same command again changes nothing. Add a module to a service with
`sandloom add deploy <m> --service <svc>`. After a code change (a new consumed
contract, a new route), run `sandloom add deploy --refresh`.

The command writes only between the `# >>> generated by sandloom add deploy`
markers. Edit outside them (or in a compose override file). An edit inside
makes the next run refuse with a diff; `--force` overwrites it.

Check, then run (the dev keys are DEVELOPMENT ONLY, in a plain file):

    uv run sandloom add deploy --check     # invariants + every service boots alone
    uv run sandloom test                   # schema & deploy checks it too (SANDLOOM-T014)
    uv run sandloom keys dev-env --out deploy/.env.microservices
    docker compose --env-file deploy/.env.microservices \\
      -f deploy/compose/microservices.yml up --build

Production keys come from `sandloom keys generate` (the JWT pair; `--purpose rpc
--service <caller>`, `--purpose rpc-dh --service <provider>`); each
`${VAR:?...}` in the profile names its command. The monolith profile
(`deploy/docker-compose.yml`) keeps working unchanged.""",
    ),
    "test-service-or-task": (
        "test code that calls db() outside a request: a service, a task body",
        '''`db()` is the session of the running unit. A request opens one; a
test that calls a service, a Celery task or a script has none, and `db()`
raises `UnitOfWorkRequiredError` (SANDLOOM-R002). In a test:

- a service: request the `db_uow` fixture (the test's rolled-back session
  is the unit) and bind the tenant with `bind_tenant(...)` for a
  `TenantScoped` table (without one, an insert raises `MissingTenantError`);
- a Celery task body: call the task from a plain `def` test (no running
  loop), so it runs `run_async(...)` like the worker; its `open_uow()`
  blocks COMMIT to the test database (request `db_engine`: it empties every
  table before the test);
- a consumer: `await deliver(Event(...))` drives the real consumer runtime.

`<module>` is your module and `<Module>Item` the table `sandloom add module`
scaffolds. A service, and a task that runs it for one tenant:

    uv run sandloom add task <module> count_items

    # file: <module>/stats.py
    """Item counts: called from routes, tasks and consumers alike."""

    import sqlalchemy as sa

    from sandloom.sdk import db

    from .models.item import <Module>Item


    async def count_tenant_items() -> int:
        """The bound tenant's items (TenantScoped filters by it)."""
        rows = await db().scalars(sa.select(<Module>Item.id))
        return len(rows.all())

    # file: <module>/tasks.py
    """Background tasks of <module> (the framework worker imports this file)."""

    from uuid import UUID

    from sandloom.sdk.data import open_uow  # sandloom: advanced task unit
    from sandloom.sdk.effects import run_async  # sandloom: advanced task body
    from sandloom.sdk.tasks import shared_task  # sandloom: advanced tasks
    from sandloom.sdk.tenant import bind_tenant  # sandloom: advanced the task's tenant

    from .stats import count_tenant_items


    async def _count_items(tenant_id: UUID) -> int:
        with bind_tenant(tenant_id):
            async with open_uow():
                return await count_tenant_items()


    @shared_task(
        name="<app>.modules.<module>.tasks.count_items",
        acks_late=True,
        reject_on_worker_lost=True,
    )
    def count_items(tenant_id: str) -> int:
        """Task arguments travel as JSON: the tenant id is a string."""
        return run_async(_count_items(UUID(tenant_id)))

    # file: <module>/tests/test_outside_a_request.py
    """A service and a task body, tested without a request."""

    from typing import Any
    from uuid import UUID, uuid4

    import pytest

    from sandloom.sdk import db
    from sandloom.sdk.data import open_uow  # sandloom: advanced the test's own unit
    from sandloom.sdk.effects import run_async  # sandloom: advanced no running loop
    from sandloom.sdk.errors import UnitOfWorkRequiredError  # sandloom: advanced SANDLOOM-R002
    from sandloom.sdk.tenant import bind_tenant  # sandloom: advanced tenant rows

    from ..models.item import <Module>Item
    from ..stats import count_tenant_items
    from ..tasks import count_items


    async def add_items(tenant: UUID, n: int) -> None:
        with bind_tenant(tenant):  # a TenantScoped row needs a bound tenant
            for _ in range(n):
                db().add(<Module>Item(title="x", owner_id=uuid4()))
            await db().flush()


    async def test_a_service_runs_in_the_db_uow_unit(db_uow: Any) -> None:
        a, b = uuid4(), uuid4()
        await add_items(a, 2)
        await add_items(b, 1)
        with bind_tenant(a):
            assert await count_tenant_items() == 2


    def test_db_outside_any_unit_is_refused() -> None:
        with pytest.raises(UnitOfWorkRequiredError):
            db()


    def test_the_task_body_runs_through_run_async(db_engine: Any) -> None:
        tenant = uuid4()

        async def given() -> None:
            async with open_uow():  # committed: the task reads it
                await add_items(tenant, 3)

        run_async(given())
        assert count_items(str(tenant)) == 3
        assert count_items(str(uuid4())) == 0

Cross-tenant work (a purge, a report over every tenant) runs in
`with unscoped():` instead of `bind_tenant` (`sandloom docs recipe
periodic-task`).''',
    ),
    "call-contract": (
        "call another module's contract: Contract(REF), its errors, faking it",
        '''A module calls another module only through its contract, never its
code. `uv run sandloom docs contracts` lists the stubs; `uv run sandloom docs <name>`
prints every method's signature and semantics (`sandloom docs
billing.credits.v1`). The bundled modules' stubs import from
`sandloom.shared.contracts.<name>`, your app's from `<app>.shared.contracts.<name>`
(`sandloom generate` writes them from the provider, with the copies of its DTOs
the stub names: never edit one; a contract you never released may change
freely: delete its stub and run `sandloom generate`).

Declare the caller at module level: `define_module` adds it to the module's
`consumes`, and startup fails when a required provider is missing
(SANDLOOM-B005). Here a route spends one credit of the organization per export:

    # file: <module>/exports.py
    """Paid exports: one credit of the organization each."""

    from uuid import UUID

    from sandloom.sdk import Contract
    from sandloom.shared.contracts.billing_credits_v1 import BILLING_CREDITS_V1

    credits = Contract(BILLING_CREDITS_V1)


    async def charge_export(organization_id: UUID, export_id: UUID) -> int:
        """Spend one credit; returns the balance left. The key makes a retry
        free: the same key never spends twice."""
        return await credits.spend(
            "organization", organization_id, 1, f"export:{export_id}", reason="export"
        )

    # file: <module>/api.py
    """Routes of <module>: ``/api/v1/<module>/...``."""

    from uuid import uuid4

    from sandloom.sdk import CurrentUser, module_router
    from sandloom.sdk.tenant import current_tenant_id  # sandloom: advanced the organization

    from .exports import charge_export

    router = module_router()


    @router.post("/exports", status_code=201)
    async def create_export(user: CurrentUser) -> dict[str, str | int]:
        export_id = uuid4()
        balance = await charge_export(current_tenant_id(), export_id)
        return {"export_id": str(export_id), "balance": balance}

What a call raises, the same in a monolith and over RPC:

- the provider's refusal: `HTTPException` with its status (`spend`: 402
  not enough credits, 409 the key reused for another amount, 403 another
  account, 422 bad input). In a route, let it propagate: the client gets
  that status. Catch it only to translate it
  (`except HTTPException as exc: if exc.status_code == 402: ...`);
- the provider deployed but unreachable (a split app): 503;
- a command is NOT atomic with your unit: it committed in the provider when
  it returned, even if your unit rolls back later. Key it (`operation_key`)
  so a retry is free, or make the pair a saga step (`sandloom add saga`).

`Contract(REF, optional=True).client()` is `None` only when the provider is
not deployed at all: use it only where you have a correct answer without it.

Tests fake the provider: a class with the methods your code calls.

    # file: <module>/tests/test_<module>.py
    """Calling billing's credits contract, faked: no billing data needed."""

    from typing import Any
    from uuid import UUID, uuid4

    from fastapi import HTTPException

    from sandloom.sdk.testing import as_user, fake_contract
    from sandloom.shared.contracts.billing_credits_v1 import BILLING_CREDITS_V1

    URL = "/api/v1/<module>/exports"


    class Credits:
        """billing.credits.v1, faked: only the method the code calls."""

        def __init__(self, balance: int) -> None:
            self.balance = balance
            self.keys: list[str] = []

        async def spend(
            self,
            tenant_type: str,
            tenant_id: UUID,
            amount: int,
            operation_key: str,
            *,
            reason: str = "",
        ) -> int:
            if amount > self.balance:
                raise HTTPException(status_code=402, detail="not enough credits")
            self.keys.append(operation_key)
            self.balance -= amount
            return self.balance


    async def test_an_export_spends_one_credit(client: Any) -> None:
        _, headers = as_user(tenant_id=uuid4())
        credits = Credits(balance=2)
        with fake_contract(BILLING_CREDITS_V1, credits):
            created = await client.post(URL, headers=headers)
        assert created.status_code == 201, created.text
        assert created.json()["balance"] == 1
        assert credits.keys == [f"export:{created.json()['export_id']}"]


    async def test_without_credits_the_client_gets_402(client: Any) -> None:
        _, headers = as_user(tenant_id=uuid4())
        with fake_contract(BILLING_CREDITS_V1, Credits(balance=0)):
            refused = await client.post(URL, headers=headers)
        assert refused.status_code == 402''',
    ),
    "projection-from-platform-events": (
        "a read model of tenancy's members (v2 events) plus the billing plan",
        '''A projection is a table of your module fed by other modules' events: a
page is one indexed query, with no call per row, and it works when tenancy
runs as another service. Tenancy's member events `.v2` are made for it: the
organization is `tenant_id` and `version` is required (`uv run sandloom docs
tenancy.member_added.v2` prints the fields). Billing's
`billing.subscription.*.v1` carry no version, so they cannot feed a
projection (refused at definition): read the plan live from
`billing.entitlements.v1`, one call per page.

The table, keyed by the tenant first, and its migration:

    # file: <module>/models/member.py
    """Members of each organization: <module>'s read model of tenancy."""

    import uuid
    from datetime import datetime

    from sqlalchemy import DateTime, Integer, PrimaryKeyConstraint, String
    from sqlalchemy.orm import Mapped, mapped_column

    from sandloom.sdk import Base, TenantScoped


    class Member(TenantScoped, Base):
        """One row per (organization, user); a removal is a tombstone
        (``deleted_at``), so a late older event cannot bring it back."""

        __tablename__ = "<module>_members"
        __table_args__ = (PrimaryKeyConstraint("tenant_id", "user_id"),)

        user_id: Mapped[uuid.UUID] = mapped_column()
        role: Mapped[str | None] = mapped_column(String(32), nullable=True)
        version: Mapped[int | None] = mapped_column(Integer, nullable=True)
        deleted_at: Mapped[datetime | None] = mapped_column(
            DateTime(timezone=True), nullable=True
        )

    uv run sandloom migrate new <module> "members read model"

The projection: views of the three v2 channels (every field the producer
requires, plus the optional ones you read: a test delivers the view and it
is checked against the producer's schema), one source group ordered by
`version`, and `rebuild_from` (the current members,
read through tenancy's contract) so `sandloom projection rebuild <module>.members`
can refill it:

    # file: <module>/projections.py
    """<module>.members: tenancy's members of each organization."""

    from uuid import UUID

    from sandloom.sdk import Contract, Event
    from sandloom.sdk.projections import (  # sandloom: advanced projections
        Projection,
        SourceGroup,
        full_state,
        tombstone,
    )
    from sandloom.shared.contracts.tenancy_membership_v1 import TENANCY_MEMBERSHIP_V1

    from .models.member import Member

    membership = Contract(TENANCY_MEMBERSHIP_V1)
    PAGE = 500


    class MemberAdded(Event, channel="tenancy.member_added.v2"):
        tenant_id: UUID
        organization_id: UUID
        user_id: UUID
        role: str | None = None
        version: int


    class MemberRoleChanged(Event, channel="tenancy.member_role_changed.v2"):
        tenant_id: UUID
        organization_id: UUID
        user_id: UUID
        role: str | None = None
        version: int


    class MemberRemoved(Event, channel="tenancy.member_removed.v2"):
        tenant_id: UUID
        organization_id: UUID
        user_id: UUID
        version: int


    async def current_members(tenant_id: UUID) -> list[MemberAdded]:
        """``rebuild_from``: the organization's members now, as full states."""
        members: list[MemberAdded] = []
        after: UUID | None = None
        while True:
            page = await membership.list_member_user_ids(tenant_id, after, PAGE)
            for user_id in page:
                state = await membership.get_member_state(tenant_id, user_id)
                if state.role is not None:
                    members.append(
                        MemberAdded(
                            tenant_id=tenant_id,
                            organization_id=tenant_id,
                            user_id=user_id,
                            role=state.role,
                            version=state.version,
                        )
                    )
            if len(page) < PAGE:
                return members
            after = page[-1]


    MEMBERS = Projection(
        "<module>.members",
        key=("tenant_id", "user_id"),
        model=Member,
        groups=[
            SourceGroup(
                events={
                    MemberAdded: full_state(lambda e: {"role": e.role}),
                    MemberRoleChanged: full_state(lambda e: {"role": e.role}),
                    MemberRemoved: tombstone(lambda e: {}),
                },
                aggregate_key="user_id",
                version_field="version",
                version_column="version",
                owns=("role",),
                tenant_from="tenant_id",
            )
        ],
        rebuild_from=current_members,
    )

The page: one query on the projection, one billing call:

    # file: <module>/team.py
    """The organization's team: the projection and the live plan."""

    from typing import Any

    import sqlalchemy as sa

    from sandloom.sdk import Contract, db
    from sandloom.sdk.tenant import current_tenant_id  # sandloom: advanced the organization
    from sandloom.shared.contracts.billing_entitlements_v1 import BILLING_ENTITLEMENTS_V1

    from .models.member import Member

    entitlements = Contract(BILLING_ENTITLEMENTS_V1)


    async def team_page(*, limit: int = 50) -> dict[str, Any]:
        """The bound organization's members (user id order) and its plan."""
        query = (
            sa.select(Member.user_id, Member.role)
            .where(Member.deleted_at.is_(None))
            .order_by(Member.user_id)
            .limit(limit)
        )
        rows = (await db().execute(query)).all()
        claims = await entitlements.subscription_claims("organization", current_tenant_id())
        return {
            "members": {str(user_id): role for user_id, role in rows},
            "plan": claims.get("plan_code"),
            "status": claims.get("subscription_status", "none"),
        }

Tests deliver the events (in any order, twice) and fake both contracts:

    # file: <module>/tests/test_members.py
    """The members projection: ordering, tombstones, rebuild."""

    from typing import Any
    from uuid import UUID, uuid4

    from sandloom.sdk.tenant import bind_tenant  # sandloom: advanced the organization
    from sandloom.sdk.testing import deliver, fake_contract
    from sandloom.shared.contracts.billing_entitlements_v1 import BILLING_ENTITLEMENTS_V1
    from sandloom.shared.contracts.tenancy_dto import MemberState
    from sandloom.shared.contracts.tenancy_membership_v1 import TENANCY_MEMBERSHIP_V1

    from ..projections import MEMBERS, MemberAdded, MemberRemoved, MemberRoleChanged
    from ..team import team_page


    class Billing:
        async def subscription_claims(
            self, tenant_type: str, tenant_id: UUID
        ) -> dict[str, Any]:
            return {"subscription_status": "active", "plan_code": "pro"}


    class Tenancy:
        def __init__(self, roles: dict[UUID, str]) -> None:
            self.roles = roles

        async def list_member_user_ids(
            self, organization_id: UUID, after: UUID | None, limit: int
        ) -> list[UUID]:
            ids = sorted(u for u in self.roles if after is None or u > after)
            return ids[:limit]

        async def get_member_state(
            self, organization_id: UUID, user_id: UUID
        ) -> MemberState:
            role = self.roles.get(user_id)
            return MemberState(
                organization_id=organization_id, user_id=user_id, role=role, version=4
            )


    async def team(org: UUID) -> dict[str, Any]:
        with bind_tenant(org), fake_contract(BILLING_ENTITLEMENTS_V1, Billing()):
            return await team_page()


    async def test_events_in_any_order_give_the_current_team(db_uow: Any) -> None:
        org, ann, bob = uuid4(), uuid4(), uuid4()
        ids = {"tenant_id": org, "organization_id": org}
        for event in (
            MemberAdded(**ids, user_id=ann, role="member", version=1),
            MemberRoleChanged(**ids, user_id=ann, role="admin", version=3),
            MemberRoleChanged(**ids, user_id=ann, role="owner", version=2),  # late
            MemberAdded(**ids, user_id=bob, role="member", version=1),
            MemberRemoved(**ids, user_id=bob, version=2),
        ):
            await deliver(event, times=2, session=db_uow)
        page = await team(org)
        assert page == {"members": {str(ann): "admin"}, "plan": "pro", "status": "active"}
        assert (await team(uuid4()))["members"] == {}


    async def test_a_rebuild_refills_it_from_tenancy(db_uow: Any) -> None:
        org, ann = uuid4(), uuid4()
        rebuilder = MEMBERS.rebuilder()
        assert rebuilder is not None
        with fake_contract(TENANCY_MEMBERSHIP_V1, Tenancy({ann: "owner"})):
            result = await rebuilder.rebuild_tenant(org)
        assert result.rows == 1
        assert (await team(org))["members"] == {str(ann): "owner"}

A gate on the plan itself is a route atom, not a projection:
`require(SubscriptionActive(of=Account.org(param="org_id")))` (`sandloom docs
recipe org-gate`).''',
    ),
    "event-v2": (
        "roll your own event to v2: dual publish, move consumers, retire v1",
        '''An optional new field stays in `.v1` (`sandloom generate` accepts it). A removed,
renamed or retyped field, a new required field or a changed meaning is
breaking: on the same `.vN` `sandloom generate` refuses it (SANDLOOM-T005). Roll to `.v2`:

1. Declare v2 next to v1 and publish both in one unit
   (`publish_event_versions`: every version is checked against its schema,
   and they commit together or not at all). Never edit v1 in place.
2. Move each consumer to a view of v2, one service at a time. The two
   versions have different envelope ids: a consumer of both sees the event
   twice, so keep one handler per module.
3. `uv run sandloom streams consumers`: when no group reads v1 in any
   environment, retire v1: publish `await ItemCreatedV2(...).publish()`
   alone. Keep the v1 class while old events may be replayed (the stream
   retention, 7 days by default).

`<module>` produces `<module>.item.created.v1` (the scaffold's event);
`<consumer>` is another module (`uv run sandloom add module <consumer>`):

    # file: <module>/events.py
    """Events of <module> (its own channels)."""

    from uuid import UUID

    from sandloom.sdk import Event
    from sandloom.sdk.events import publish_event_versions  # sandloom: advanced dual-publish


    class ItemCreated(Event, channel="<module>.item.created.v1"):
        """Unchanged while v1 readers remain."""

        item_id: UUID
        tenant_id: UUID


    class ItemCreatedV2(Event, channel="<module>.item.created.v2"):
        """v2: the owner and the title are required (breaking for v1)."""

        item_id: UUID
        tenant_id: UUID
        owner_id: UUID
        title: str


    async def publish_item_created(
        item_id: UUID, tenant_id: UUID, owner_id: UUID, title: str
    ) -> None:
        """Both versions in the running unit: all commit or none do."""
        v1 = ItemCreated(item_id=item_id, tenant_id=tenant_id)
        v2 = ItemCreatedV2(
            item_id=item_id, tenant_id=tenant_id, owner_id=owner_id, title=title
        )
        await publish_event_versions(
            {
                ItemCreated.channel: v1.model_dump(mode="json"),
                ItemCreatedV2.channel: v2.model_dump(mode="json"),
            }
        )

In `<module>/services.py`, where v1 was published:

    await publish_item_created(item.id, item.tenant_id, item.owner_id, item.title)

    # file: <module>/tests/test_event_v2.py
    """Both versions are published, in one unit."""

    from uuid import uuid4

    from sandloom.sdk.testing import recording_uow  # sandloom: advanced records the outbox

    from ..events import ItemCreated, ItemCreatedV2, publish_item_created


    async def test_both_versions_are_published_together() -> None:
        item, tenant, owner = uuid4(), uuid4(), uuid4()
        with recording_uow() as unit:
            await publish_item_created(item, tenant, owner, "first")
        channels = [channel for channel, _payload, _version in unit.events]
        assert channels == [ItemCreated.channel, ItemCreatedV2.channel]
        assert unit.events[1][1]["title"] == "first"

The consumer, moved to v2. Its view declares every field the producer
requires (a test delivers the view, checked against the producer's schema)
and the optional ones it reads:

    # file: <consumer>/consumers.py
    """<consumer> copies <module>'s new items (v2)."""

    from uuid import UUID

    from sandloom.sdk import Event, db, on

    from .models.item import <Consumer>Item


    class ItemCreated(Event, channel="<module>.item.created.v2"):
        item_id: UUID
        tenant_id: UUID
        owner_id: UUID
        title: str


    @on(ItemCreated)
    async def copy_item(event: ItemCreated) -> None:
        """Durable: a redelivery (the same envelope id) is applied once."""
        db().add(<Consumer>Item(title=event.title, owner_id=event.owner_id))

    # file: <consumer>/tests/test_consumers.py
    """The v2 consumer, driven through the real consumer runtime."""

    from typing import Any
    from uuid import uuid4

    import sqlalchemy as sa

    from sandloom.sdk import db
    from sandloom.sdk.tenant import bind_tenant  # sandloom: advanced the event's tenant
    from sandloom.sdk.testing import deliver

    from ..consumers import ItemCreated
    from ..models.item import <Consumer>Item


    async def test_a_v2_event_is_copied_once(db_uow: Any) -> None:
        tenant = uuid4()
        event = ItemCreated(
            item_id=uuid4(), tenant_id=tenant, owner_id=uuid4(), title="first"
        )
        assert await deliver(event, times=2, session=db_uow) == 1
        with bind_tenant(tenant):
            titles = (await db().scalars(sa.select(<Consumer>Item.title))).all()
        assert list(titles) == ["first"]

A field named like the Event API (`channel`, `meta`, `publish`,
`wire_schema`) is refused at definition (SANDLOOM-B015); keep such a wire key
with an alias: `via: str = Field(alias="channel")`.''',
    ),
    "periodic-task": (
        "a nightly task: sandloom add task --periodic, an idempotent body, the beat",
        '''`sandloom add task --periodic` writes the task and its beat schedule
(`setup_periodic_tasks`, which the framework worker connects). The body is
at-least-once (`acks_late`: a task whose worker dies runs again), and the
beat may fire while a previous run still works: make every run harmless to
repeat (delete or update by a condition, upsert, or claim a
`durable_inbox` key). Work in short units, one `open_uow()` per batch.

    uv run sandloom add task <module> purge_old_items --periodic "0 3 * * *"

    # file: <module>/tasks.py
    """Background tasks of <module> (the framework worker imports this file)."""

    from datetime import UTC, datetime, timedelta
    from typing import Any

    import sqlalchemy as sa

    from sandloom.sdk import db
    from sandloom.sdk.data import open_uow  # sandloom: advanced task unit
    from sandloom.sdk.effects import run_async  # sandloom: advanced task body
    from sandloom.sdk.tasks import cron, shared_task  # sandloom: advanced tasks
    from sandloom.sdk.tenant import unscoped  # sandloom: advanced every tenant

    from .models.item import <Module>Item

    KEEP = timedelta(days=30)
    BATCH = 1000


    async def purge_batch(cutoff: datetime) -> int:
        """Delete up to BATCH items older than ``cutoff``, in every tenant. By
        a condition: a repeated or overlapping run deletes nothing twice."""
        with unscoped():
            old = sa.select(<Module>Item.id).where(<Module>Item.created_at < cutoff)
            ids = list((await db().scalars(old.limit(BATCH))).all())
            if ids:
                await db().execute(sa.delete(<Module>Item).where(<Module>Item.id.in_(ids)))
        return len(ids)


    async def _purge_old_items() -> int:
        cutoff = datetime.now(UTC) - KEEP
        total = 0
        while True:
            async with open_uow():  # one short unit per batch
                deleted = await purge_batch(cutoff)
            total += deleted
            if deleted < BATCH:
                return total


    @shared_task(
        name="<app>.modules.<module>.tasks.purge_old_items",
        acks_late=True,
        reject_on_worker_lost=True,
    )
    def purge_old_items() -> int:
        """At-least-once (acks_late): Celery redelivers it if a worker dies."""
        return run_async(_purge_old_items())


    def setup_periodic_tasks(sender: Any, **kwargs: Any) -> None:
        """Beat hook: exactly one beat per deploy profile runs it (SANDLOOM-T010)."""
        sender.add_periodic_task(
            cron("0 3 * * *"), purge_old_items.s(), name="<module>.purge_old_items"
        )

    # file: <module>/tests/test_tasks.py
    """The nightly purge: idempotent, committed per batch, scheduled."""

    from datetime import UTC, datetime, timedelta
    from typing import Any
    from unittest.mock import MagicMock
    from uuid import uuid4

    import sqlalchemy as sa

    from sandloom.sdk import db
    from sandloom.sdk.data import open_uow  # sandloom: advanced the test's own unit
    from sandloom.sdk.effects import run_async  # sandloom: advanced no running loop
    from sandloom.sdk.tenant import bind_tenant, unscoped  # sandloom: advanced tenants

    from ..models.item import <Module>Item
    from ..tasks import purge_batch, purge_old_items, setup_periodic_tasks

    NOW = datetime.now(UTC)
    OLD = NOW - timedelta(days=40)


    async def add_item(created_at: datetime) -> None:
        with bind_tenant(uuid4()):
            db().add(<Module>Item(title="x", owner_id=uuid4(), created_at=created_at))
            await db().flush()


    async def test_a_batch_purges_old_items_of_every_tenant(db_uow: Any) -> None:
        for created_at in (OLD, OLD, NOW):
            await add_item(created_at)
        cutoff = NOW - timedelta(days=30)
        assert await purge_batch(cutoff) == 2
        assert await purge_batch(cutoff) == 0  # a repeated run is harmless


    def test_the_task_commits_its_batches(db_engine: Any) -> None:
        async def given() -> None:
            async with open_uow():
                await add_item(OLD)
                await add_item(NOW)

        async def left() -> int:
            async with open_uow():
                with unscoped():
                    return len((await db().scalars(sa.select(<Module>Item.id))).all())

        run_async(given())
        assert purge_old_items() == 1
        assert purge_old_items() == 0
        assert run_async(left()) == 1


    def test_the_beat_runs_it_nightly() -> None:
        sender = MagicMock()
        setup_periodic_tasks(sender)
        (call,) = sender.add_periodic_task.call_args_list
        assert call.kwargs["name"] == "<module>.purge_old_items"

The beat: the monolith profile (`deploy/docker-compose.yml`) runs one beat
for every module; `sandloom test` checks each profile has exactly one (SANDLOOM-T010).
Split into services, `uv run sandloom add deploy <module> --service <svc>` gives
the service its worker and, for periodic tasks, its one beat. Schedules use
Celery's timezone (UTC unless configured).''',
    ),
}


def _recipe_index() -> str:
    width = max(len(name) for name in _RECIPES)
    lines = ["sandloom docs recipe <name>:"]
    lines += [f"  {name:<{width}}  {title}" for name, (title, _) in _RECIPES.items()]
    return "\n".join(lines)


def _recipe(name: str) -> str | None:
    found = _RECIPES.get(name)
    if found is None:
        return None
    title, text = found
    return f"# {name}: {title}\n\n{text}"


_FILE = re.compile(r"^    # file: (\S+)\s*$")
_COMMAND = re.compile(r"^    (uv run sandloom (?:add|migrate new) .+?)\s*$")


def recipe_parts(name: str) -> list[tuple[str, str, str]]:
    """The steps of recipe ``name`` in text order, placeholders unexpanded:
    ``("command", "uv run sandloom add|migrate new ...", "")`` and ``("file",
    <path relative to the app's modules package>, <whole content>)`` for each
    ``# file: <path>`` block. What the recipes check runs in a generated
    application."""
    _title, text = _RECIPES[name]
    steps: list[tuple[str, str, list[str]]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        marker = _FILE.match(line)
        command = _COMMAND.match(line)
        if marker or command or (line.strip() and not line.startswith("    ")):
            current = None  # a block ends at prose, a command or the next file
        if marker:
            current = []
            steps.append(("file", marker.group(1), current))
        elif command:
            steps.append(("command", command.group(1), []))
        elif current is not None:
            current.append(line[4:])
    return [
        (kind, what, "\n".join(body).strip("\n") + "\n" if kind == "file" else "")
        for kind, what, body in steps
    ]


__all__: list[str] = []
