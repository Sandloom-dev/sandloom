# Sandloom: единая целевая архитектура, трассировка нарушений и исполняемая карта миграции

**Revision 2 / 2026-11-08.** Добавлены coverage matrix всех 46 judged edges, решения по 12 owner-вопросам, отдельные решения для A19/B-R2, public SDK, graph, security, split/rolling upgrade, PR gates.

> **Приоритет в случае расхождений:** разделы 14–20 уточняют/заменяют предварительные формулировки разделов 1–13. Ни одна рекомендация не равна approval владельца; без реального PoC и regression suite целевая архитектура остаётся предложением.

**Статус:** proposed / engineering design, не принятый ADR и не утверждение, что миграция выполнена.

**Основание:** инвентаризация `baseline(1).md` на ветке `arch/kernel-boundary`, SHA `b8d85068f70335f30c067d46b971b900be522d00`; `requirements.md` (draft, без owner approval); статическая сверка с `main` на SHA `fe24a7960366551789f00caeae54d908bfed9f1c` (включает PR #70 по JWT claims). Ветки расходятся. **Классификация 46 edges относится к `b8d8506`, а не автоматически к `fe24a79`.** Утверждать выполнение PR по коммит-сообщению нельзя: тесты и benchmarks для этого документа не запускались. Результаты isolated install/RPC взяты из транскриптов baseline и здесь не повторялись.

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

### 3.1 Что уже изменено в исследованном main (`fe24a79`)

PR #70 (`e264b09`, присутствует в `fe24a79`) ввёл `RESERVED_CLAIM_KEYS` и проверку дублирования ключа между разными claims contributors в `_merge_claim_outcomes`. На `b8d8506` был `claims.update(value)` с last-writer-wins. PR #70 — полезный **containment**: препятствует замене одного и того же claim двумя разными owners и запрещает contributors формировать `iat`, `jti`, `iat_us`, `sub`, `tenant_id`, `org_id`, `claims_sources`, `claims` и прочие envelope/subject поля.

### 3.2 Чего исправление ещё НЕ доказывает

Код PR #70 фактически выбирает владельца ключа по принципу **first writer in this token**: `key_owner.setdefault(key, owner)`. Нет заранее утверждённой карты `roles -> approved authority`, `tenant_ids -> approved authority`. Если ожидаемый authority не выдал ключ, другой contributor может оказаться единственным автором ключа; нужно проверить, может ли результат быть принят конкретным guard в поддерживаемых конфигурациях. Строка имени `identity` сама по себе тоже не доказательство полномочия.

Это *остаточный trust-model gap*, не утверждение об эксплуатации в реальном production HTTP flow. Полноценный threat model обязан различать доверенный установленный Python-код (он уже исполняется в процессе) и ошибочные/скомпрометированные удалённые providers; Core не является sandbox для враждебных Python-пакетов.

**Конкретный остаточный дефект модели:** если ожидаемый provider вовсе не возвращает `roles`, значение `roles` от единственного постороннего provider не конфликтует ни с чем. Также `CoreFacade.add_claims_contributor(..., owner=...)` допускает имя owner из аргумента. Нужны negative tests «foreign-only claim», «spoofed owner», «identity absent», а не только duplicate-key. Это статически вытекает из текущего `key_owner.setdefault(key, owner)`; внешняя эксплуатация не проверена.

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
2. `installed-subsets`: **все 16 subsets** четырёх батареек (включая 1-модульные); ожидаемая готовность либо owner-approved отказ по необходимому контракту, с явно заявленными infra prerequisites; никаких скрытых import errors.
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
| PR-00 | Синхронизировать baseline с main; трассировать 46 edges; ADR-A..D proposed; зафиксировать решения owner Q1–Q12, threat model и спорные I/H | runtime | однозначные инварианты, owner и evidence |
| PR-01 | Security PR #70 audit + pre-approved authorities, provenance enforcement, single platform admin predicate, old-token migration | authz algebra, Billing contracts | все security negative/fail-closed тесты; нет лишнего request RPC |
| PR-02 | Fix architecture guards / dependency closure; synthetic negative tests, baseline allowlist ratchet | generic module loader | deliberate violation causes CI failure and useful code |
| PR-03 | Packaging `celery` optional boundary + lean/consumer/installed subsets tests | taskqueue semantics | supported subsets bootstrap with declared dependencies |
| PR-04 | Consumer contract artifacts for first-party consumers + schema fingerprints + mixed-version harness | transport/codec unless test proves bug | independent provider wheel, caller without provider, local/remote parity |
| PR-05 | Remove framework-owned domain DTOs/event JSON, special-cased `sandloom.shared` generation paths | generic generator | kernel wheel free of owner artifacts; generate stable |
| PR-06 | Module-owned settings metadata, two-phase discovery/validate/activate, external module fixture | platform config guarantees | all entrypoints validate, no env read on import, secrets safe |
| PR-07 | Authorization PoC: parameterized AccessFact vs consumer Requirement; отдельно A19 closed adapters и B-R2 tenant-kind; измерить DX/perf/security | existing algebra | choose one by evidence; no new network/DB ops for mono |
| PR-08 | Migrate domain atoms/guards and risk-check metadata; проверить обход правил через алиасы; review SDK ABI, statuses и `of=` | platform Role/Perm/require | complete mono/split policy truth-table, DX acceptance |
| PR-09 | CLI/admin/platform operations, templates/deploy/recipes, domain error codes и migration lint, `shared/membership.py`, `shared` cleanup | UX contracts unless explicitly reviewed | CLI smoke/golden path, zero remaining unapproved runtime owner edges |
| PR-10 | Full conformance, graph coverage, version matrix, public SDK review, migration docs, benchmarks; ADR accepted только после owner approval | no unreviewed capability removal | guarantee ledger green, release wheels verified |

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


---

## 14. Coverage gate: все 46 judged baseline edges

Это **обязательная карта реализации**, а не список из 46 независимых рефакторингов. Edge IDs и исходные verdict взяты из `baseline(1).md` на `b8d8506`: `A19` (1), `B-*` (40), `C*` (5). Привязки к PR и следующие решения — **предложения ревью**, ещё не утверждённые owner и не проверенные тестовым запуском на `main`. PR-00 обязан обновить `source:line`, наличие edge и verdict на одном актуальном SHA; исчезнувшие edges отмечаются `fixed`, а не удаляются из истории.

**Обозначения:** `V` — violation, `D` — disputed, `A` — allowed по baseline; `P` — предложенное решение. `PR` — этап из §10. `Proof` — конкретный сценарий/приёмочное испытание из `requirements.md` (§5) или новый тест, который нужно добавить.

| Edge | Baseline | Решение P и владелец после миграции | PR | Proof / exit test |
|---|---|---|---|---|
| `A19` | D | Убрать закрытый process-wide registry `entitlements/membership` или доказать его необходимость; для third-party policy использовать `Requirement`/`AccessFact`, не создавать ещё один registry | 07–08 | C-H1, C-F3; inventory policy без замены глобального slot |
| `B-I1` | V | Identity password policy/CLI-owned adapter; generic CLI не импортирует `sandloom_identity.password_policy`; без provider — явная ошибка, не немая смена правил | 09 | A-B2; lean CLI без Identity |
| `B-R1` | V | Конкретные Billing/Tenancy live adapters и service names убрать из Core; consumer binding/typed domain policy у owner | 07–08 | C-H2, C-H3, C-H4; local и remote |
| `B-R2` | V | `tenant_kind` не определять через обязательный membership adapter Core: отделить tenant scope, tenant kind и Billing account target; unknown/canonical fail closed | 07–08 | C-B4, C-H2; no Tenancy, single-tenant, RPC count |
| `B-R3` | V | Убрать `WELL_KNOWN_FACT_OWNERS`; explicit owner из декларации или namespaced fact IDs и migration plan | 07–08 | C-H3, C-B2; `inventory.*` без Core patch |
| `B-R4` | D | Identity owns admin user CRUD/password commands; Core оставляет generic privileged operation shell и bootstrap security guarantee | 01, 09 | A-B2; privileged create/list mono/split |
| `B-R5` | D | Core retains token revocation primitive; user listing, membership fan-out и password reset — команды/контракты владельцев | 01, 09 | A-B1, A-B2; revocation/incident split |
| `B-R6` | V | Сделать `fake_access_fact` generic; billing-specific stub/tenant_type wiring перенести в Billing тестовый helper | 08 | C-H1, C-F1; tests без Billing |
| `B-S1` | D | `require_platform_admin` использует проверенную authority по claim, не строку `identity`; единый predicate | 01 | A-B3, A-F2, A-F3; forged/absent authority |
| `B-S2` | D | `platform_operator` использует тот же Core predicate, что B-S1; сохранить отказ без trusted authority | 01 | A-B1, A-B3; parity всех platform routes |
| `B-S3` | D | `tenant_ids` — допустимый платформенный security claim ABI; grants из утверждённого owner; selector/header не даёт полномочий | 01 | I-10, A-F2; foreign tenant, missing grants, revocation |
| `B-C1` | V | Identity/Billing/Admin own fields; Core composes immutable validated module settings | 06 | D-H1, D-H2, D-H4 |
| `B-C2` | V | `TENANT_CLAIMS_MAX_ORGANIZATIONS` принадлежит Tenancy; Core ограничивает общий payload/token size, если нужно | 06 | D-H1; no Tenancy config pollution |
| `B-C3` | V | 2FA key-strength validation принадлежит Identity metadata, применяется до activation | 06 | D-F1; app/worker/migrate/CLI |
| `B-C4` | V | Stripe/fake PSP validation принадлежит Billing, с прежней production fail-fast семантикой | 06 | D-H3; no Billing excludes Stripe checks |
| `B-C5` | V | Module-owned accepted risks + проверка их namespace/owner в общем Core risk processor | 06 | D-F4; unknown risk/removed module |
| `B-C6` | V | Module-owned tiers и semantic count; framework ratchet политики и существующих bundled settings сохраняется | 06 | D-B3; tier review + ratchet |
| `B-C7` | V | Deploy-profile shared settings декларирует модуль; generator собирает env из composition, не hardcoded Billing | 06, 09 | D-B2; split profile equality |
| `B-C8` | V | Backup/restore instruction fragments предоставляет module metadata; Core общий backup engine | 09 | backup plan with/without Identity/Billing |
| `B-C9` | V | Platform settings UI динамически показывает validated metadata модулей, не `billing_provider` константу | 06, 09 | D-B5; no Billing settings displayed |
| `B-C10` | V | Console link target приходит из app/Admin capability; password reset identity-owned; отказ когда target отсутствует | 09 | D-B5, A-B2; no console/default frontend |
| `B-N1` | V | Admin manifest contributor узнаётся через module declaration, а не `sandloom_admin` literal | 09 | A-H3; third-party admin resource |
| `B-N2` | V | Имя `admin-manifest.json` и publish/read protocol — owner Admin/explicit declared resource | 09 | manifest generation consistency |
| `B-N3` | V | `/admin/set-password` page принадлежит Admin UI; ссылка от composition/Admin declaration, без `admin` hardcode | 09 | password link custom frontend/absent Admin |
| `B-N4` | V | Start page получает UI links через manifest/capability declarations приложения | 09 | new app with/without Admin |
| `B-N5` | D | First-party `new` preset разрешён как DX-композиция, но не runtime dependency; approved starter policy | 09 | A-H1; generate third-party module without kernel edit |
| `B-N6` | D | `new` module-specific notes/env metadata — presets/templates owner; статический пример может остаться с явной классификацией | 09 | A-H3; custom starter |
| `B-N7` | D | Wizard choices from explicit catalog; `admin ⇒ identity` только dependency declaration, не Core semantic rule | 09 | wizard with alternate issuer |
| `B-N8` | V | Deploy environment suggestions из module config declarations, не `MODULE_ENV` hardcodes | 09 | D-B2; third-party module env |
| `B-N9` | V | DB names и issuer signing role в deploy templates derived from composition; keys CLI говорит `issuer` без Identity assumption | 09 | A-B1; split with alternative issuer |
| `B-N10` | D | Agent split-deploy skill: оставить first-party example, явно отделить от enforced runtime boundary | 09 | docs/examples smoke; no false positives |
| `B-N11` | D | Recipes и contract imports owner/app; examples могут показывать Billing/Tenancy, но не связывают Core release с их stubs | 04–05, 09 | recipe compile without kernel domain DTO |
| `B-N12` | V | Billing owns `Subscribed/Feature/WithinLimit/SubscriptionActive/Credits/Account` semantics; consumer-safe typed API | 07–08 | C-H2, C-B1, C-B4; explicit `of=` |
| `B-N13` | V | Tenancy owns `OrgRole` и role hierarchy; Core only evaluates generic `Requirement` | 07–08 | C-H4, C-F1; alternate tenancy |
| `B-N14` | V | Domain atoms leave platform beginner exports by explicit SDK ABI review; generated consumer public API accepted category | 08, 10 | I-17; API baseline + DX import checks |
| `B-N15` | V | Identity owns `Permissions`/`Roles` named constants; Core keeps generic `Role`/`Perm` types | 08 | public API baseline + import-surface tests |
| `B-N16` | D | Platform `UserContext.roles/permissions` как generic verified claims можно сохранить; убрать Identity doc assumption; authority must be checked | 01, 08 | C-B3, A-F2; no provenance must deny |
| `B-N17` | V | Static rules switch to declared trusted metadata plus enforceable route risk; no hardcoded `OrgRole`/Billing names | 08 | synthetic alias/imported atom bypass tests |
| `B-N18` | V | Domain error definitions/diagnostics move with Billing; Core only generic error protocol/codes | 08–09 | error-code review; 402/403/501/503 parity |
| `B-N19` | V | Identity-specific redundant index lint moves to Identity-owned migration checks | 09 | schema lint when Identity present/absent |
| `B-N20` | A | Оставить: Tenancy упомянут как пример в docs `contract_cache_shadow`; не runtime edge | 02 | boundary checker distinguishes example vs default |
| `C1` | V | Contract stubs generate and ship only in consumer-owned package | 04–05 | B-H2, B-B2, B-F1 |
| `C2` | V | DTO copies generate in consumer-owned package; no provider implementation import at runtime | 04–05 | B-H2, B-B5, B-F1 |
| `C3` | V | Event JSON schemas owned/published by event provider and copied to interested consumer/release artifact; no Core wheel files | 05 | B-H4; event replay/mixed versions |
| `C4` | D | Remove privileged kernel `sandloom.shared` default, preserve app-local shared package; wheel owns no module artifacts | 04–05 | B-B5, I-11 kernel wheel diff |
| `C5` | V | `member_event_action`/`refresh` compatibility logic moves to Tenancy consumer helper or app, not dropped | 05, 09 | old version-less events; out-of-order; duplicate; concurrent refresh |

**Контроль целостности матрицы:** 1 A19 + 40 B + 5 C = **46 уникальных IDs**. Исходные verdict: 32 V, 13 D, 1 A. Это **покрытие планом**, не число исправленных нарушений. Для каждого `D` PR-00 должен записать `accept/reject/change` и supporting test. `B-N20` сохраняется как контроль, что scanner не запрещает примеры.

### 14.1 Результат PR-00: machine-readable edge register

Рекомендуемый `docs/architecture/assessments/kernel-boundary/edges.yml` (новый **проверочный артефакт**, не новый runtime registry):

```yaml
# Псевдосхема, которую должен утвердить owner и проверить CI.
- id: B-R2
  baseline_sha: b8d85068f70335f30c067d46b971b900be522d00
  current_sha: <pinned-reassessment-sha>
  status: open                 # open / fixed / retained-approved / not-applicable
  verdict: disputed            # violation / disputed / allowed
  owner: billing               # or platform / application
  decision: <ADR-C-section>
  pr: PR-07
  invariant: [I-06, I-07, I-13]
  tests: [C-B4]
  evidence: [<code-path-with-line>]
```

CI **не** пытается обнаружить все архитектурные нарушения одним `grep`: оно (а) проверяет известные edges и новые доменные literals/импорты/артефакты, (б) выполняет mutation-negative tests, (в) проверяет реальные wheels и runtime conformance. Dynamic loader по `sandloom.modules` намеренно разрешён. Каждый PR сравнивает «новые / удалённые / одобренные» edges и запрещает увеличение неутверждённого debt.

## 15. Разрешение 12 owner-вопросов из `requirements.md` — рекомендации, НЕ approvals

Ниже — предложенные решения. Перед ADR-A..D **владелец обязан** отметить каждый пункт `approved / changed / rejected`, дату, причины. Пока статус `proposal`; реализация не может ссылаться на эту таблицу как на оформленное согласование.

| Q | Рекомендованное решение | Последствие / критерий |
|---|---|---|
| Q1 | Утверждать I-01…I-19 **после точных поправок** к I-02, I-06, I-08/09, I-14, I-17, I-19 (ниже) | Инварианты получают однозначные проверяемые outcomes |
| Q2 | H-1…H-13 сохранить после исправления H-12; **не назначать произвольный максимум числа механизмов** | Новый механизм обязан закрывать edge либо необходимый invariant и проходить cost comparison |
| Q3 | Production kernel (`src/sandloom/{core,sdk,shared,main.py}`) — строгий boundary; kernel integration tests (не shipped) могут импортировать batteries **явно** | Guards различают shipped runtime / тесты / examples |
| Q4 | Стабильные contract declarations должны порождать byte-identical output **при той же версии генератора**. При смене версии генератора diff допускается только с явной причиной, миграцией и runtime compatibility | Нельзя молча менять старые snapshots; `sandloom test` не пишет |
| Q5 | При замене Billing разрешены изменения **application gate code**, но **не Core**; если declared API одинаков, изменения gates не требуются | Не навязывать альтернативам один доменный `BillingProvider` ABI |
| Q6 | Env setting отсутствующего модуля не валидировать и не показывать; **без обязательного warning** по умолчанию; strict diagnostics могут предупреждать о typo | Не превращать clean split deployment в ошибки чужих env vars |
| Q7 | Семантические настройки стороннего модуля обязательны для классификации/review; **не считать их в жёсткий Core ratchet 16**, если owner не утверждает новую общую политику | Bundled baseline ratchet не ослабляется, third-party ownership проверяется |
| Q8 | Accepted risk отсутствующего модуля в production/strict profile — **отказ как inactive/unknown risk**. Для иных профилей предпочтителен тот же отказ ради ранней диагностики | Единый boot decision во всех entrypoints; не «тихо inert» |
| Q9 | Core reserved structural JWT keys **и** все security-sensitive claims, читаемые Core, под authority grants; domain claims, используемые для access decisions, — собственные owner schemas | `subscription_status` не становится Core-owned только потому, что его проверяет domain policy |
| Q10 | **Да**: запретить `Role/Perm` trust без подтверждённого provenance; pre-1.0 breaking change с migration guide и политикой legacy tokens | Никаких silent privilege escalation на старых токенах |
| Q11 | +1% instruction budget сохранить для request/write согласно ADR; authz/mint измерять отдельно; **не вводить hard CI gate без воспроизводимого harness**. Исключение на обязательную security-защиту требует owner approval | Сравнить instruction counts, latency и RPC/DB counts, release gate, не подгонять безопасность под число |
| Q12 | CLI/starter/recipes и физическую судьбу `shared` решать **после A–D**, но security/package fixes не ждать | PR-09 следует после substrate и authz PoC |

### 15.1 Точные поправки к требованиям до approval

- **I-02:** запретить hardcoded imports конкретных business implementations из shipped Core; **разрешить** generic `import_module(descriptor.path)` в loader. Non-literal dynamic import вне approved loader либо запрещён, либо требует явного review. Не писать «Core вообще не может импортировать модуль», иначе loader ошибочно заблокирован.
- **I-06:** local/remote parity — одинаковые контрактные результаты/ошибки/решения, границы UoW и tenant authority **при одинаковых заявленных capabilities**; backend-specific PostgreSQL/SQLite semantics должны быть явно задокументированы и покрыты отдельными tests. Не обещать абсолютной идентичности неподдерживаемых транзакционных режимов.
- **I-08/I-09:** заранее утверждённая authority map, доказанная identity bindings и verifier semantics; first writer of a JWT key **не** считается доверенным владельцем. Отказ без expected authority. Scope доверия для in-process installed Python code и remote claims providers различается.
- **I-14:** не снижать обязательную безопасность ради +1% budget; сохранять отсутствие нового I/O и новую latency matrix. +1% gate только после воспроизводимых baseline/candidate запусков и owner decision.
- **I-17/H-8:** `sandloom.sdk` остаётся **единственным платформенным публичным API**, но generated contract DTO/stub и **generated consumer policy API** являются отдельными consumer-owned интерфейсами, разрешёнными для импорта модулем. Это изменение формулировки требует ADR amendment и snapshot update.
- **I-19:** не заменять «every subset» на расплывчатое «supported subsets». Перечислить **все 16 комбинаций** четырёх батареек, разрешённые required contracts, минимальные заявленные инфраструктурные prerequisites и ожидаемые исходы. Если owner хочет сузить гарантию — оформить письменное исключение, а не скрытое редактирование теста.
- **H-12:** новый registry/wrapper/phase/manifest допустим, лишь если он убирает baseline edge **или** необходим для I-xx guarantee, и нет более простой реализации на существующих API. Каждый новый механизм перечислить в cost ledger (код, пакеты, startup, hot path, DX).
- **H-9:** никаких бессрочных compatibility aliases/deprecated platform APIs. При breaking prerelease — автоматическая миграция generated артефактов и upgrade guide. **Wire/event backward compatibility при rolling upgrade — отдельная эксплуатационная гарантия, не скрытый deprecated SDK alias**; её тестировать B-H4/C5.

## 16. Security contract: authority, provenance, tenants и privileged gates

### 16.1 Threat model и границы доверия

Доверенные code paths: Core token mint/verify, подписывающий issuer и явно связанный с ним authority config. Установленный произвольный Python-модуль в том же процессе **не изолирован sandbox** и может исполнять код, поэтому цель — предотвратить неправильные/неавторизованные claims в пределах supported extension APIs и spoofed remote contributions, а не защититься от произвольного RCE внутри процесса.

Состояния:

1. `issuer` — одобренный подписывающий token issuer (в split only signer holds private key).
2. `claim authority` — заранее одобренный owner каждой security claim (может отличаться от issuer).
3. `binding identity` — конкретная service endpoint/authentication и verified provider declaration, а не одно строковое имя контракта.
4. `claim source` — успешно завершённый contributor; **не** равно доказательству ownership конкретного claim.
5. `verifier` — процесс, проверяющий signature/issuer/audience/revocation и применяющий trust policy к утверждениям токена.

Пример **схемы композиции, а не текущего API**:

```yaml
security:
  issuer:
    module: identity
    binding: identity.token_issuer       # explicit verified binding, not name match
  claim_authorities:
    roles: identity
    permissions: identity
    tenant_ids: tenancy
  privileged_role:
    claim: roles
    value: admin
  authority_policy_version: 2
```

**Требуемая реализация:** на boot Core сопоставляет authority grants с установленным `sandloom.modules`/проверенным remote service binding; запрещает неоднозначность/отсутствие обязательного grant; issuer отказывает в mint при чужом ключе **даже если legitimate owner не выдал этот ключ**; verifier не принимает конфликт authority policy/legacy privileged provenance молча. Расхождение issuer/verifier deployment policy — диагностируемая несовместимость; формат version/fingerprint доказать PoC и выбрать минимальный подход (не обязательно новый JWT claim, если безопасно реализуется существующими binding/config). Remote pair-MAC сам по себе не подтверждает предметного authority без корректного endpoint binding и key policy. Существующая revocation и key rotation остаются обязательными.

### 16.2 Negative tests PR-01 (добавить сверх PR #70)

| Случай | Обязательный исход |
|---|---|
| `identity` объявлен, но не возвращает `roles`; `rogue` один возвращает `roles=['admin']` | Mint fails, privileged action denied; no first-writer ownership |
| Локальный contributor передаёт `owner='identity'`, но его регистрирует другой module | Registration/boot refuses with stable error code |
| Сторонний module имеет short name `identity`, но approved bound identity иной | Refuse authority grant or boot; no platform admin |
| Remote `rogue.claims.v1` отвечает полем `tenant_ids` или `roles` | Mint fails; no minted foreign claim |
| Auth authority down / missing | Mint 503 if critical; previously minted token evaluated according to signed provenance and revocation policy; privileged gates deny when untrusted |
| Старый токен без trusted provenance / устаревший authority policy | Privileged 403 or explicit re-issuance; **не** считать trust implied by role alone |
| `Role('admin')`, `Perm('...')` и platform operator получают claims foreign-only | Deny, no RPC on claims-only evaluation |
| `TenantScope` получает arbitrary `X-Tenant-Id`, нет signed grants или revoked membership | No foreign tenant access; revocation respected |
| `Or/Not` над indeterminate atom | Существующие truth tables сохраняются; indeterminate не становится самостоятельным основанием allow |

**Не смешивать:** логическое `Or(allowed, indeterminate)` разрешает на основании allowed-ветви — это уже существующая semantics, а не fail-open. Риск анализа — *какая ветвь реально trusted*, а не сама алгебра.

### 16.3 Tenant-kind/subject separate decision (B-R2)

- **Tenant identity / scope**: UUID + signed allowlist + RLS — Core.
- **Tenant kind**: если тип требуется как платформенный атрибут, источник/протокол утверждается на уровне composition, но Core **не** обращается к `tenancy.membership.v1` по дефолту. Альтернатива: kind целиком остаётся доменным Billing/потребителем через own contract, если платформенные инварианты не требуют kind.
- **Billing account target**: `Account.user() / .org() / .bound_tenant()` — Billing-owned; отсутствие determination `unknown/canonical` ⇒ indeterminate по прежней таблице; никакого implicit fallback к user account.
- **Perf:** `Account.bound_tenant()` не создаёт скрытый дополнительный membership RPC в co-located topology; в split count/preflight/caching должны быть задокументированы, без stale grant bypass. Compare c `shared/authz/account.py:tenant_kind()` на base.

**Решение ADR-C должно выбрать платформенный kind-port или domain-only account resolver экспериментально; оба одновременно не внедрять.**

## 17. Authorization portability, static safety и Public SDK contract

### 17.1 A19 и конкурирующие runtime layers

На `b8d8506` `register_live_adapter` допускает только два kinds — `entitlements`, `membership`, хранит один adapter на kind в process-global `_ADAPTERS`. Сторонний `inventory` не может добавить kind без Core, а registration второго adapter заменяет первого. При Option A один app runtime/process это не делает registry универсальным. **Предпочтительное решение:** после migration typed policies удалить специальные live adapters как путь общего расширения, оставив `Requirement` + `AccessFact` + existing contracts; до удаления доказать parity для обеих прежних atom families. Если минимальный compatibility adapter всё-таки нужен, это отдельное одобренное исключение по H-12, не новый business driver system.

### 17.2 Типизированные parameterized domain atoms — ADR-C experiment

Текущий `Entitled(fact_name, category, code, mode)` не принимает параметры наподобие `OrgRole(minimum)` или `Credits(amount, of=Account.org(...))`; `AccessFact` содержит claim/projection/live, но не универсальную params schema. Поэтому рассмотреть **ровно две** реализации, не обе в production:

- **B: consumer-owned `Requirement` wrappers**, generated from versioned declarative public contract, вызывают Core runtime/contract; stable `tier`, `contracts()`, `org_params()`, `risks()` и `of=`. Owner code не импортируется при runtime consumer.
- **A: минимальное расширение `AccessFact` параметрами**, типизированный generated wrapper, параметры передаются существующему fact resolver; предотвращены collisions, injection, serialization mismatch и unauthorized override of freshness/risk.

Тестовый сценарий: один внешний `inventory` fact + две стандартные политики `OrgRole(minimum='admin')` и `Credits(amount=2, of=Account.org(...))`; исполнить в monolith, split и consumer-only venv. Сравнить public signatures, error status 402/403/501/503, explicit subject `of=`, local DB/RPC counts, p95/p99, request ordering, type-checking, generated files и число ручных шагов. **Решение по фактам не должно подменить договорённость о Billing account semantics.**

### 17.3 Security rules и невозможность магического полного AST-proof

`module_rules.py` и `api10_rules.py` сегодня узнают строки `OrgRole`, `Subscribed`, `Credits`, `require_org_role` и т. п. Статический scanner не может доказать безопасность произвольного динамического Python-кода. Следовательно, нельзя обещать, что любые aliases/reflective calls будут автоматически правильно классифицированы.

**Заменяющая модель:** декларативная, versioned policy metadata в generated wrapper (`required_tenant_scope`, contract dependency, category of risk, freshness bound, possible remote I/O) + проверка `require()`/route admission на boot. Domain code **не может самовольно понизить платформенную risk classification**: Core uses signed/reviewed metadata and independent route effect tags; неизвестная dynamic policy в критичном маршруте — refusal или explicit reviewed escape hatch. Линтер должен обрабатывать известные import aliases и supported syntactic forms, а unsupported shapes выдавать диагностику, а не считать безопасными. Negative mutations: alias `OrgRole as X`, module-qualified call, wrapper re-export, dynamically built policy, custom `Requirement` raising, unregistered fact, competing owner.

**Порог безопасности:** ни один тестовый обход не превращает sensitive/admin/money route в маршрут без требуемой runtime защиты. Поскольку runtime enforcement не выводится автоматически из произвольного AST, критичные эффекты также защищаются независимо от синтаксической эвристики.

### 17.4 Публичный API и несовместимости

`ADR-0010` и `I-17/H-8` называют `sandloom.sdk` единственной платформенной публичной поверхностью. `generated contract stubs/DTO` уже являются допустимым исключением для межмодульного взаимодействия. **Явно дополнить ADR:** generated **policy** interface — такой же consumer-owned публичный API, но **не** новый namespace платформенного SDK.

Требуется в рамках **PR-08/10**:

1. Таблица `old import → new consumer import`, exact signatures/types, `__all__`, beginner tier; изменения в `public_api_baseline.json` только с review.
2. `Role`, `Perm`, `require`, `Requirement`, `Owner`, `Entitled`, `authorize` как platform API; `Subscribed`, `Credits`, `SubscriptionActive`, `Feature`, `WithinLimit`, `OrgRole`, named `Roles`/`Permissions` — module-owned. Исключения по идентичному имени внутри SDK не оставлять молча.
3. Отсутствие provider wheel не мешает import generated consumer API; обновление consumer snapshot — явная build-time операция.
4. H-9: no deprecated alias в Core; для pre-release breaking change описать codemod/upgrade steps, ошибки отсутствия с понятным кодом.
5. Проверить warnings/help/doc generator/agents, типизацию (mypy/pyright), public API snapshot и beginner budget; **не обещать прежний импорт** без фактического compatibility решения.

## 18. Runtime, versions, packaging, graph и operational guarantees

### 18.1 Contract/event artifact release matrix

| Конфигурация | Ожидаемая гарантия |
|---|---|
| New provider + old consumer, additive DTO/event field | Consumer продолжает получать прежнюю shape, unknown additive field не ломает декодирование |
| Old provider + new consumer, новый обязательный method | `/ready` сообщает incompatibility (когда declared required), call не даёт silent data/default; ожидаемый 501, а не 500 |
| No provider implementation in consumer wheel | Consumer-only `pip list` не содержит provider; его generated DTO, policy wrapper и RPC client импортируются |
| Two contract versions в одном процессе | Отдельные namespaces/types; нет collision |
| Old version-less Tenancy membership event | `apply/skip/refresh` сохраняет решение о том, когда перечитывать authoritative current state; replay не регрессирует version |
| One module updated, framework unchanged | `sandloom` wheel остаётся byte-identical, no forced framework release |
| Generator upgraded, schema unchanged | Детерминизм внутри одной версии генератора; justified diff между версиями с совместимостью и regeneration step |

В `C5` необходимо сохранить old version-less event → refresh, order/duplicate/late event semantics. Нельзя удалить compatibility reducer просто потому, что `shared` должен очиститься. Move it to consumer/owner-owned helper and test rolling upgrade.

### 18.2 Packaging / all 16 subsets

`I-19` требует не усреднённого smoke, а матрицы `2^4 = 16` combinations Identity/Tenancy/Billing/Admin, плюс explicit required consumed contract errors и infrastructure prerequisites (Postgres, Redis, Celery) из **реально объявленных** зависимостей. Для каждого subset фиксировать `install → import → create_app → startup/readiness → CLI`. Если subset не является допустимым по продуктовой модели, только owner-approved exception с доказанным dependency reason — не «CI проверяет только supported». Отдельно проверить Billing-only без Celery/без Tenancy, как в baseline §13.2; нужен либо lazy taskqueue integration, либо объявленное требование capability/extras без silent import error. Каждая установка делается в clean venv из собранных wheels.

### 18.3 `sandloom graph` и наблюдаемость ownership

Граф показывает не только `Module → Module` contracts/events, но и **обобщённый узел Core** и публичные механизмы, через которые Core использует declarations: claims authorities, tenant source, generated contract ownership, module settings/CLI contributions, privileged operation owners и dynamic bindings. При этом graph может показывать **approved runtime/composition edges**, а не ложные `Core imports Billing` для generic registry. Каждое ребро с полями `type`, `owner`, `source`, `validation`, `runtime/packaging`, `approved`. `freshness/source_changed` следует проверять по runtime manifest/snapshot; stale graph не считается доказательством отсутствия edge. Проверить граф в empty/standard/third-party/split topology и сохранить до/после diff; не увеличивать количество ложных доменных связей.

### 18.4 Tests/benchmarks as release gates

- `uv run pytest -m guarantees src` (существующая проверка ledger), `sandloom test --full`, existing split conformance script — все тесты и статические gates не ослаблены и не удалены без owner approval.
- Новые CI suites, имена **предлагаются**, не утверждены в репозитории: `test_claim_authority_matrix`, `test_policy_portability_consumer_only`, `test_module_settings_external`, `test_wheel_owner_artifacts`, `test_arch_boundary_mutations`, `test_graph_kernel_edges`, `test_module_subset_wheels`, `test_mixed_versions`.
- Сравнить monotonic event state/ordering, UoW, idempotency, tenant binding, fail-closed, RPC errors; test matrix против same SHA baseline и candidate.
- Perf: same hardware/runtime/backends; instructions/request/write с текущим +1% budget или явным исключением; добавить authz/mint instructions, p50/p95/p99, `DB queries/request`, `RPC calls/request`, memory/startup и DX concept/file count. **Ноль новых remote calls** для unchanged co-located topology и unchanged freshness; `Role`/`Perm` — no I/O.
- CI проверяет не только отсутствие hardcoded strings, но и способность поймать deliberately injected invalid import/role/contract/migration rule; compiled docs/examples не считаются нарушением runtime boundary без evidence.

## 19. Зависимости PR и go/no-go критерии

| Gate | Must pass before | Tests / decision |
|---|---|---|
| G0: approved assessment | PR-01 structural, PR-04/06/07 design freeze | 46 edges mapped; Q1–Q12 signed; I/H revisions; ADR-A..D proposed; one SHA |
| G1: JWT security authority | Release privileged gate change; PR-08 rollout | foreign-only, spoof owner, legacy token, mint/verify split, no Role/Perm RPC |
| G2: trustworthy architecture checks | PR-05 deletion of framework artifacts | synthetic negative mutations; wheel checks, no false positives for generic loader |
| G3: consumer isolation PoC | PR-05 moving artifacts | consumer-only clean venv, generated types, mixed versions, no provider import |
| G4: validated config bootstrap | Removing old `ModulesConfig` | all entrypoints, secret redaction, env stability, no import-time I/O |
| G5: portable typed authz PoC | PR-08 domain atoms migration | authz algebra law suite, 402/403/501/503, `of=`, B-R2 and A19 solved, perf/DX baseline |
| G6: public API review | Removal of old atoms from SDK | ADR-0010 amendment, snapshot, beginner tier, migration script, docs, H-9 respected |
| G7: release readiness | PR-10 acceptance/tag | every owner invariant passed or approved exception, 16 subset matrix, full ledger, graph, perf/DX, rolling upgrade |

**Критический путь:** PR-00 → (PR-01 security) и (PR-02 guards) → PR-04 consumer-only → PR-07 authz PoC → PR-08 public authz migration → PR-09 CLI/shared cleanup → PR-10 release. PR-03 packaging и PR-06 settings можно вести независимо при сохранении G0. Нельзя удалять domain artifact defaults до G3, domain atoms до G5/G6 или trusted admin gating до G1.

### 19.1 Definition of Done одного PR

- Указаны `edge_ids`, `invariants`, `guarantees`, named scenarios и planned downgrade/upgrade effect; явно список `new runtime mechanisms` с доказательством необходимости.
- Архитектурный diff на pinned SHA: owner/fact/DTO/setting/contract/import graph до и после. Для иных веток redo baseline.
- Существующие tests, contract compatibility, `sandloom test --full`, guarantees/CI по применимости; новый негативный тест для каждой запрещённой конструкции; проверка static test не подменяет runtime.
- Весь новый API documented (`sandloom explain`, errors, usage), generated files не редактируются вручную; no hidden import of another module.
- Benchmarks при изменении hot path; A/B PoC до выбора дополнительного механизма. No UX regression ignored because release is prerelease.
- Все baseline edges затронутые PR имеют машинно проверяемый статус `fixed` или `retained-approved` со ссылкой на ADR; **никакой «чистоты» только по grep**.

## 20. Открытые технические решения, которые нельзя выдавать за завершённые

| Решение | Кто подтверждает | Обязательное доказательство |
|---|---|---|
| Policy portability: parameterized AccessFact vs consumer Requirement wrapper | ADR-C / owner | mono/split/consumer-only, typed signature, DX/perf |
| Tenant-kind resolution: platform trusted kind vs domain-only Billing resolver | ADR-C / owner | `Account.bound_tenant`, unknown/canonical, same topology no extra RPC |
| Authority verification: runtime claim-map, binding identity, legacy token transition | Security ADR / owner | full mint+HTTP+split negative tests, revocation/key rotation |
| Settings discovery: module declaration before config validation without import side effects | ADR-D / owner | app/worker/migrate/CLI parity, secret and risk rules |
| First-party starter catalog and recipes | ADR-A/CLI owner | no runtime kernel coupling, excellent DX for third-party module |
| Public API breaking release and replacement imports | ADR-0010 amendment / owner | generated consumer API + no provider implementation, SDK snapshot/codemod |

**Release rule:** никаких окончательных «done» по архитектуре, пока 46 edges не получили статусы на одном целевом SHA, а все 19 invariants не получили воспроизводимые evidence/approved exception. Проект может принять частичную поставку отдельных PR (особенно P0 security) до завершения общей миграции; это не считается архитектурой, доведённой до конца.


## 21. Финальный cross-check: все 19 инвариантов и 52 quality scenarios

Этот раздел закрывает организационный пробел: нельзя сказать «все требования учтены», если PR map охватывает только edges. Ниже — **маршрутизация проверок**, не утверждение, что они уже прошли. Источник формулировок — `requirements.md` на SHA `b8d8506`; любые approved changes из §15 сначала вносятся в requirements.

### 21.1 Карта I-01…I-19 → PR и доказательство

| Invariant | Responsible stage | Минимальное доказательство |
|---|---|---|
| `I-01` | PR-03/10 | A-H1; lean wheel; CLI, health/readiness |
| `I-02` | PR-02 | A-F1; dynamic loader allowlist |
| `I-03` | PR-00/02/09 | A-H4; 46-edge ledger and mutant checks |
| `I-04` | PR-02/04 | Module→module import prohibition; packaging test |
| `I-05` | PR-04 | B-H2; no provider implementation in consumer venv |
| `I-06` | PR-04/07/10 | B-B1; mono/split contract and authz semantics |
| `I-07` | PR-07/08 | C-H1; Or/Not/indeterminate law tests |
| `I-08` | PR-01 | A-F2/F3; preapproved claim authorities |
| `I-09` | PR-01 | A-B3; claims-only Role/Perm provenance |
| `I-10` | PR-01/08 | TenantScope signed allowlist and revocation |
| `I-11` | PR-04/05 | B-H1/B-H4; independent artifacts and wheel digests |
| `I-12` | PR-04/10 | B-B3/B-B4; mixed-version RPC/ready |
| `I-13` | PR-02/06/07/09 | A-H3; synthetic third-party module; no Core edits |
| `I-14` | PR-07/10 | Request/write instructions; authz/mint latency and I/O |
| `I-15` | PR-04/05 | B-H3; hashseed/module order, stale file refusal |
| `I-16` | PR-10 (each PR) | Guarantee ledger retained with independent tests |
| `I-17` | PR-08/10 | SDK imports side-effect free; API snapshot/ABI review |
| `I-18` | PR-06 | D-H1..D-F4; config every entrypoint |
| `I-19` | PR-03/10 | All 16 module subsets; allowed binding failures |

### 21.2 Все 52 сценария §5 `requirements.md` распределены по PR

Каждая ячейка сценария означает **«создать/сохранить тест в указанном PR»**, а не «тест прошёл». В колонке `Final gate` для всех сценариев подразумевается PR-10 (conformance release). Обязательные выходы (HTTP codes, readiness, type identity, no I/O, no Core diffs) берутся из **оригинального текста** сценария в `requirements.md`, а не сокращаются до обозначения PR.

| Scenario | Implement / verify in PR | Scenario | Implement / verify in PR |
|---|---|---|---|
| `A-H1` | PR-02, PR-03 | `A-H2` | PR-10 |
| `A-H3` | PR-02, PR-06, PR-07 | `A-H4` | PR-02 |
| `A-B1` | PR-01, PR-10 | `A-B2` | PR-09 |
| `A-B3` | PR-01 | `A-B4` | PR-04, PR-10 |
| `A-B5` | PR-02, PR-04, PR-10 | `A-F1` | PR-02 |
| `A-F2` | PR-01 | `A-F3` | PR-01 |
| `A-F4` | PR-01 | `B-H1` | PR-04, PR-05 |
| `B-H2` | PR-04, PR-05 | `B-H3` | PR-04 |
| `B-H4` | PR-05 | `B-B1` | PR-04, PR-10 |
| `B-B2` | PR-04 | `B-B3` | PR-04 |
| `B-B4` | PR-04 | `B-B5` | PR-04 |
| `B-F1` | PR-04 | `B-F2` | PR-04 |
| `B-F3` | PR-04 | `B-F4` | PR-04 |
| `C-H1` | PR-07, PR-08 | `C-H2` | PR-07, PR-08 |
| `C-H3` | PR-07, PR-08 | `C-H4` | PR-07, PR-08 |
| `C-B1` | PR-07, PR-08 | `C-B2` | PR-07 |
| `C-B3` | PR-01, PR-07 | `C-B4` | PR-07 |
| `C-B5` | PR-07 | `C-F1` | PR-07 |
| `C-F2` | PR-07 | `C-F3` | PR-07, PR-08 |
| `C-F4` | PR-01, PR-07 | `D-H1` | PR-06 |
| `D-H2` | PR-06 | `D-H3` | PR-06 |
| `D-H4` | PR-06 | `D-B1` | PR-06 |
| `D-B2` | PR-06 | `D-B3` | PR-06 |
| `D-B4` | PR-06 | `D-B5` | PR-06, PR-09 |
| `D-F1` | PR-06 | `D-F2` | PR-06 |
| `D-F3` | PR-06 | `D-F4` | PR-06 |


**Проверка покрытия файла:** 46 unique baseline edges (в §14) + 19 invariants + 52 scenario IDs (в §21) + 12 owner decisions (в §15). Это административно полная **coverage map исходного baseline/requirements**, но не доказательство полноты анализа всех будущих изменений или корректности implementation. PR-00 должен добавить новые выявленные edges/scenarios по актуальному SHA, а не объявлять этот набор неизменяемой истиной.
