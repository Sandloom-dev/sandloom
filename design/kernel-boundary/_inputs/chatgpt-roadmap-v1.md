# Sandloom: единая целевая архитектура и карта миграции

**Статус:** proposed / engineering design, не принятый ADR и не утверждение, что миграция выполнена.

**Основание:** исследование `arch/kernel-boundary` на `b8d85068f70335f30c067d46b971b900be522d00`, `baseline(1).md`, `requirements.md` (draft), исходники на этом SHA, плюс изменения безопасности на `main` `e264b097df96f1fa38c734c9388d577508b3966f` (PR #70). Ветки расходятся; перед реализацией нужен свежий baseline на целевой ветке. Запуски изолированной установки и consumer/RPC описаны в baseline и **не повторялись автором этого документа**. Код PR #70 проверен статически; его тесты здесь не запускались.

## 1. Одна архитектурная идея

**Sandloom — kernel of guarantees + composable domain modules + consumer-owned interfaces.**

- Ядро владеет **гарантиями исполнения** и **публичными механизмами**: lifecycle/loader, SDK primitives, UoW, SQL isolation, events/outbox/inbox, sagas, RPC/contracts/compatibility, JWT/claims validation, tenant scope, authorization algebra, risk classification, config validation и diagnostics.
- Бизнес-модуль владеет **предметными решениями**: Identity (credentials, account lifecycle, login policies, SSO, собственные security contracts), Tenancy (membership, organizations, invitations, org roles), Billing (plans, subscriptions, credits, Stripe, собственные access facts), Admin (консоль UI, domain resources). Батарейки устанавливаются независимыми wheels и подчиняются общим правилам.
- Приложение (composition root) выбирает установленные modules, service bindings, конфигурацию и **доверенные полномочия (authorities)**. Название пакета или саморегистрация не дают security authority автоматически.
- Другой модуль или приложение получает интерфейс провайдера через **сгенерированный consumer-owned stub/DTO/event schema/policy wrapper** (вариант, который следует проверить PoC). Потребитель **не импортирует реализацию** провайдера в runtime; в monolith/runtime local binding тот же контракт идёт локально, в split — через RPC.
- `sandloom.driverkit` обслуживает только infrastructure drivers. **НЕ создавать** `IdentityDriver`, `BillingDriver`, `TenancyDriver` и дополнительный универсальный capability registry. В первую очередь использовать уже существующие `sandloom.modules`, `ModuleAPI`, `CoreFacade`, contracts, `AccessFact`, `Requirement`.

### 1.1 Направление зависимостей

```text
                   APP / COMPOSITION ROOT
             bundles, bindings, authority grants,
                 deployment + config profiles
                            |
             configures/authorizes/assembles
                            v
  +----------------------------------------------------+
  | SANDLOOM CORE / SDK                                |
  | loader; UoW; events; contracts/RPC; JWT;           |
  | tenant scope/RLS; authz algebra/facts;             |
  | config validation; ops guarantees                 |
  +----------------------------------------------------+
          ^                   ^                   ^
      SDK/ports           SDK/ports           SDK/ports
          |                   |                   |
  +---------------+   +---------------+   +---------------+
  | IDENTITY      |   | TENANCY       |   | BILLING       |
  | owns identity |   | owns org data |   | owns payments |
  +---------------+   +---------------+   +---------------+
          \                   |                   /
           \----- typed consumer interfaces ------/
                    (contracts/events/facts)

Module A -> Module B implementation: forbidden.
Module A -> generated B wire contract: allowed.
Core -> generic registered implementation at runtime: allowed.
Core -> hardcoded specific implementation/domain contract: forbidden,
except documented platform ABI and explicit composition metadata.
```

**Не путать:** пакет необязателен ≠ платформенная гарантия необязательна. Без Identity нет логина по паролю; защищённые маршруты при этом не становятся публичными. Без Tenancy нет membership, но tenant isolation не отключается для scoped данных. Без Billing нет подписок; требование entitlement при отсутствии владельца не разрешает доступ.

### 1.2 Критерий, что остаётся в Core

Оставляем, только если: (1) описывает платформенный инвариант; (2) нет привязки к конкретному бизнес-состоянию; (3) гарантия/семантика отказа проверяема; (4) поставщик данных может быть заменён через существующий механизм; (5) отсутствие поставщика определено; (6) одинаковые контрактные гарантии работают local/remote в пределах поддерживаемых backend capabilities.

**Разрешённые понятия в Core:** principal/subject, signed claims, roles/permissions как общий тип, trust provenance, token issuance/verification/revocation, tenant ID/tenant scope/grants (не организация как бизнес-объект), admin operation risk, fact, policy, UoW, audit mechanisms.

**Не принадлежат Core:** Stripe, subscription statuses, credits, roles member/admin/owner для организации, invitations, SSO provider config, Billing/Tenancy конкретные contracts, generated DTO/JSON schemas стандартных модулей.

## 2. Целевые границы и ownership

| Область | В Core / SDK | В батарейке | В приложении |
|---|---|---|---|
| Module lifecycle | entry points / registration / atomic bootstrap | module descriptor, routes, tasks | набор модулей и обязательность |
| JWT | signing/verifying/revocation, reserved fields, claims integrity | Identity login, refresh, account management | issuer binding, approved authorities, keys |
| Claims | owner-aware merge + failure semantics | own contributions | per-claim authority grants |
| Authz | Requirement, And/Or/Not, AccessDecision, Role/Perm (provenance-aware), Entitled, evaluation | Credits/Subscribed/OrgRole and their meaning | policies on routes, provider bindings |
| Tenant | TenantScope, RLS, signed grant enforcement | membership, org roles, invitations | single-tenant/trusted-grant source |
| Contracts | ContractRef, registry, codec, transport, compat checks | provider schema + implementation | generated copy and binding |
| Events | envelope, delivery, schema validation | named events + payload schemas | generated consumer event schemas |
| Configuration | Pydantic-based schema composition, tiers, redaction, prod checks, accepted-risk processing | fields, constraints, secrets, risk definitions | values/profiles/deployment checks |
| Admin | platform operations API and high-risk action gates | UI/Admin resources; Identity incident commands | trusted platform-admin authority |
| Driver system | generic driver ports and driverkit boundary | no backend driver import | selects driver |
| CLI | generic commands and extension mechanism | module-specific commands/recipes | starter composition and templates |

**`sandloom.shared` не является предметным уровнем:** временно содержит платформенные primitives; после миграции domain code/artifacts удалить, остаток при необходимости переместить в Core без изменения semantics. Само удаление каталога не является целью.

## 3. Security: отдельное приоритетное направление

### 3.1 Что исправлено на текущем main

PR #70 (`e264b09`) ввёл `RESERVED_CLAIM_KEYS` и проверку дублирования ключа между разными claims contributors в `_merge_claim_outcomes`. На `b8d8506` был `claims.update(value)` с last-writer-wins. PR #70 — полезный **containment**: препятствует замене одного и того же claim двумя разными owners и запрещает contributors формировать `iat`, `jti`, `iat_us`, `sub`, `tenant_id`, `org_id`, `claims_sources`, `claims` и прочие envelope/subject поля.

### 3.2 Чего исправление ещё НЕ доказывает

Код PR #70 фактически выбирает владельца ключа по принципу **first writer in this token**: `key_owner.setdefault(key, owner)`. Нет заранее утверждённой карты `roles -> approved authority`, `tenant_ids -> approved authority`. Если ожидаемый authority не выдал ключ, другой contributor может оказаться единственным автором ключа; нужно проверить, может ли результат быть принят конкретным guard в поддерживаемых конфигурациях. Строка имени `identity` сама по себе тоже не доказательство полномочия.

Это *остаточный trust-model gap*, не утверждение об эксплуатации в реальном production HTTP flow. Полноценный threat model обязан различать доверенный установленный Python-код (он уже исполняется в процессе) и ошибочные/скомпрометированные удалённые providers; Core не является sandbox для враждебных Python-пакетов.

### 3.3 Целевая модель: approved claim authority

1. **System/token-envelope claims:** `sub`, `iss`, `aud`, `iat`, `iat_us`, `jti`, `nbf`, `exp`, `token_type`, `claims_sources*`, структурные поля UserContext формирует только Core / доверенный issuer path. Contributor не пишет их.
2. **Platform authority claims:** `roles`, `permissions`, `tenant_ids` и другие platform-security claims получают *заранее утверждённого владельца* из composition. Владелец определяется разрешённой конфигурацией и identity привязкой сервиса, не только строкой name. Core отказывает в mint при чужом ключе даже без столкновения. Должен быть ровно один authority на ключ в deployment.
   Для стандартного `sandloom new` manifest может быть **сгенерирован со стандартными предложенными bindings**, которые приложение явно принимает; не требовать от разработчика ручной настройки каждого ключа. Подмена/добавление владельца должна проходить validation/preflight и review; предложение модуля не является grant.
3. **Domain claims:** `subscription_status`, `plan_code`, `features`, `limits` и подобные принадлежат явно объявленным domain schemas/authorities, если используются для решения доступа. Для иных пользовательских claims допускается namespacing и некритичное отсутствие; запрещено неявное повышение authority.
4. `claims_sources` показывает успешно выполненных владельцев, но **все guards проверяют соответствие claim -> authority**, а не просто присутствие одного знакомого owner среди sources. Для signed token это можно сделать по заранее проверенной композиции/выходу issuer; желательно versioned auth provenance для legacy tokens. `Role` / `Perm` по-прежнему claims-only и не делают RPC.
5. В split issuer доверяет claims response только по authenticated binding и разрешённой identity удалённого сервиса (включая существующий pair-MAC и ограничения его угроз); имя контракта само не подтверждает владельца.
6. `require_platform_admin` и `platform_operator` используют **один** Core admission predicate; нет двух расходящихся реализаций. Authority grants и эффективный claims ownership принадлежат неизменяемому runtime snapshot/`CoreFacade`, а не process-global изменяемому реестру. Sandloom использует один app runtime на процесс (Option A), с явным reset для изолированных тестов.
7. Старые подписанные токены не получают автоматически новую модель provenance: принять политику legacy-token handling (отказ для privileged gates / перевыдача / короткий rollout) с проверкой revocation, clock-skew и UX.

**Недопустимо:** заменить hardcode `"identity"` на `config.get("admin_owner", "identity")` и считать trust-модель законченной.

**Готово, когда:** tests cover foreign-only claim, duplicate claims, spoofed owner, owner absence, delegated authority, remote provider, signing envelope, Role/Perm and platform gates, legacy token and refresh, split; unauthorized claims never allow.

## 4. Contracts, DTO, events и автономность модулей

**Выбранный основной вариант:** consumer-generated interfaces из существующего `sandloom generate` (с проверкой). Не отдельные `sandloom-*-api` wheels по умолчанию; они остаются возможным исключением, если PoC обнаружит реальную потребность в исполняемой consumer-side библиотеке, которую нельзя выразить generated declarations.

- **Provider source:** модуль хранит оригинальный contract method, DTO class, event schema/declaration и версионируемый snapshot. Каждый интерфейс имеет owner, contract name, semantic version и schema fingerprint.
- **Build/generate:** app и модуль-потребитель генерируют копии в **своём** Python-пакете. Получение исходной schema при генерации требует доступного provider declaration/snapshot в build/authoring environment (установленного provider в dev/build env либо отдельно доступного зафиксированного snapshot). На runtime у потребителя provider wheel **не требуется**. Не обещать «генерацию из несуществующего источника».
- **Runtime:** одна и та же копия DTO декодирует local и remote результаты. Provider владеет собственным DTO class; copy не содержит бизнес-методов или validators, чья семантика недостижима без provider. Preserve schema fidelity and compatibility tests.
- **Versions:** additive modifications могут сохранять backwards compatibility; breaking changes получают новый `.vN`. Проверить old consumer + new provider и new consumer + old provider, `ready` и 501/compatibility failure. Точное поведение optional/required checks сверить с гарантиями.
- **Event schemas:** domain event JSON не поставляются wheel `sandloom`; в consumer используются generated event schemas, с версионным compatibility check.
- **Migration:** `sandloom_billing/shared/contracts/tenancy_*`, `sandloom_admin/shared/contracts/identity_*`, `<app>/shared/contracts/...`; удалить kernel defaults `sandloom.shared` из `core/cli/generated.py` и `module_api/_provider.py` после миграции imports.

**Не трогать:** contract transport/runtime и CoreFacade без доказанного нарушения. **Не добавлять:** ещё один registry API packages, общий `sandloom-shared` wheel с интерфейсами всех стандартных модулей.

## 5. Authorization: ядро сохраняет алгебру, батарейки владеют атомами

- Оставить `Requirement`, `And`, `Or`, `Not`, `AccessDecision`, error categories, short circuit/tier sorting, `require`, `authorize`, `authorize_resource` без изменения observables. `Or` может разрешить на достоверной alternative branch, даже если другая indeterminate; `Not(indeterminate)` всегда indeterminate.
- В Core остаются `Role`, `Perm` (только после проверки claims provenance), `Entitled`, `Owner` там, где универсален, общий authz context.
- Domain atoms `Credits`, `Subscribed`, `SubscriptionActive`, `OrgRole`, `Feature`, `WithinLimit` и организация/биллинг-specific account target перемещаются к owners. Сигнатуры и `of=` должны сохранять явное указание субъекта; **не переименовывать `Account` в `Party` автоматически**.
- **Критическая техническая деталь:** нынешний `AccessFact/Entitled` не поддерживает произвольные параметры fact в `Entitled(...)`: `OrgRole(minimum)` и `Credits(amount, of=...)` нельзя просто заменить на текущий `Entitled("...")`. Требуется один PoC, который сравнит: (A) минимальное расширение existing facts с typed parameters и declarative wire metadata; (B) consumer-generated `Requirement` wrappers поверх существующих contract clients. Принимать вариант с меньшим runtime cost/новыми механиками при сохранении всех правил.
- Вплоть до PoC не вводить новый fact engine и не удалять существующие adapters до обеспечения полной feature parity.
- **Claim-only остаётся без I/O**, co-located provider выполняется локально; remote RPC — только при реальном split или требовании свежести. Предварительное получение `describe()`/schema кэшируется/проводится на startup, не запускается на каждый запрос без причины.
- Сохранить HTTP 402/403/501/503 и риск-порядок. Guard metadata должна быть доступна `sandloom check` через публичные declarations/snapshots: tenant requirement, resource requirement, criticality/risk, contract dependence, freshness/tier. Статическая проверка не должна зависеть от конкретных имён классов Billing/Tenancy; высокорисковые действия нельзя сделать «низкорисковыми» одним произвольным флагом.

**Желаемый DX:** пользователь пишет простую политику `OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant())`, импортируя typed wrappers из consumer-owned generated package, без ручного описания `Fact`, owner, RPC, tier. Framework API imports остаются через `sandloom.sdk`, generated API — отдельная разрешённая категория.

## 6. Tenant boundary и platform-admin

- `TenantScope`, signed tenant selection, revocation before binding, RLS and ORM guards остаются Core. `tenant_id`, tenant grants/allowlist — platform claim schema. `org_id` может остаться совместимым alias/selector на переходный период (при условии утверждённого ownership); не связывать абстрактный tenant с business organization.
- Tenancy владеет membership, `member/admin/owner` hierarchy, organizations, invitations, org role guards; поставляет tenant grants, но не реализует гарантию изоляции Core.
- Single-tenant mode может получать tenant scope от доверенной deployment configuration. В multi-tenant режиме отсутствие trusted grants не должно позволить выбрать произвольный tenant по заголовку.
- Platform operations guard остаётся Core. Identity может исполнять account/security workflows, но не становится единственным возможным issuer «по имени». При отсутствии доверенного admin authority privileged requests deny; команда инцидента без Identity показывает missing-provider diagnostic.

## 7. Module settings и lifecycle (важный sequencing)

Нельзя просто перенести `ModulesConfig` в `sandloom_billing.config` и забыть: в `create_app()` вызов `enforce_configuration(settings)` сегодня предшествует загрузке modules. Для module-owned settings нужна **двухфазная инициализация**, либо эквивалентная минимальная metadata discovery:

1. `discover`: прочесть app composition и установленные module descriptors/entry points без env/file I/O на import; найти schema и validation declarations.
2. `validate`: собрать ядро + **только подключённые** module settings, tiers, risks, secret/redaction, prod guards, profile-consistency; выполнить одну валидацию для app/worker/migrate/CLI (профиль entrypoint согласован). Секреты не попадут в dump/logs.
3. `activate`: создать инфраструктуру, загрузить/зарегистрировать implementations, routes, contracts, migrations, startup tasks; сверить фактический состав/декларации с validated snapshot.

**Нет:** процессного чтения env в момент импорта модуля, загрузки DB для открытия config schema, отдельного framework поверх Pydantic. Снимок настроек immutable для одного Sandloom app runtime/процесса (договорённость Option A).

- `BillingSettings`, `IdentitySettings`, `TenancySettings`, `AdminSettings` владеют своими полями.
- Module registry лишь собирает definitions; Core гарантирует validation tiers/risk ratchet, collision detection, profile parity, backward-incompatible removed keys policy, production fail-fast.
- По возможности сохранять текущие env names в первом шаге; унификацию названий делать отдельно и только с migration guide (без бессрочных compatibility aliases в runtime, если ADR запрещает их).

**Packaging bug:** по baseline `sandloom-billing` + голый `sandloom` без Celery bootstrap падает (`No module named celery`), хотя Celery optional. Проверить, должна ли поддерживаться эта комбинация. Если нужна независимость taskqueue, убрать eager import и выбрать driver при runtime; иначе корректно объявить обязательный extra/capability. То же проверить для Identity.

## 8. CLI / Starter / Admin / Shared

- Generic CLI (`new`, `generate`, `migrate`, `check`, `config`, diagnostics) остаётся Core.
- Domain commands (identity password/incident/bootstrap, admin console specific, tenancy invitations, billing env recipes) принадлежат модулям или application composition; подключения — существующий module registration/manifest или **один минимальный CLI extension**, если он нужен. Не создавать без необходимости множество entry-point groups.
- Каталог first-party modules, UI wizard и doc recipes могут быть инструментами DX с явными first-party examples. **Не объявлять всякое слово «billing» в шаблоне runtime нарушением.** Разделить static enforcement для shipped runtime и template/starter policy. Отдельный `sandloom-starter` wheel — отложить, пока нет доказанной необходимости.
- Platform operations endpoints и generic manifest compiler остаются Core; Admin UI — Admin wheel.
- Разделить `sandloom.shared` на платформенную основу и domain-owned surfaces; убрать module DTO/events/authz/tenancy membership compatibility из framework wheel. Физический перенос оставшихся primitives в `sandloom.core` — последний, возможно необязательный шаг.

## 9. Гарантии, тесты и perf/DX

### Mandatory architecture fitness suite

1. `kernel-only`: isolated wheel install, no `sandloom-identity/-tenancy/-billing/-admin`, CLI bootstrap, `/health` `/ready`.
2. `installed-subsets`: каждый поддерживаемый subset 4 батареек (включая 1-модульные), с явно заявленными infra prerequisites; никаких скрытых import errors.
3. `fake-provider`: сторонний Identity-like issuer/claim authority, Tenancy-like tenant grants provider и Billing-like fact/contract provider не требуют редактировать Core.
4. `consumer-only`: install `sandloom` + wheel consumer **без** provider wheel, remote binding authenticated, typed local DTO copies and policies work.
5. `mono/split`: same policy truth table, errors and semantics, excluding documented backend-specific differences; no automatic extra RPC.
6. `versions`: provider N/consumer N+1 and reverse; additive and breaking schema cases; readiness/diagnostics.
7. `security`: reserved claims, authorized key with no collisions, forged owner, missing authority, remote spoof, legacy token, claims-only Role/Perm, tenant isolation, platform gates, `Not(indeterminate)` and `Or` valid-alternative behavior.
8. `config`: third-party module adds semantic/ops/secret/prod check; app, worker, migrate and CLI agree; absent module's settings not active.
9. `generation`: sorted modules, repeated generation, hash seeds, schema fingerprint, stale artifacts; check mode never writes.
10. `architecture guards`: import-linter includes external first-party packages; distribution closure handles optional extras correctly; no `sandloom.shared` module DTOs; code and generated wheels checked for approved ownership edges; static checks don't mistake documentation for runtime.
11. `DX`: tasks for human + AI agent: create module, add typed authz fact, generate contract, split deploy. Target no manual kernel edits, stable beginner concepts, clear diagnostics.
12. `perf`: compare instruction counts on request/write/claims-only/authz/mint; measure p50/p95/p99 and number of DB/RPC calls in mono and split. Co-location not allowed to add RPC or DB reads merely because of move of classes; claims-only stays 0 I/O; evaluate +1% budget per agreed target without sacrificing security.

### Performance contract

- **Zero new network hops** for the same local topology and same freshness policy.
- **Zero I/O** on claims-only `Role` / `Perm` paths.
- Descriptor resolution/metadata caches built at generation/startup or bounded cached; not per-request schema discovery.
- Preserve tier sorting, existing short-circuit, UoW boundaries, fail-closed outcome categories.
- Compare measured results on same hardware / DB / runtime profiles. No ungrounded percentage promises.

### DX contract

```python
# Псевдокод желаемого consumer-facing API — не текущая реализация.
from sandloom.sdk.authz import require, Role
from shop.shared.authz import OrgRole, SubscriptionActive, Account

policy = OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant())
# Синтаксис policy должен быть простым и типизированным.
# Ни FactSpec, ни RPC binding, ни schema details вручную в route.
```

## 10. Work breakdown: независимые PR, порядок, критерии выхода

Каждый PR должен ссылаться на baseline edge ids, invariants I-01..I-19, контрольные тесты и содержать comparison **before/after**. Ниже **логическая зависимость**, не календарные сроки.

| PR / фаза | Изменение | Что не трогать | Exit gate |
|---|---|---|---|
| PR-00 | Rebase baseline на целевую branch, оформить ADR-A..D status proposed, утвердить threat model и скорректировать спорные I/H | runtime | однозначные инварианты, owner и evidence |
| PR-01 | Security PR #70 audit + pre-approved authorities, provenance enforcement, single platform admin predicate, old-token migration | authz algebra, Billing contracts | все security negative/fail-closed тесты; нет лишнего request RPC |
| PR-02 | Fix architecture guards / dependency closure; synthetic negative tests, baseline allowlist ratchet | generic module loader | deliberate violation causes CI failure and useful code |
| PR-03 | Packaging `celery` optional boundary + lean/consumer/installed subsets tests | taskqueue semantics | supported subsets bootstrap with declared dependencies |
| PR-04 | Consumer contract artifacts for first-party consumers + schema fingerprints + mixed-version harness | transport/codec unless test proves bug | independent provider wheel, caller without provider, local/remote parity |
| PR-05 | Remove framework-owned domain DTOs/event JSON, special-cased `sandloom.shared` generation paths | generic generator | kernel wheel free of owner artifacts; generate stable |
| PR-06 | Module-owned settings metadata, two-phase discovery/validate/activate, external module fixture | platform config guarantees | all entrypoints validate, no env read on import, secrets safe |
| PR-07 | Authorization PoC: existing AccessFact parameter extension vs typed consumer Requirement, benchmark DX/perf + security | existing algebra | choose one by evidence; no new network/DB ops for mono |
| PR-08 | Migrate domain atoms/guards and risk-check metadata, preserve status codes and `of=` semantics | platform Role/Perm/require | complete mono/split policy truth-table, DX acceptance |
| PR-09 | CLI/admin/platform operation ownership, templates/deploy/recipes, `shared` last cleanup | UX contracts unless explicitly reviewed | CLI smoke/golden path, zero remaining unapproved runtime owner edges |
| PR-10 | Full conformance, version matrix, migration codemod/docs, ADR accepted, release line | no unreviewed capability removal | guarantee ledger green, release wheels verified |

**Риск/приоритет:** security authority — P0; packaging/guards — P1; contracts/settings — P1; authz portability/risk checks — P1, высокий риск; CLI/shared final — P2. Реальный порядок PR-04 и PR-06 может быть независимым; PR-07 PoC запустить **до** необратимого удаления authz defaults. Не делать один giant PR.

## 11. Решения, которые принимаем / НЕ принимаем

### Базовые архитектурные решения

1. **Принимаем:** независимость implementation от Core, не независимость Core от понятий tenant/subject/role/authorization.
2. **Принимаем:** один module loader, один contract/RPC system, одна authz algebra, одна driver system только для infrastructure.
3. **Принимаем:** security claims имеют владельцев, утверждённых composition, а не первыми авторами ключа.
4. **Принимаем как primary candidate:** consumer-owned generated DTO/stubs/events.
5. **Принимаем:** domain settings принадлежат модулям; kernel сохраняет validation guarantees.
6. **Принимаем:** platform guards остаются в Core; доменные guards/atoms — owner domain + consumer-safe API.
7. **Принимаем:** `Account` не переименовывать автоматически; явно сохранить выбор authorization subject.
8. **Принимаем:** проверять отдельно packaged/runtime dependencies, static naming, execution and security authority; documentation не смешивать с runtime violations.

### Ещё необходимо доказать до финального ADR

- **Authz portability:** выбрать минимальную реализацию parameterized typed atoms для OrgRole/Credits; текущий `Entitled` один не поддерживает эти параметры.
- **Contract generation without provider at runtime:** build-time access к schemas, mixed versions, no namespace collisions.
- **Settings bootstrap:** двуфазная конфигурация без скрытых import-time effects и без дополнительного сложного registration framework.
- **Authority validation:** локальные и удалённые namespaces, distinct app authority vs module self-claim, legacy JWT rollout.
- **Performance + DX:** сравнение до/после на одной топологии и сценариях, сохранить текущую простоту route code.

### Явно отклоняем пока

- Большой rewrite CoreFacade/loader/RPC/UoW.
- Обязательные `IdentityDriver`, `TenancyDriver`, `BillingDriver`.
- Ещё один Fact engine или универсальный capability registry поверх уже имеющихся.
- Отдельный `sandloom-shared` mega wheel или обязательные `*-api` wheels для всех батареек.
- `Account -> Party` только ради устранения слова `Account`.
- Сначала удалить `sandloom.shared`, а потом думать о последствиях.
- Удалить `"identity"` из admin gate без установленного нового trusted authority.
- Принять сразу весь план 8 фаз Claude без сравнительного PoC.

## 12. Какой результат означает «готово»

- Изолированный `sandloom` запускается; runtime kernel не знает names/contracts/config конкретных batteries, кроме явно одобренных platform ABI/compatibility clauses.
- Сторонний модуль с settings, typed domain policy, fact, contracts, events и admin declarations не требует редактирования framework и может быть включён в monolith/split.
- Один и тот же module wheel работает в поддерживаемых topology без другого module implementation в runtime process; framework не поставляет domain DTO/events.
- Trusted claims невозможно перезаписать чужому contributor, даже при отсутствии legitimate owner data; Core gates используют корректный authority, в том числе при split и legacy tokens.
- Все прежние гарантии остаются в guarantee ledger и имеют запускающиеся проверки; authz truth table и statuses сохранены.
- Нет дополнительных network hops при сохранённой co-location; claims-only path не делает I/O; performance измерен, DX eval не ухудшен критически.
- Architecture checks находят умышленно введённые нарушения и не ломают общий module loader.
- ADR-A/B/C/D приняты с явными cost/alternatives и есть migration guide на одну согласованную breaking prerelease line.

## 13. Точки источников

- [Baseline на зафиксированном SHA](https://github.com/Sandloom-dev/sandloom/tree/b8d85068f70335f30c067d46b971b900be522d00)
- [Kernel claims merge на main e264b09](https://github.com/Sandloom-dev/sandloom/blob/e264b097df96f1fa38c734c9388d577508b3966f/src/sandloom/core/facade/_claims.py)
- [Tests claims ownership на main](https://github.com/Sandloom-dev/sandloom/blob/e264b097df96f1fa38c734c9388d577508b3966f/src/sandloom/core/tests/test_claims_key_ownership.py)
- [Original Core settings](https://github.com/Sandloom-dev/sandloom/blob/b8d8506/src/sandloom/core/config/settings.py)
- [Original contracts generator](https://github.com/Sandloom-dev/sandloom/blob/b8d8506/src/sandloom/core/cli/generated.py)
- [Original authz facts](https://github.com/Sandloom-dev/sandloom/blob/b8d8506/src/sandloom/shared/authz/facts.py)
- [Original authz algebra](https://github.com/Sandloom-dev/sandloom/blob/b8d8506/src/sandloom/shared/authz/algebra.py)
- [ADR-0010 (status Proposed on inspected SHA)](https://github.com/Sandloom-dev/sandloom/blob/b8d8506/docs/adr/0010-public-api-1.0.md)
