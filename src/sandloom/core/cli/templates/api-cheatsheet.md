## Public API cheat-sheet (`sandloom.sdk`; any other signature: `uv run sandloom docs <name>`)

Two tiers need no marker. Tier 1, 29 names, free in any file: the 24 of `sandloom.sdk` and the 5 test helpers (`client`, `as_user`, `published`, `deliver`, `fake_contract`). Tier 2, 12 names, free in the module file made for them: `sandloom.sdk.sagas` (`Saga`, `SagaContext`, `step`, `local_step`, `start_saga`) in `sagas.py`, `sandloom.sdk.projections` (`Projection`, `SourceGroup`, `full_state`, `tombstone`) in `projections.py`, `sandloom.sdk.tasks` (`task`, `periodic`) with `open_uow` of `sandloom.sdk.data` in `tasks.py`. Any other import, or a tier 2 name in another file, carries `# sandloom: advanced <reason>`.

```python
# from sandloom.sdk import ...
register = define_module(__name__, *, version="0.1.0", consumes=(), capabilities=(), depends_on=(), setup: Callable[[ModuleAPI], None] | None = None, ...)  # __init__.py
class Note(TenantScoped, Base): ...        # tenant_id column + automatic filter; Base alone: global
    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id); created_at: Mapped[CreatedAt]; updated_at: Mapped[UpdatedAt]  # id set at construction, both times loaded by flush()
    due_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)  # any other time (C029)
db() -> AsyncSession                       # the running unit's session: add/flush, never commit
raise NotFound("note not found")           # also Conflict("code taken"), Refused(422, "reason"; status 4xx): service, contract, saga step
with bind_tenant(tenant_id): ...           # serve one tenant: a task, a consumer, a script, a test
router = module_router(*, prefix=None, tags=None, tenant="derive", dependencies=())  # api.py
public(*, reason: str, issues_tokens=False) -> Depends   # dependencies=[public(reason="...")]
user: CurrentUser                          # the caller (UserContext: .user_id, roles, claims)
class NoteCreated(Event, channel="notes.note.created.v1"): note_id: UUID; tenant_id: UUID
await NoteCreated(...).publish(*, database=None)   # outbox of the running unit
@on(E, *, name=None, durable=True, required=True, dead_letter=True, start_from="earliest", group=None, database="default", tenant="derive")  # async def f(event: E) -> None
@contract("<m>.<name>.v1", *, database=None)                  # stateless class, async methods
@command(*, idempotency_key: str, idempotency_scope=(), database=None, risk: "money" | None = None)
quota = Contract(STATS_QUOTA_V1)           # REF from the generated <app>.shared.contracts.<name>
Contract(REF, optional=True).client() -> P | None          # None only when not deployed
require(policy, *, status_code=None)       # dependencies=[Depends(require(OrgRole("admin")))]
Account.user() | Account.org(param="org_id") | Account.bound_tenant()   # the subject a policy atom reads (`of=`)
# Generated, not sdk names (`sandloom generate`): from <app>.shared.contracts.billing_entitlements_v1 import
#   Subscribed(statuses=None, *, of), SubscriptionActive(*, of), Credits(minimum, *, of), Feature, WithinLimit;
#   from <app>.shared.contracts.tenancy_membership_v1 import OrgRole(minimum, *, org_param="org_id")
#   of= is required: pass an Account form
record_audit(action, *, actor_id=None, actor_type="user", resource_type=None, resource_id=None, data=None, required=None, database=None)   # sync, in a unit
await on_commit(callback, *, database=None)                 # runs only after COMMIT
# tier 2, in the file `sandloom add saga` / `projection` / `task` writes: from sandloom.sdk.sagas / .projections / .tasks import ...
ENROLL = Saga(name, *, version, input: type[BaseModel], deadline: timedelta, steps=[...])
step(name, *, call=contract.method, args=fn(ctx) -> dict, compensate=None, kind=None, refuse_on=(), deadline=None)
local_step(name, *, apply, compensate=None, kind=None, deadline=None)   # apply: async def f(ctx: SagaContext[In]), reads db()
start_saga(ENROLL, *, saga_id, input) -> SagaHandle      # in the request's unit
Projection(name, *, key, groups=[SourceGroup(events={E: full_state(fn) | tombstone(fn)}, aggregate_key, version_field, version_column, owns, tenant_from="tenant_id")], model=Row, ...)
@task(*, queue="default", at_least_once=False)   # async def f(payload: Model): `async with open_uow():` (sandloom.sdk.data), then db(); `await f.enqueue(payload, delay=None, idempotency_key=None)`
@periodic(*, cron: str, id: str, queue="default", at_least_once=False)   # a task without a payload, on a schedule (UTC)
# from sandloom.sdk.testing import ...   (`client` is a fixture of -p sandloom.sdk.testing.plugin)
as_user(user_id=None, tenant_id=None, *, email="", email_verified=True, tenant=True, **claims) -> tuple[UUID, dict[str, str]]   # (user_id, auth headers)
await deliver(event: str | Event, payload=None, *, times=1, event_id=None, session=None, core=None) -> int   # drives the real consumer runtime
published(E) -> list[E];   with fake_contract(REF, impl): ...
```

Advanced names, by module (`uv run sandloom docs sandloom.sdk.<module>` lists them):

| module | holds |
|---|---|
| `sandloom.sdk.web` | `TenantId`, `TenantScope`, `DbSession`, `Database`, `idempotency_guard`, `no_idempotency`, `replay_safe`, `sensitive`, `at_commit`, `operation_intent`, `writes`, `CurrentService`, `rate_limiter` |
| `sandloom.sdk.data` / `.tenant` | `open_uow`, `Versioned` (a `version` the database counts up), `MoneyLedger`, `enable_tenant_rls`, the read planner, the portable SQL forms (`CaseInsensitiveText`, `partial_index`, `search_index`, `conflict_insert`, `transaction_lock`; C028), `tenant_upsert` (the upsert of a `TenantScoped` model: `conflict_insert` is refused there outside `unscoped()`), `WallClock` for `func.now()` (only portable `func.<name>`, raw SQL marked: C031), the feature forms (`Array`, `fulltext_index`, `matches`, `json_path`, `json_contains`, `transaction_lock`, `aggregate_list`: declare the feature, C030) / `unscoped`, `current_tenant_id`, `tenant_kind` |
| `sandloom.sdk.events` / `.effects` | `EventMeta`, `publish_event_versions` (dual-publish) / `before_commit`, `durable_inbox`, `operation_key`, `run_async`, `sensitive_effect` |
| `sandloom.sdk.contracts` / `.authz` | `ContractRef`, `service`, `contract_method`, the provider runtime / `Owner` and `authorize_resource` (a resource policy; the loader is `async (id)`), `Role`, `Perm`, `Not`, `authorize`, access facts |
| `sandloom.sdk.sagas` / `.projections` | `SagaStep`, `saga_status`, `SagaHandle` / `View`, `ProjectionView` (the read side: `rows: Annotated[ProjectionView[Row], View(name, Row, *, on_stale="serve", wait_ms=None)]`), `monotonic_upsert` (L2 handlers), `QueryProjectionDescriptor`, `ProjectionConsumerDescriptor` |
| `sandloom.sdk.module` / `.app` / `.migrations` | `ModuleAPI`, `ContractRequirement` / `create_app` / `run_module_migrations` |
| `sandloom.sdk.infra` / `.security` / `.errors` | `cache`, `notifier`, `settings` / tokens, revocation, keys / every exception |
| `sandloom.sdk.testing` | `capture_effects`, `assert_contract_conformance`, `boot_module_alone`, `fake_access_fact`, harness helpers |
