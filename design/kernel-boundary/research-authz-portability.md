# Research: portable typed authorization atoms at today's cost

tm task `kb-research-authz-portability` (parent `kb-assess`). Evidence and option space for
decision C; it decides nothing. Read at `38f33cb` (branch `arch/kernel-boundary`); the
authorization code is the code `perf-dx-baseline.md` measured (`git diff b8d8506 -- src/sandloom/shared/authz` is empty). Labels:
**[verified: read]** (code read at the cited line), **[verified: run]** (probe output in
`_evidence/research-authz/probe_authz.out.txt`), **[measured]** (perf-dx-baseline.md),
**[inferred]**, **Unverified**. Inputs read: `goals.md`, `requirements.md`,
`perf-dx-baseline.md`, `validation.md`, `guarantee-map.md`, `_evidence/perf-dx/` (incl. the DX log
and its 31-line `QuotaAvailable`), the tm reflections on `kb-alt-authz`, `kb-poc-authz`,
`kb-assess`. Not read (blinding rule): `target-architecture.md`, `codex-review.md`,
`reconciled.md`, the roadmap inputs.

## 0. In plain words

A typed atom such as `OrgRole("admin")` is three things glued into one Python class: a
**vocabulary** (its name, its typed parameters, what they mean), **metadata** the framework reads
before any request (cost tier, which contract it calls, which organization parameter it reads,
its risk, its denial category) and **a few lines of evaluation** (bind the request's subject and
organization, call one contract method, turn the answer into allow/deny/indeterminate). Today
the kernel writes all three for tenancy and billing. A consumer process can use them only
because the kernel ships them.

The hard part is not running the check remotely: that already works, one contract call per atom,
local or over RPC. The hard parts are:

1. **The class must exist in the consumer process without the provider's package**, and be typed,
   or the consumer loses autocompletion and errors before run. Somebody has to write that class:
   the consumer (then every consumer writes it, and it is the consumer's word on risk), or the
   provider (then it must travel to processes that cannot import the provider).
2. **The metadata is semantic, not decoration.** The tier decides the evaluation order, and the
   order decides some decisions: `~(U & F)` is indeterminate, but with `F` re-tiered cheaper it
   allows (probe P3, [verified: run]). A moved atom must keep its tier exactly, statically.
3. **Risk must come from someone the route's own module cannot overrule**, and architecture checks
   read it today by class name (`OrgRole`, `Credits`, ...), which a moved or third-party atom does
   not have.
4. **No extra call.** The only parameterized, provider-owned mechanism that already travels
   (`AccessFact` / `Entitled`) adds a different path: a string name, no parameters, a runtime
   `describe()` every 300 s, tier fixed to live (so it opens the request DB session), and in a split
   an extra hop through `<owner>.facts.v1` inside the provider.

**Recommendation (section 7):** *contract-anchored access checks*. The provider marks one of its
own boolean contract read methods as an access check and states which arguments come from the
request (subject, organization, billing account) and what risk a parameter value carries.
`sandloom generate` already writes the provider's typed stub into the consumer's shared package;
it would also write a small typed atom class there, with its metadata as a literal. The kernel
keeps one generic evaluator that binds the context arguments and calls the stub. The atom then
calls the same contract method with the same arguments as today, so calls, queries and order are
equal by construction; the metadata is generated from the provider's declaration, so the
consumer cannot lower it. Fix three inherited costs beside it, each separately measurable:
describe on the first request, the request DB session for live policies, the third call of the
bound-tenant shape.

## 1. How a domain atom works today

### 1.1 The `Requirement` contract

`shared/authz/algebra.py:41-135` [verified: read].

| member | meaning | who reads it, when |
|---|---|---|
| `tier` (class attr, `TIER_CLAIM`=1 / `TIER_RESOURCE`=2 / `TIER_LIVE`=3, `:36-38`, `:52`) | cost class; combinators sort by it | `And`/`Or.decide` at each request (`:165`, `:209`); `require()` at wiring (`deps.py:121-123`) |
| `category`, `code` (`:54-56`) | denial category (→ HTTP status) and machine code (default: class name, `:85`) | `_deny()` at the request |
| `evaluate` / `decide` (`:58-81`) | boolean primitive (raises `PolicyIndeterminate`) / structured decision | the request |
| `describe()` (`:91-93`) | label in 403 detail and deny logs | request, and wiring (consumes reason, `sdk/_decl/module.py:396`) |
| `mentions_resource()` (`:95-102`) | needs the loaded resource | `require()` refuses such a policy at wiring (`deps.py:150-154`, G-AUTHZ-06) |
| `org_params()` (`:104-111`) | path params (or `<bound tenant>`) it reads an organization from | `_tag_gate` → `__sandloom_org_params__` (`deps.py:189`) → `create_app` gate order SANDLOOM-B007 (`core/guard_order.py:232-254`) |
| `contracts()` (`:113-120`) | sync contracts it may call | `_tag_gate` → `__sandloom_policy__` (`deps.py:191`) → derived optional `consumes` (`sdk/_decl/module.py:376-409`) → startup preflight warning and `sandloom add deploy` bindings (DX log row 35) |
| `risks()` (`:122-126`) | framework risks, `{"admin"}` for an admin gate | `_tag_gate` wraps the gate in `sensitive_effect("admin")` (`deps.py:192-194`) → route policy `risk` → `routes.snapshot.json` (`core/route_snapshots.py:88-109`), weakening refused (SANDLOOM-T001) |

`And`/`Or`: `tier = max(parts)` (`:158`, `:202`); evaluation `sorted(parts, key=tier)`, a stable
sort, so equal tiers keep declaration order; `And` returns the first non-allow, `Or` the first
allow, else the first indeterminate, else a deny (`:163-169`, `:207-226`). `Not` keeps an
indeterminate (`:259-270`) and has `tier = inner.tier` and no `risks()` (`:252-254`).

**The order is semantic** ([verified: run], P3): with both operands live, `~(F & U)` evaluates `F`
only and allows, `~(U & F)` evaluates `U` only and is indeterminate (the "known order
dependence" G-AUTHZ-13 pins). Re-tier `F` to the claim tier and `~(U & F)` allows. So an atom that
changes tier when it moves can change a decision, not only a cost. Any portable form must carry
the tier as static, provider-stated metadata.

### 1.2 Wiring: `require()`

`shared/authz/deps.py:129-195` [verified: read; run P2].

- `_needs_db(policy) = policy.tier > TIER_CLAIM` (`:121-123`). A claims-only policy gets a guard
  without `get_db` (`:166-174`, G-AUTHZ-02); every other policy depends on
  `Depends(get_db, scope="function")` (`:176-183`). P2: `Role("admin") | Credits(10, …)`,
  `Entitled("subscription.active")`, `OrgRole("admin")` all take `db`.
- No live atom reads `ctx.db`: contract calls run in the provider's own unit
  (`live_contracts.py:17-19`, `atoms_live.py:19-21`); the only use in `shared/authz` is
  `_AccountClaimAtom` forwarding it into a scoped context (`atoms_claims.py:276-283`), read by a
  fact's projection reader if any. So the session is opened for nothing on every live policy:
  [measured] "request DB session opened: yes, no statement", ≈ +0.56 M instructions for
  `Role("admin") | Credits(10)` with an admin who short-circuits (1,746,413 vs 1,182,904 Ir/req).

### 1.3 Evaluation of the live atoms

`shared/authz/atoms_live.py` [verified: read].

- **Adapter.** `SubscriptionActive`/`Credits` (`_TenantScopedLiveAtom`, `:79-118`) and `OrgRole`
  (`:168-231`) do not name a contract in their code: an *adapter* from a process-wide table does
  (`live_contracts.py:107-114`; defaults `DefaultEntitlementsAdapter.service =
  "billing.entitlements.v1"`, `:74-87`, and `DefaultMembershipAdapter.service =
  "tenancy.membership.v1"`, `:90-104`). `register_live_adapter` replaces a slot, consumer-side
  (`:125-164`, G-AUTHZ-05). The slots are fixed kinds `entitlements`/`membership` (`:139-148`).
- **Metadata.** `tier = TIER_LIVE`; `category = PAYMENT_REQUIRED` for billing (`:84-85`);
  `contracts() = {adapter.service}` (`:107-108`, `:199-200`); `org_params()` from `of=`
  (`:104-105`) or `{org_param}` (`:193-194`); `OrgRole.risks() = {"admin"}` unless `member`
  (`:196-197`). The role vocabulary is hard-coded: `minimum not in {"member","admin","owner"} →
  ValueError` (`:187-188`).
- **Steps.** (1) resolve the account (`account.py:204-250`) or the organization
  (`_resolve.py:109-173`; `OrgRole` with `validate_against_token=False`, `atoms_live.py:208-215`);
  (2) `resolve_contract` → `core.get_service(name)`: the local provider, else the
  `SERVICE_BINDINGS` RPC proxy (`_resolve.py:28-47`); absent → `ContractUnavailable` →
  indeterminate **501** (`atoms_live.py:57-76`); (3) one contract call through the adapter; (4)
  `bool(...)` (`Credits`: `balance >= minimum`, `:159-162`).
- **Cost per call** [measured]: monolith 3 DB round trips (`SubscriptionActive`, `OrgRole`) or 4
  (`Credits`) in the provider's own unit; split 1 RPC + the same statements in the provider.

### 1.4 `Account.bound_tenant()` and the third call

`account.py:153-201`, `:231-250` [verified: read; run P4]. `resolve_account` for `bound_tenant`
asks `tenant_kind(bound)`: `canonical` in single-tenant mode, `personal` when it equals the user
id, else `_is_member` calls **the membership adapter's `has_role_at_least(org, user,
"member")`**. The per-request kind memo (`_KIND_MEMO`, `:44-45`, `:143-150`) holds only
`tenant_kind` results; the contract read memo dedupes only identical calls. P4, with a counting
fake core, reproduces the measured shape exactly:

```
call: has_role_at_least (<org>, <user>, 'admin')      # OrgRole
call: has_role_at_least (<org>, <user>, 'member')     # tenant_kind for Account.bound_tenant()
call: check_subscription_active ('organization', <org>)
total contract calls: 3
```

[measured] mono 3 calls / 9 DB round trips, split 3 RPC / 9 DB; 5,827,384 Ir/req mono; p50
8.4 ms mono, 9.6 ms split. The kernel also names tenancy here: tenant kind = "confirmed by the
membership adapter" (`account.py:153-168`).

### 1.5 Access facts: the only provider-owned atom that travels

`facts.py`, `atoms_fact.py`, `facts_contract.py` [verified: read].

- `AccessFact` (`facts.py:166-252`): `name` (string), `owner_module`, `claim_key`, a declarative
  `ClaimRule` (`:71-126`; or a zero-argument factory evaluated with the owner's settings at
  `describe()` time, `:195-198`, `:212-219`), `projection_reader`, `live_service` +
  `live_resolver`, `min_freshness`, `default_mode`, `deny_category`. The mode is configuration
  (`ACCESS` setting, `:255-277`), clamped to the floor.
- `Entitled(fact_name)` (`atoms_fact.py:32-125`): **tier fixed to `TIER_LIVE`** "for composition
  ordering (worst case)" (`:9-10`, `:46`), even when the fact resolves from a claim, so
  `require(Entitled(...))` opens the request DB session (P2) and sorts after cheaper siblings.
  `contracts() = {fact_owner_contract(name)}` (`:63-64`).
- Remote: `lookup_access_fact` (`facts.py:544-600`) asks every bound `<owner>.facts.v1` for
  `describe()`, caches it on the core for **300 s** (`:64`, `:590-593`), owner = first dotted
  segment except the kernel's `WELL_KNOWN_FACT_OWNERS = {"subscription.active": "billing",
  "organization.subscription.active": "billing"}` (`:528-541`). The remote live answer is
  `facts.v1.evaluate(name, subject, request)` (`:466-473`, `facts_contract.py:73-108`): **no
  parameters**. The owner runs it in its process (`facts_contract.py:84-97`).
- [inferred] `Feature`/`WithinLimit` with an organization account pass their parameters as a
  *resource* (`{"organization_id", "key", "used"}`, `atoms_claims.py:276-285`, `:320-321`,
  `:359-360`); `_remote_live_resolver` sends no resource, so a split owner evaluates them without
  `key`. No bundled module registers `organization.features` / `organization.limits`, so nothing
  shipped hits it today. Unverified by a run.

### 1.6 Remote metadata on the request path

- **Contract describe.** The RPC proxy caches the provider's describe document per
  `(base_url, contract)` in `_remote_contracts` (`core/rpc/proxy.py:163-164`). Before the first
  call of a request with a contract read scope (memo) or an operation intent it describes first
  (`:318-334`), and after any response whose contract-hash header differs (`:362-367`,
  `:656-679`). The boot readiness probe fetches the **same document** (`core/rpc/readiness.py:51-70`,
  "`GET /_sandloom/rpc/<contract>` = the describe document") but does not seed
  `_remote_contracts`. [measured] one extra `GET` per bound contract on the first request,
  although the provider log shows the boot `GET` of each.
- **Fact describe.** `Subscribed` with `of=Account.user()` reads the claim through the
  `subscription.active` fact (`atoms_claims.py:156-202`): in a process without billing,
  `GET billing.facts.v1` + `POST …/describe` on first use and every 300 s [measured].

### 1.7 Failure handling: absent vs down

- **Absent** (neither local nor bound): indeterminate 501, `Not`/`Or`-safe (G-AUTHZ-01).
- **Bound but down**: nothing in `shared/authz` maps `RemoteServiceUnavailable` (grep: only
  `ServiceNotFound` is caught, `_resolve.py:44`). P1 [verified: run]: `OrgRole("admin") |
  SubscriptionActive(of=Account.user())` with tenancy bound but failing **raises 503 out of the
  `Or`**: the allowing sibling never runs, no deny log, no `access_denied_*` metric. The same
  policy with tenancy *absent* allows (501 on one branch, allow on the other). Fail-closed, but not
  the algebra's rule "an indeterminate branch beside an allow stays allow" (H-4), and different from
  the 503 indeterminate `Entitled` returns when the *describe* fails (`atoms_fact.py:75-82`). The
  hand-written DX atom inherits the same (DX log, "surfaces as the proxy's 503").

### 1.8 Static checks keyed on class names

[verified: read]

| check | how it recognises an atom |
|---|---|
| untagged sensitive route (`api10_rules.py:168-178`, used `:198`, `:251`, `:283`) | regex `\bRole\(\s*['"]admin`, `\bOrgRole\(\s*['"](admin\|owner)`, `require_org_role(...)`; member gate `OrgRole("member")` |
| SANDLOOM-C027 account-route mismatch (`api10_rules.py:499-502`, `:550-580`) | `BILLING_ATOMS = {"Subscribed","SubscriptionActive","Credits","Feature","WithinLimit"}` and `Account.<x>(...)` calls |
| static gate order before `TenantScope()` (`module_rules.py:59-64`, `:245-280`, `:454`) | `ORG_ATOMS = {"OrgRole"}`, `ORG_IF_ORGANIZATION` = the billing names, `org_param=` / `of=` keywords |
| SANDLOOM-B007 at startup (`guard_order.py:232-254`) | metadata: `__sandloom_org_params__` from `org_params()` |
| SANDLOOM-B017 (`account.py:112-123`) | runtime, at construction (`of` defaults to a sentinel, so a type checker does not see a missing `of=`) |

The runtime checks already key on metadata; the static ones do not. A moved, renamed or
third-party atom is invisible to the three static rules.

### 1.9 What a consumer can write today

DX log rows 10-18, 29, 33; `_evidence/perf-dx/dx/files/reports_access.py.txt`: a
`Requirement` subclass in the **consumer**, over the provider's generated stub: `tier =
TIER_LIVE`, `category`, `__init__(units)`, `evaluate` calling `limits.available(user_id,
units)`, `contracts()`, `describe()`, 31 lines, four advanced-tier names and a marker. It works
locally and over RPC with zero kernel change and zero manual split wiring. The provider cannot
ship it (its package is not importable by consumers); its tier, category and risk are the
consumer's word; a provider outage is the proxy's 503.

## 2. What portability is missing

| need | today | gap |
|---|---|---|
| a typed class in a consumer process without the provider's package | kernel ships `OrgRole`/`Credits`/…; others hand-write (1.9) | no provider-authored, consumer-importable atom |
| typed parameters, errors before run | `minimum: str` + runtime `ValueError` (`atoms_live.py:187`); `of: Account = _MISSING` (runtime B017) | `Literal[...]` and a required keyword would let mypy reject `OrgRole("admn")` and a missing `of=` |
| parameters over the wire | facts: none (`facts.py:466-473`) | the fact path cannot carry `minimum`, `units`, `key` |
| party binding (`of=`, organization) | kernel helpers `resolve_account`, `resolve_org_id` (general protocol, keep) | a provider must say *which* argument of its method is the subject / organization / account |
| static tier | class attribute; `Entitled` fixed live | a portable atom must state its tier statically (1.1) |
| metadata for checks | runtime: `org_params`, `contracts`, `risks`; static: class names (1.8) | static rules need literal metadata, not names |
| metadata the route's module cannot lower | kernel-authored today; consumer-authored in 1.9 | must be generated from the provider's declaration and snapshot-checked |
| no describe on the request path | first request per contract; facts every 300 s (1.6) | seed from boot / carry statically |
| equal calls | 1 call per live atom; 3 for the bound shape | any new path must not add a hop (facts.v1 would, 4.2) |
| outage as a decision | exception (1.7) | indeterminate 503 would match the algebra; changes a decision (owner) |
| kernel names no module | adapter defaults, role vocabulary, `WELL_KNOWN_FACT_OWNERS`, tenant kind via membership, name lists in archcheck | all of them must go to the owners or become declarations |

## 3. Precedents

| system | what is distributed, and how a consumer uses a typed, parameterized check without the provider's code | static analysis of policy metadata | lesson |
|---|---|---|---|
| **OPA bundles / Rego** | Policy and data travel as signed bundles, separately from services; a manifest's `roots` scope which data/policy namespace a bundle owns, overlapping roots rejected [OPA bundles docs]. The service queries a string path with JSON input: untyped at the call site. | `# METADATA` annotations (scope, title, `schemas`, `entrypoint`, `custom`) readable by `opa inspect -a` and `rego.metadata.rule()`; inlined schemas drive type checking in `opa check`/`eval` [OPA policy language, annotations]. | Metadata attached to the policy at its source, machine-readable without evaluation, is the right shape for archcheck. Ownership by namespace root is how a bundle claims "its" policies (cf. one owner per fact, C-F3). A query by string path is what `Entitled` is: it loses typing. |
| **Cedar** | A schema declares entity types, attributes, actions and the principal/resource/context types per action; policies are validated against it **before use**; "validation soundness ensures the absence of most, but not all errors" at evaluation [Cedar validation]. Templates parameterize only `?principal`/`?resource` in the scope, never the `when` conditions [Cedar templates]. | The validator is the static analysis: unknown types, wrong attributes, type mismatches. | Errors-before-run comes from a schema the provider publishes and the consumer's policy is checked against. Cedar deliberately keeps parameters out of conditions: parameterized logic stays with the owner. |
| **Zanzibar / SpiceDB / OpenFGA** | Schema-first: namespaces/definitions state object types, relations, permissions; a client asks `Check(object#permission@subject)` by name, with no provider code [Zanzibar, secondary sources]. Parameterized conditions: SpiceDB **caveats** and OpenFGA **conditions**, typed parameters + a CEL expression; context may be bound in the tuple or supplied in the check; SpiceDB answers *conditional* with the missing fields when context is incomplete [SpiceDB caveats; OpenFGA conditions]. Typed clients are generated from the schema by third-party tools (authzed-codegen emits per-permission `Check` methods and typed caveat argument structs [authzed-codegen]). | Schema compilation validates relations and caveat parameter types; CEL is bounded and statically checkable [authzed blog via search]. | (a) A typed parameterized check usable without provider code = schema + generated client + one remote call: exactly the stub-plus-atom shape. (b) A third outcome for "cannot decide" exists there too (conditional ≈ indeterminate). (c) These run a policy engine (CEL); the owner's constraint "no new runtime engine" rules that part out here. |
| **ASP.NET Core** | A *requirement* is plain data (`IAuthorizationRequirement`, e.g. `MinimumAgeRequirement(int)`); *handlers* are registered separately in DI, several per requirement (OR) [ASP.NET policies]. `IAuthorizationPolicyProvider` builds parameterized policies on demand from an attribute: `[MinimumAge(21)]` → policy name `"MinimumAge21"` parsed back [custom policy providers]. Handlers run in any order; `InvokeHandlersAfterFailure` defaults to true (no short-circuit). | None for policy semantics; the attribute is the only static marker. | Requirement-as-data in a shared assembly + handler in the owner is the split this repo needs (atom class in the consumer, evaluation at the provider). Encoding parameters in a string name (the policy provider) is the anti-pattern `Entitled` already is. ASP.NET gives no order guarantee; Sandloom must keep its order. |
| **gRPC / Buf** | Generated clients carry the service's declarations; **custom method options** (extensions of `MethodOptions`) put per-method metadata, e.g. an authorization rule, into the descriptor; interceptors read it at runtime [protobuf options write-ups; o3co/grpc.authz]. protovalidate puts constraints in the schema as options so every language enforces "exactly the same constraints" [protovalidate]. | Descriptors are data: tools read options without running the service. | Per-method, source-declared metadata travelling inside the generated client is the closest precedent; Sandloom already does this with `@contract_method(risk="money")`, recorded in the describe document and snapshot (`core/module_api/_method.py:102-160`, `_schema.py:30-60`). |
| **Django / DRF** | Apps ship `BasePermission` classes; a consumer imports them; they compose with `&`, `\|`, `~` [DRF permissions]. | None. | Same algebra as here, shipped by import: works only when the consumer may import the provider app, which the one rule forbids. A reported composition surprise (DRF issue #6500) shows why the algebra's semantics need laws and tests, as G-AUTHZ-13 has. |

Common pattern: the **owner publishes a typed declaration** (schema, proto, requirement type);
**consumers get generated or shared types** and call by name; **evaluation stays with the owner**
(or with an engine); **metadata is data at the declaration**, read by tools without running it.

## 4. The option space

Common ground for all options: `Requirement`, `&`/`|`/`~`, three-valued decisions, `require`,
`resolve_account`, `resolve_org_id`, `Account` stay in the kernel (goals.md, "Authorization: the
mechanism stays in the kernel"). The criteria are the owner's: (C1) zero extra RPC/DB vs the
baseline; (C2) the same order and short-circuit; (C3) typing and autocompletion; (C4) errors before
run; (C5) archcheck-visible metadata that the route's module cannot lower; (C6) generation only
when a declaration changes; (C7) number of new mechanisms (definition: requirements.md §4.2);
(C8) migration of existing users, incl. beginner-tier imports.

### 4.1 (i) Consumer-owned declarative Requirement classes over existing contract clients

**(i-a) as today**, documented and taught (1.9). **(i-b)** the kernel adds one declarative base so
the 31 lines become about six:

```python
class QuotaAvailable(ContractCheck, contract=QUOTA_LIMITS_V1, category=Category.QUOTA_EXCEEDED):
    units: int = 1
    async def ask(self, quota: QuotaLimitsV1, ctx: AuthzContext) -> bool:
        return await quota.available(ctx.subject.user_id, self.units)
```

- C1: pass by construction (same stub, same call). C2: pass if the consumer copies today's tier;
  nothing enforces it. C3/C4: pass (consumer's own class).
- C5: **fails**: tier, category and risk are the consumer's word, the module whose route risk is
  being classified. An `OrgRole` copy without `risks()` silently drops the admin tag. Static rules
  still need names or a convention.
- C6: no generation. C7: +1 (a kernel base class; (i-a): 0). C8: every consumer of `OrgRole`,
  `Credits`, … writes its own copy: bundled examples and apps each gain files; beginner imports
  disappear. The vocabulary forks per consumer (two consumers may give `OrgRole` different
  meanings).
- Verdict: the minimal option for a module's *own* one-off check; not sufficient for provider
  vocabularies (C5, C8). It is what the owner said to try first; the PoC should run it as the
  control (section 8).

### 4.2 (ii) Generated typed wrappers over a parameterized `AccessFact` / `Entitled`

The provider registers `AccessFact(name="tenancy.org_role", params={"minimum": Literal[...]},
tier=..., risk=..., ...)`; `sandloom generate` emits `class OrgRole(Entitled)` whose typed
`__init__` builds `Entitled("tenancy.org_role", params={"minimum": "admin"})`; `facts.v1.evaluate`
gains `params` (a wire change: `facts.v2` or an optional argument, C-B1).

- C1: **at risk in a split.** Today `OrgRole` calls `tenancy.membership.v1.has_role_at_least`, a
  planned read (one statement in a read-only lease). Through the fact path the provider runs
  `FactsContract.evaluate` (a contract call in the owner's own read-only unit,
  `facts_contract.py:20-23`), which calls the resolver, which calls the membership contract
  locally: an extra unit (`BEGIN`/`ROLLBACK`) is likely [inferred; measure]. In the monolith a
  local fact's resolver calls the contract directly: equal. Plus the 300-s describe unless the
  descriptor is generated statically (then a settings-dependent `ClaimRule` cannot be static).
- C2: needs a static tier on the fact (today `Entitled` is always live: moving `Subscribed` onto
  it would re-tier it and change decisions, P3) and the mode stays configuration, so a deploy
  setting could change the path behind a fixed tier.
- C3/C4: pass for the wrapper's `__init__`; the parameter types must be expressible in the fact
  descriptor (a second type system beside contract wire types).
- C5: pass if the metadata is the provider's descriptor, generated and snapshot-checked.
- C6: pass (generate on declaration change). C7: +3 (parameter wire format, generated wrapper
  artifact, static fact metadata/tier) and the deploy wiring changes: consumers bind
  `<owner>.facts.v1` instead of the domain contract (bindings, `RPC_ALLOWED_CALLS`, existing
  split deployments).
- C8: import changes (generated module); `WELL_KNOWN_FACT_OWNERS` disappears with names prefixed
  by the owner.
- Verdict: reuses a mechanism, but routes every typed atom through a generic string-keyed second
  contract; it adds a hop, a parameter wire format and a configuration-chosen mode to atoms
  whose semantics today are fixed. Keep `Entitled` for what it is good at (one fact, mode by
  configuration), not as the carrier of typed vocabularies.

### 4.3 (iii) Provider-published declarative policy schema, compiled at generate time

**(iii-a) a schema the kernel interprets** (Cedar/caveat-like: "call method M with arguments from
{param, subject, account}; compare result `>=` / `in` / rank"). C1-C6 can pass, but it is a
policy language with an interpreter: **fails "no new runtime engine"** (owner, `kb-poc-authz`).

**(iii-b) the provider writes the atom class in a restricted form and `sandloom generate` copies
its source** into the consumer's shared package, as it copies DTOs. C1/C2 pass by construction
(same code). C3/C4 pass. C5 passes (generated, stale-checked). C6 passes. C7: +2 (a new artifact
kind with a source copier, an archcheck rule restricting the authorable form). Against it: the
DTO copier's principle is to copy the wire shape and **never methods or validators** ("a validator
is the provider's and runs where the provider binds an argument", `core/cli/dto_copies.py:9-17`);
this would ship provider-authored executable code into every consumer, and proving that a body
only binds arguments and calls its own stub is a static-analysis project of its own.

### 4.4 (iv) Contract-anchored access checks (found during this research)

The provider declares the atom **on the contract method that answers it**. The predicate is the
provider's own contract method returning `bool`; the declaration says which arguments the
request supplies; the generator emits a typed atom class next to the stub; the kernel evaluates
every such atom with one generic, domain-free evaluator.

Provider (tenancy, sketch; names illustrative):

```python
@contract.read(planned=True)
@access_check(
    "OrgRole",
    bind={"organization_id": From.org(param="org_param", validate_against_token=False),
          "user_id": From.subject_id()},
    category=Category.FORBIDDEN,
    risk={"admin": {"minimum": ["admin", "owner"]}},   # a value table, not code
    witnesses_membership=("organization_id", "user_id"),  # section 6.3, optional
)
async def has_role_at_least(self, organization_id: UUID, user_id: UUID,
                            minimum: Literal["member", "admin", "owner"]) -> bool: ...
```

Billing: `@access_check("SubscriptionActive", bind={("tenant_type", "tenant_id"):
From.account()}, category=PAYMENT_REQUIRED)` on `check_subscription_active`; `Credits` on a
boolean read `has_credits_at_least(tenant_type, tenant_id, minimum) -> bool` (an additive method;
today's atom compares `get_credit_balance` in the consumer, `atoms_live.py:159-162`). The DX
task's provider adds one decorator to `available(owner_id, units)` and its consumer writes zero
atom lines.

Generated into the stub module (`<shared>/contracts/tenancy_membership_v1.py`, where the
`TenancyMembershipV1` Protocol already is):

```python
class OrgRole(ContractCheck):
    """<the method's docstring>"""
    __sandloom_atom__ = {"contract": "tenancy.membership.v1", "method": "has_role_at_least",
                         "tier": 3, "category": "forbidden",
                         "binds": {"organization_id": ["org", "org_param", False], "user_id": ["subject"]},
                         "risk": {"admin": {"minimum": ["admin", "owner"]}}}
    def __init__(self, minimum: Literal["member", "admin", "owner"], *, org_param: str = "org_id") -> None: ...
```

For an account-bound check the generated `__init__` has `*, of: Account` with no default, so a
missing `of=` is a type error before run (today only the runtime B017).

Kernel: `ContractCheck(Requirement)` reads `__sandloom_atom__`: `tier`, `category`,
`contracts()`, `org_params()` (from the `org`/`account` binds, via `account_org_params`),
`risks()` (from the value table and the instance's parameters), `describe()`; `evaluate` binds
`subject` → `ctx.subject.user_id`, `org` → `resolve_org_id(...)`, `account` →
`resolve_account(...)`, then `resolve_contract` + one method call, absent → 501 as today. The
binding sources are the kernel concepts goals.md keeps (subject, tenant, billing account); the
kernel names no module, contract or role word.

- **C1: pass by construction.** Same method, same arguments, same unit on the provider side (the
  decorator changes no wire). Credits moves from `get_credit_balance` to a boolean method with the
  same query: DB count equal if implemented the same (measure).
- **C2: pass.** Tier and category are literals generated from the provider's declaration (live
  for every contract check, as today); `And`/`Or` sort exactly as now.
- **C3/C4: pass, better than today** (`Literal` parameters, required `of=`).
- **C5: pass.** The metadata is generated from the provider's source into a file the consumer must
  not edit (stale/hand-edit → SANDLOOM-T007 as for stubs); the provider's declaration is part of
  its describe document and `contracts.snapshot.json`, so lowering a risk is a snapshot change the
  generator can classify as breaking (SANDLOOM-T004), like a removed method. Static rules read the
  literal by resolving the imported name to the generated file (AST, no import), so a third-party
  atom is recognised by `binds`/`risk`, not by name. Route risk stays recorded in
  `routes.snapshot.json` (`risks()` → `sensitive_effect`), whose weakening is refused (T001).
- **C6: pass.** The atom is regenerated exactly when the stub is: when the provider's contract
  declaration changes (`generated.py:491-544`, `stub_problems` before writing, `:346-370`).
- **C7: +1 to +2.** One new kind of generated content (atom classes in the stub module) and one
  declaration field on contract read methods (`access_check`, like `risk=`). Removed: the live
  adapter table and `register_live_adapter` (a process-wide default and a registration call),
  `_TenantScopedLiveAtom`/`OrgRole`/`SubscriptionActive`/`Credits` in the kernel, the class-name
  lists in three archcheck rules. No new registry, wire format, contract kind, package or engine.
- **C8: the import changes; the expression does not.** `from sandloom.sdk import OrgRole` becomes
  an import from the generated stub module (or a generated per-app re-export, owner Q2). Four
  beginner names leave `sandloom.sdk` (`Credits`, `OrgRole`, `Subscribed`, `SubscriptionActive`;
  `sdk/authz.py:7-8`, `:142-151`). Where the bundled modules' stubs live is decision B's question
  (`sandloom/shared/contracts/` is a kernel package today, so generating tenancy's atoms there
  would keep the kernel shipping tenancy artifacts).
- Limits: the predicate must be one boolean method; a check needing two provider calls is two
  atoms or one new provider method. Claim-tier atoms (`Subscribed`) are not contract calls: see
  4.6.

### 4.5 (v) A light client package per provider

The provider ships a second distribution (`sandloom-tenancy-client`) with its stubs, DTOs and
hand-written atoms; consumers install it, not the implementation (gRPC/Stripe-style client
libraries; ASP.NET requirements in a shared assembly). C1-C4 pass (provider-authored, same code
as today). C5 passes for consumers; static checks must parse an installed package. C6: no
generation, but a manual release per declaration change, and the versions of the client and the
implementation must be kept in step by hand (I-12). C7: +1 package per provider and a packaging
convention. C8: imports change to the client package. It competes with decision B (where stubs
live) rather than with decision C; it fits a multi-repo world where the provider is a separate
product, and is heavier than (iv) in the monorepo app the golden path generates.

### 4.6 Claim-tier atoms (`Subscribed`; `Feature`/`WithinLimit` with `Account.user()`)

These read a token claim under the owner's provenance (`atoms_claims.py:156-202`); they make no
call. Portable form under every option: the owner's existing `AccessFact` declaration, generated
statically into a typed class with `tier = TIER_CLAIM` and the descriptor as a literal (claim key,
rule, floor, owner, deny category), so no runtime `describe()`; the live fallback when provenance
shows the owner failed at mint stays `<owner>.facts.v1.evaluate` (cold path). The blocker is the
**settings-dependent rule** (billing's `past_due` grace, `AccessFact(claim_rule=factory)`,
`facts.py:195-198`): it depends on the owner's runtime settings, which a generator cannot see.
Two ways out, both owner decisions (Q4): the owner interprets at mint and stamps a plain boolean
claim (staleness equal to the status claim's, ≤ token TTL), or the consumer refreshes the
descriptor off the request path (boot + timer) instead of on it. `Feature`/`WithinLimit` have no
bundled producer (guarantee-map row "claim atoms Feature, WithinLimit"): move them to billing as
declared facts or retire them (Q5). Generating the class only from a composed owner also removes
today's silent path where `Subscribed` with no owner anywhere reads `subscription_status` from any
writer (`atoms_claims.py:184-188`; C-F5).

### 4.7 Summary

| criterion | (i-a) today | (i-b) kernel base | (ii) param. facts | (iii-a) schema + interpreter | (iii-b) source copy | **(iv) contract-anchored** | (v) client package |
|---|---|---|---|---|---|---|---|
| C1 zero extra RPC/DB | pass | pass | **at risk** (split hop) | pass if compiled to the same call | pass | **pass by construction** | pass |
| C2 order, short-circuit | consumer's tier | consumer's tier | needs static tier; mode by config | pass | pass | **pass** (generated literal) | pass |
| C3 typing, autocompletion | pass | pass | pass (wrapper) | pass (generated) | pass | **pass** | pass |
| C4 errors before run | runtime B017 | same | partial | pass | pass | **pass, better** (`Literal`, required `of=`) | provider's choice |
| C5 metadata, not lowerable by the route's module | **fail** | **fail** | pass | pass | pass | **pass** (generated + snapshot) | pass |
| C6 generate only on declaration change | n/a | n/a | pass | pass | pass | **pass** (with the stub) | manual releases |
| C7 new mechanisms | 0 | +1 | +3 | **engine** (disqualified) | +2 | **+1..2, −2** | +1 per provider |
| C8 migration, beginner imports | n/a (consumers write) | same | import change, binding change | import change | import change | **import change only** | import change, new dependency |

## 5. Inherited costs: fix or do not worsen

Each is independent of the option chosen, measurable alone, and should land as its own change so
that the C-B6 comparison (atom moved, nothing else) stays clean.

| cost | evidence | fix | mechanism | changes a decision? |
|---|---|---|---|---|
| request DB session opened for every live policy, incl. a short-circuiting admin | 1.2, P2; ≈ +0.56 M Ir/req | replace `tier > TIER_CLAIM` with a per-atom `needs_request_session` (true for resource atoms and for `Entitled` facts with a projection reader; false for contract checks); `And`/`Or` take the union | none new (a `Requirement` hook like `contracts()`) | no; counts unchanged; Ir lower |
| describe `GET` on the first request per bound contract | 1.6; +1 RPC per contract per process | seed the proxy's `_remote_contracts` from the boot readiness probe's document (same `GET`) | none | no |
| `billing.facts.v1` describe on first use and every 300 s | 1.6 | static descriptor (4.6), or refresh off the request path | none / a timer | no (Q4 decides the rule's freshness) |
| bound-tenant shape: 3 calls, not 2 | 1.4, P4 | 6.3 | one optional declaration field | no; −1 call, −3 DB round trips |
| a bound-but-down provider is an exception, not an indeterminate | 1.7, P1 | map `RemoteServiceUnavailable` in the evaluator to `AccessDecision.error(503, "owner_unavailable")` | none | **yes**: `OrgRole \| SubscriptionActive` with tenancy down goes from 503 to allow when the subscription allows (the algebra's own rule, H-4) - owner Q3 |
| remote fact loses resource parameters | 1.5 [inferred] | carry them, or retire org `Feature`/`WithinLimit` | wire change if carried | n/a today (no producer) |

## 6. Details the recommendation depends on

### 6.1 Who may author the risk

The route's module (the consumer) never authors it under (iv). The provider authors it once, in a
reviewed, snapshotted declaration. A provider that lowers the risk of its own check changes its
`contracts.snapshot.json`; the generator should treat a lowered or removed `risk` entry like a
removed method (T004). A consumer can still bypass the atom by writing its own `Requirement` that
calls the contract directly; two existing defences apply (the route snapshot records the route's
risk, T001; the hand-rolled authority heuristic, `api10_rules.py:230-252`), and a third is
possible: archcheck flags a `Requirement` subclass in a module that calls a method declared as
an access check (the stub literal names it). Residual risk: a consumer comparing `get_role` itself.
This is the "independent route classification" half of the owner's rule; it is not complete under
any option (Q6).

### 6.2 Trust is a separate axis

(iv) makes `OrgRole` the tenancy contract's own check; it does not make whoever serves
`tenancy.membership.v1` a trusted authority. Today `register_service` checks no name
(guarantee-map row "live atoms"; `core/facade/_services.py:53-69`). Which provider the application
assigns as the organization-role authority, and how the kernel verifies the bound remote service,
belongs to the trust research (`research-trust.md`); (iv) gives it a hook: the access-check
declaration names its contract, so the composition can require that contract's provider to be the
assigned authority for atoms carrying `risk: admin`.

### 6.3 The third call of `Account.bound_tenant()`

Tenant kind is a kernel concept (goals.md); "confirmed by tenancy's membership contract" is not.
Options, none decided:

- **(a) Membership witness.** A check declared with `witnesses_membership=(org_arg, user_arg)`
  records in the request's kind memo, when it allows, that the user is a member of that
  organization. `tenant_kind` reads the memo first. For `OrgRole("admin") &
  SubscriptionActive(of=Account.bound_tenant())` (declaration order, both live): 2 calls. Written
  the other way round, still 3 (as today). Domain-free in the kernel; the provider states the
  implication.
- **(b) Same question twice.** Both `OrgRole` and the kind resolution ask `get_role(org, user)`,
  so the existing read memo dedupes them; needs a rank comparison in the consumer (a comparator in
  the evaluator: an engine creep) or in tenancy's generated atom (code).
- **(c) Kind from the grant.** The tenant authority states the kind in the signed grant
  (`tenant_ids` → `{id: kind}`), 0 calls; changes a claim's shape and G-AUTHZ-11's "confirmed by
  the membership contract" (freshness ≤ token TTL; `SubscriptionActive` would then answer for an
  organization the user just left until the token expires, while `OrgRole` in the same policy still
  denies live).

Either way `tenant_kind` must ask the application-assigned tenant authority's declared membership
method instead of the `membership` adapter slot; without one, kind is unknown and
`bound_tenant()` is indeterminate (C-B4).

## 7. Recommendation

**(iv) contract-anchored access checks**, with the five fixes of section 5 as separate changes,
and (i-a) kept and documented for a module's own one-off check.

Reasons:

1. It is the only option where equal calls, queries and order hold **by construction**: the
   generated atom calls the method today's atom calls, with the arguments today's atom passes, and
   carries today's tier as a literal. (ii) adds a generic hop; (i) leaves the tier to each consumer.
2. It reuses the mechanisms that already carry provider declarations to consumers that cannot
   import the provider: the contract, its describe document and snapshot, and the stub generator
   with its compare-before-write checks. The only new things are one declaration field on a
   contract method (with `risk=` as the precedent) and the atom classes inside the stub. It
   removes a process-wide adapter table and the kernel's tenancy and billing atoms.
3. It answers the metadata half of the owner's three problems with the same artifact: the
   literal in the generated file is what archcheck reads, what `require()` reads at wiring and what
   the route snapshot records; the route's module cannot change it, the provider changes it only
   through a snapshot diff.
4. It follows the precedents' common pattern (owner-published typed declaration, generated
   consumer type, evaluation at the owner, metadata as data) without a policy engine.
5. It improves errors before run (`Literal` parameters, a required `of=`) and turns the DX task's
   31 consumer lines into one provider decorator.

Trade-offs accepted:

- The import of `OrgRole` & co. changes and four beginner names leave `sandloom.sdk` (Q2). The
  policy expression is unchanged.
- An atom's predicate must be one boolean contract method; billing gains `has_credits_at_least`.
- Claim-tier atoms need the settings-dependent rule decided (Q4) before `Subscribed` can move
  without a runtime describe.
- Where the bundled modules' stubs and atoms live is decision B's; until B is decided, generating
  them into `sandloom/shared/contracts/` keeps the kernel *shipping* (not importing) module
  artifacts.
- A consumer can still hand-roll an authority check; the risk rule is complete only for checks
  written through declared atoms (6.1, Q6).

## 8. PoC plan (for `kb-poc-authz`)

Code in `design/kernel-boundary/poc/authz/`, results in `validation.md`. Baseline numbers are
perf-dx-baseline.md's; base and candidate are re-measured **in one session, alternating**, with the
same stand (`_evidence/perf-dx/authz_stand.py`, `cg_authz.sh`) and the same seed.

**Steps**

1. *Control (i-a/i-b)*: a fixture provider `quota` and consumer `reports` (the DX task) with a
   consumer-written atom; then the same with a ~20-line PoC `ContractCheck` base. Record lines,
   advanced names, archcheck visibility.
2. *(iv) minimal*: a PoC `access_check` field on `@contract.read`, the stub generator emitting
   `__sandloom_atom__` + a typed class, the generic `ContractCheck` evaluator, applied to
   `tenancy.membership.v1.has_role_at_least` (`OrgRole`) and
   `billing.entitlements.v1.check_subscription_active` (`SubscriptionActive`), outside
   `src/sandloom` where possible (the evaluator may live in the PoC package and import only
   `sandloom.sdk.authz` names).
3. *Decisive scenario*: `require(OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant()))`
   behind `TenantScope()`, in **two processes**: the consumer venv has neither `sandloom_tenancy`
   nor `sandloom_billing` importable (assert `python -I -c "import sandloom_tenancy"` fails), and
   imports the generated atoms; tenancy + billing in a provider process bound by
   `SERVICE_BINDINGS`. Same route in the monolith.
4. *(ii) comparison* on the same scenario, only if step 2's counts or DX are doubtful: an
   `Entitled` with a `params` argument over a patched `facts.v1.evaluate`, counting the provider's
   statements.
5. *Fixes*, each as a separate run on top of step 3: session flag; proxy seeding; witness (6.3 a);
   outage as indeterminate.

**Pass / fail** (fail on any one)

| # | criterion | pass | source of the number |
|---|---|---|---|
| P1 | decisions | equal to today for admin+active (200), member (403 `OrgRole`), admin+inactive (402), outsider (403, `SubscriptionActive` not evaluated), no bound tenant (indeterminate 400) - mono and split | perf-dx §2.1 rows |
| P2 | calls and queries, steady state | mono: 3 contract calls, 9 DB round trips; split: 3 RPC, 9 provider DB round trips, 0 consumer DB; outsider: 1 call / 3 DB. With the witness fix only: 2 / 6 and 2 RPC / 6 (reported as a separate row) | perf-dx §2.1 (`log_statement`, Redis `MONITOR`, provider access log) |
| P3 | order | the atoms evaluated and their order equal today's (counting wrapper); G-AUTHZ-13 law properties pass with generated atoms in the trees (I-07 missing proof) | `tests/laws/test_authz_algebra_laws.py` |
| P4 | metadata on the request path | 0 describe `GET`s on the first request after boot with the seeding fix (today 1 per bound contract); without the fix, not more than today | perf-dx §2.2 |
| P5 | instructions (cachegrind, mono, K = 200) | bound shape ≤ 5,827,384 Ir/req + 1 %, `OrgRole` ≤ 2,624,547 + 1 %, `SubscriptionActive` ≤ 4,140,788 + 1 % (live atoms' run spread ≤ 0.3 %, so 1 % is resolvable); claims atoms inside the control's ±4.3 % band | perf-dx §2.3 |
| P6 | instructions (rusage, split) | bound shape consumer ≤ 4,823 k + 3 %, provider ≤ 6,463 k + 3 % | perf-dx §2.3 rusage |
| P7 | latency | no p50 increase > 50 % and no p99 increase > 2× against the base measured in the same session (mono base p50/p95/p99 8,411/11,886/14,975 µs, split 9,626/15,176/19,750) | perf-dx §2.4 noise rule |
| P8 | typing, errors before run | mypy rejects `OrgRole("admn")` and `SubscriptionActive()` without `of=`; the IDE lists `minimum` values | mypy output in validation.md |
| P9 | archcheck | a third-party fixture atom with an organization bind is found by the gate-order and C027-equivalent rules and an admin-risk atom by the sensitive-route rule, without its class name in any list; hand-editing the generated literal fails `sandloom test` (T007); lowering the provider's `risk` fails generate (T004) | `sandloom test` output |
| P10 | fail-closed | provider down: 503, never allow, incl. under `Not`; provider absent: 501; with the outage fix, `OrgRole \| SubscriptionActive` with tenancy down allows only on an active subscription, with a deny-log line for the indeterminate branch | perf-dx DX log row 41; G-AUTHZ-01 |
| P11 | generation | `sandloom generate` writes nothing when only a provider's service body changes; rewrites stub + atom when the decorated method's declaration changes; byte-identical under two `PYTHONHASHSEED`s | I-15 |
| P12 | kernel boundary | `git diff --stat -- src/sandloom` for adding `QuotaAvailable` via `access_check`: empty; the moved atoms leave no tenancy/billing name in `shared/authz` (grep) | I-13 |
| P13 | DX task re-run | consumer atom lines 31 → 0; consumer advanced-tier names 4 → 0; framework/doc errors ≤ 6; files edited by hand ≤ 7 | perf-dx §3 |

Counts are exact (P2, P4); instructions and latency carry the stated noise; a P5-P7 regression
beyond noise is a fail unless the owner approves it (I-14 part 3).

## 9. Open questions for the owner

1. Is a declaration field on `@contract.read` (`access_check`) an acceptable home for an atom, or
   must atoms be declared apart from contracts (a separate provider declaration that still
   generates into the stub)?
2. Beginner-tier imports: may `from sandloom.sdk import OrgRole` become an import from the
   generated stub module, or should `sandloom generate` also write one per-app re-export module
   (`<app>.shared.access`) so a route imports every atom from one place? (requirements Q5)
3. Provider down: should an unreachable provider become an indeterminate 503 decision inside the
   algebra (so `A | B` allows on `B`, with deny log and metrics), as a separate change from the move?
4. Claim-tier rules that depend on the owner's settings (`past_due` grace): interpret at mint
   into a plain claim, or keep a descriptor refreshed off the request path? Which staleness is
   acceptable?
5. `Feature` / `WithinLimit`: move to billing as declared facts, or retire them (no bundled
   producer; the remote fact path drops their parameters)?
6. "A module cannot lower its own risk": is it enough that declared atoms' risks are generated and
   snapshotted, plus the route snapshot and the hand-rolled-authority heuristic, or must archcheck
   also flag a module's own `Requirement` that calls a method declared as an access check?
7. Bound-tenant third call: fix it (6.3 a, b or c), or keep 3 calls as the baseline the move must
   only not exceed?
8. `Credits`: may billing add a boolean `has_credits_at_least` read (the predicate stays at the
   owner), or must the atom keep comparing `get_credit_balance` on the consumer side (a comparison
   in the evaluator)?

## Sources

Code: cited inline (file:line at `38f33cb`). Probe: `_evidence/research-authz/probe_authz.py`,
output `_evidence/research-authz/probe_authz.out.txt`.

- [OPA: bundles](https://openpolicyagent.org/docs/bundles) (manifest `roots`, signing, separate distribution)
- [OPA: policy language, annotations](https://www.openpolicyagent.org/docs/policy-language#annotations) (`# METADATA`, `schemas`, `opa inspect -a`)
- [Cedar: validation](https://docs.cedarpolicy.com/policies/validation.html), [Cedar: templates](https://docs.cedarpolicy.com/policies/templates.html)
- [AWS Verified Permissions vs Cedar](https://docs.aws.amazon.com/verifiedpermissions/latest/userguide/getting-started-differences-verifiedpermissions-cedar.html)
- [SpiceDB: caveats](https://authzed.com/docs/spicedb/concepts/caveats), [OpenFGA: conditions](https://openfga.dev/docs/modeling/conditions)
- [authzed-codegen (third-party SpiceDB Go generator)](https://pkg.go.dev/github.com/danhtran94/authzed-codegen)
- [Google Zanzibar (overview)](https://en.wikipedia.org/wiki/Google_Zanzibar), [Zanzibar FAQ, MIT 6.824](https://pdos.csail.mit.edu/6.824/papers/zanzibar-faq.txt) (secondary sources; the USENIX ATC 2019 paper was not opened)
- [ASP.NET Core: policy-based authorization](https://learn.microsoft.com/en-us/aspnet/core/security/authorization/policies?view=aspnetcore-10.0), [custom policy providers](https://learn.microsoft.com/en-us/aspnet/core/security/authorization/iauthorizationpolicyprovider?view=aspnetcore-10.0)
- [o3co/grpc.authz (policy in proto method options)](https://awesome.ecosyste.ms/projects/github.com%2Fo3co%2Fgrpc.authz), [grpc-io: custom method options in interceptors](https://groups.google.com/g/grpc-io/c/Hkzlh1KHTt0), [protovalidate](https://protovalidate.com/)
- [Django REST framework: permissions](https://www.django-rest-framework.org/api-guide/permissions/), [DRF issue #6500](https://github.com/encode/django-rest-framework/issues/6500)
