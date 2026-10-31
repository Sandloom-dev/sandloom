## Public API cheat-sheet (`sandloom.sdk`; any other signature: `uv run sandloom docs <name>`)

The 29 beginner names, exact signatures; any other name's import carries `# sandloom: advanced <reason>`.

```python
# from sandloom.sdk import ...
register = define_module(__name__, *, version="0.1.0", consumes=(), capabilities=(),
              depends_on=(), setup: Callable[[ModuleAPI], None] | None = None, ...)  # __init__.py
class Note(TenantScoped, Base): ...        # tenant_id column + automatic filter; Base alone: global
    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id); created_at: Mapped[CreatedAt]
db() -> AsyncSession                       # the running unit's session: add/flush, never commit
router = module_router(*, prefix=None, tags=None, tenant="derive", dependencies=())  # api.py
public(*, reason: str, issues_tokens=False) -> Depends   # dependencies=[public(reason="...")]
user: CurrentUser                          # the caller (UserContext: .user_id, roles, claims)
class NoteCreated(Event, channel="notes.note.created.v1"): note_id: UUID; tenant_id: UUID
await NoteCreated(...).publish(*, database=None)   # outbox of the running unit
@on(E, *, name=None, durable=True, required=True, dead_letter=True, start_from="earliest",
    group=None, database="default", tenant="derive")          # async def f(event: E) -> None
@contract("<m>.<name>.v1", *, database=None)                  # stateless class, async methods
@command(*, idempotency_key: str, idempotency_scope=(), database=None, risk: "money" | None = None)
quota = Contract(STATS_QUOTA_V1)           # REF from the generated sandloom.shared.contracts.<name>
Contract(REF, optional=True).client() -> P | None          # None only when not deployed
require(policy, *, status_code=None)       # dependencies=[Depends(require(OrgRole("admin")))]
Subscribed(statuses=None, *, of: Account, claim_key="subscription_status")   # claims, zero I/O
SubscriptionActive(*, of: Account, adapter=None);  Credits(minimum, *, of: Account, adapter=None)
#   of= is required: Account.user() | Account.org(param="org_id") | Account.bound_tenant()
OrgRole(minimum, *, org_param="org_id", adapter=None);  Owner(get_owner: Callable[[Any], UUID | None])
authorize_resource(*, loader, by: str, policy, cast=None, not_found_detail="Not found",
              status_code=None)            # loader: async (id, db) -> entity | None
record_audit(action, *, actor_id=None, actor_type="user", resource_type=None,
              resource_id=None, data=None, required=None, database=None)   # sync, in a unit
await on_commit(callback, *, database=None)                 # runs only after COMMIT
# from sandloom.sdk.testing import ...   (`client` is a fixture of -p sandloom.sdk.testing.plugin)
as_user(user_id=None, tenant_id=None, *, email="", email_verified=True, tenant=True,
        **claims) -> tuple[UUID, dict[str, str]]           # (user_id, auth headers)
await deliver(event: str | Event, payload=None, *, times=1, event_id=None, session=None,
              core=None) -> int                   # drives the real consumer runtime
published(E) -> list[E];   with fake_contract(REF, impl): ...
with fake_access_fact(name, value=True, *, deny_category=None, core=None): ...
```

Advanced names, by module (`uv run sandloom docs sandloom.sdk.<module>` lists them):

| module | holds |
|---|---|
| `sandloom.sdk.web` | `TenantId`, `TenantScope`, `DbSession`, `Database`, `idempotency_guard`, `no_idempotency`, `replay_safe`, `sensitive`, `at_commit`, `operation_intent`, `writes`, `CurrentService`, `rate_limiter` |
| `sandloom.sdk.data` / `.tenant` | `open_uow`, `UpdatedAt`, `MoneyLedger`, `enable_tenant_rls`, the read planner / `bind_tenant`, `unscoped`, `current_tenant_id`, `tenant_kind` |
| `sandloom.sdk.events` / `.effects` | `EventMeta`, `publish_event_versions` (dual-publish) / `before_commit`, `durable_inbox`, `operation_key`, `run_async`, `sensitive_effect` |
| `sandloom.sdk.contracts` | `ContractRef`, `service`, `contract_method`, the provider runtime |
| `sandloom.sdk.authz` | `Account`, `Role`, `Perm`, `Feature`, `WithinLimit`, `Not`, `authorize`, access facts |
| `sandloom.sdk.sagas` / `.projections` | `Saga`, `step`, `start_saga` / `Projection`, `SourceGroup`, `full_state`, `View` |
| `sandloom.sdk.module` / `.app` / `.migrations` | `ModuleAPI`, `ContractRequirement` / `create_app` / `run_module_migrations` |
| `sandloom.sdk.infra` / `.drivers` / `.security` / `.errors` | `cache`, `notifier`, `settings` / what a driver package implements / tokens, revocation, keys / every exception |
| `sandloom.sdk.testing` | `capture_effects`, `assert_contract_conformance`, `boot_module_alone`, harness helpers |
