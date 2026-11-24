---
paths: ["src/sandloom_*/**", "src/*/{api,schemas,services,events,consumers,contracts,sagas,projections,tasks}.py", "src/*/{models,tests,migrations}/**", "examples/*/*/modules/**"]
---
## Public API cheat-sheet (`sandloom.sdk`; any other signature: `uv run sandloom docs <name>`)

The 25 beginner names of `sandloom.sdk`, the 5 of `sandloom.sdk.sagas` and the 6 of `sandloom.sdk.projections` (42 with the test helpers and the `client` fixture), exact signatures; any other name's import carries `# sandloom: advanced <reason>`.

```python
# from sandloom.sdk import ...
register = define_module(__name__, *, version="0.1.0", consumes=(), capabilities=(), depends_on=(), setup: Callable[[ModuleAPI], None] | None = None, ...)  # __init__.py
class Note(TenantScoped, Base): ...        # tenant_id column + automatic filter; Base alone: global
    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id); created_at: Mapped[CreatedAt]; updated_at: Mapped[UpdatedAt]  # id set at construction, both times loaded by flush()
    due_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)  # any other time (C029)
db() -> AsyncSession                       # the running unit's session: add/flush, never commit
raise NotFound("note not found")           # also Conflict("code taken"), Refused(422, "reason"; status 4xx): service, contract, saga step
with bind_tenant(tenant_id): ...           # serve one tenant: a task, a test, an organization route
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
Owner(get_owner: Callable[[Any], UUID | None])   # a resource policy, used with authorize_resource
# Generated, not sdk names (`sandloom generate`): from <app>.shared.contracts.billing_entitlements_v1 import
#   Subscribed(statuses=None, *, of), SubscriptionActive(*, of), Credits(minimum, *, of), Feature, WithinLimit;
#   from <app>.shared.contracts.tenancy_membership_v1 import OrgRole(minimum, *, org_param="org_id")
#   of= is required: Account.user() | Account.org(param="org_id") | Account.bound_tenant()
authorize_resource(*, loader, by: str, policy, cast=None, not_found_detail="Not found", status_code=None)   # loader: async (id) -> entity | None, reads db()
record_audit(action, *, actor_id=None, actor_type="user", resource_type=None, resource_id=None, data=None, required=None, database=None)   # sync, in a unit
await on_commit(callback, *, database=None)                 # runs only after COMMIT
# from sandloom.sdk.sagas / .projections import ...   (`sandloom add saga` / `sandloom add projection`)
ENROLL = Saga(name, *, version, input: type[BaseModel], deadline: timedelta, steps=[...])
step(name, *, call=contract.method, args=fn(ctx) -> dict, compensate=None, kind=None, refuse_on=(), deadline=None)
local_step(name, *, apply, compensate=None, kind=None, deadline=None)   # apply: async def f(ctx: SagaContext[In]), reads db()
start_saga(ENROLL, *, saga_id, input) -> SagaHandle      # in the request's unit
Projection(name, *, key, groups=[SourceGroup(events={E: full_state(fn) | tombstone(fn)}, aggregate_key, version_field, version_column, owns, tenant_from="tenant_id")], model=Row, ...)
rows: Annotated[ProjectionView[Row], View(name, Row, *, on_stale="serve", wait_ms=None)]   # the read side
# from sandloom.sdk.testing import ...   (`client` is a fixture of -p sandloom.sdk.testing.plugin)
as_user(user_id=None, tenant_id=None, *, email="", email_verified=True, tenant=True, **claims) -> tuple[UUID, dict[str, str]]   # (user_id, auth headers)
await deliver(event: str | Event, payload=None, *, times=1, event_id=None, session=None, core=None) -> int   # drives the real consumer runtime
published(E) -> list[E];   with fake_contract(REF, impl): ...;   with fake_access_fact(name, value=True, *, owner, deny_category=None, core=None): ...
```

Advanced names, by module (`uv run sandloom docs sandloom.sdk.<module>` lists them):

| module | holds |
|---|---|
| `sandloom.sdk.web` | `TenantId`, `TenantScope`, `DbSession`, `Database`, `idempotency_guard`, `no_idempotency`, `replay_safe`, `sensitive`, `at_commit`, `operation_intent`, `writes`, `CurrentService`, `rate_limiter` |
| `sandloom.sdk.data` / `.tenant` | `open_uow`, `Versioned` (a `version` the database counts up), `MoneyLedger`, `enable_tenant_rls`, the read planner, the portable SQL forms (`CaseInsensitiveText`, `partial_index`, `search_index`, `conflict_insert`, `transaction_lock`; C028), `tenant_upsert` (the upsert of a `TenantScoped` model: `conflict_insert` is refused there outside `unscoped()`), `WallClock` for `func.now()` (only portable `func.<name>`, raw SQL marked: C031), the feature forms (`Array`, `fulltext_index`, `matches`, `json_path`, `json_contains`, `transaction_lock`, `aggregate_list`: declare the feature, C030) / `unscoped`, `current_tenant_id`, `tenant_kind` |
| `sandloom.sdk.events` / `.effects` | `EventMeta`, `publish_event_versions` (dual-publish) / `before_commit`, `durable_inbox`, `operation_key`, `run_async`, `sensitive_effect` |
| `sandloom.sdk.contracts` / `.authz` | `ContractRef`, `service`, `contract_method`, the provider runtime / `Account`, `Role`, `Perm`, `Not`, `authorize`, access facts |
| `sandloom.sdk.sagas` / `.projections` | `SagaStep`, `saga_status`, `SagaHandle` / `monotonic_upsert` (L2 handlers), `QueryProjectionDescriptor`, `ProjectionConsumerDescriptor` |
| `sandloom.sdk.module` / `.app` / `.migrations` | `ModuleAPI`, `ContractRequirement` / `create_app` / `run_module_migrations` |
| `sandloom.sdk.infra` / `.security` / `.errors` | `cache`, `notifier`, `settings` / tokens, revocation, keys / every exception |
| `sandloom.sdk.testing` | `capture_effects`, `assert_contract_conformance`, `boot_module_alone`, harness helpers |
