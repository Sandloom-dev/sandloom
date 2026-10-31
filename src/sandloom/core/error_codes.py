"""Stable error codes: one catalogue for checks, boot refusals and runtime errors.

Every framework diagnostic an author can hit carries a stable code, so a
message, a test failure and ``sandloom explain <code>`` all point at the same
entry. The entry holds the long form (what happened, why it matters, how to
fix it); the message at the failure site stays short::

    error[SANDLOOM-B002]: route POST /api/v1/notes resolves the guard before auth
      --> src/sandloom/notes/api/notes.py:42 (create_note)
      fix: move Depends(get_current_user_id) before the guard
      docs: https://sandloom.dev/docs/reference/error-codes/#sandloom-b002
      help: sandloom explain SANDLOOM-B002

Code families: ``SANDLOOM-C`` static ``sandloom check`` rules (also addressable by the
rule id, e.g. ``sandloom explain tenant-scoped-models``), ``SANDLOOM-B`` startup
refusals (``create_app``, ``sandloom check --boot``), ``SANDLOOM-R`` runtime errors,
``SANDLOOM-T`` test-harness and snapshot gates. Codes are never reused or
renumbered; a retired code stays listed with ``retired=True``.

Pure data + formatting: imports nothing from the kernel, so every layer
(configuration excepted) may use it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Kind = Literal["check", "boot", "runtime", "test"]

_CODE_RE = re.compile(r"^SANDLOOM-[CBRT]\d{3}$")


@dataclass(frozen=True)
class ErrorCode:
    """One catalogue entry (see the module docstring)."""

    code: str
    slug: str
    kind: Kind
    title: str
    fix: str
    explain: str
    docs: str | None = None
    retired: bool = False
    #: Set while nothing raises the code yet: the hand-off, owner and spec
    #: section of the check that will (``"H1 (WS-A): spec §4.4"``). The code
    #: is reserved and documented now (Public API 1.0, spec Appendix A); the
    #: field is cleared in the change that implements the check.
    planned: str | None = None


def _entry(
    code: str,
    slug: str,
    kind: Kind,
    title: str,
    fix: str,
    explain: str,
    docs: str | None = None,
) -> ErrorCode:
    return ErrorCode(code, slug, kind, title, fix, explain.strip(), docs)


_API10_SPEC = "tests/fixtures/specs/public-api-1.0-spec.md"


def _planned(
    code: str,
    slug: str,
    kind: Kind,
    title: str,
    fix: str,
    explain: str,
    *,
    lands: str,
    docs: str = _API10_SPEC,
) -> ErrorCode:
    """A reserved code whose check lands later (``lands``: hand-off, owner,
    spec section)."""
    return ErrorCode(code, slug, kind, title, fix, explain.strip(), docs, planned=lands)


def _landed(
    code: str,
    slug: str,
    kind: Kind,
    title: str,
    fix: str,
    explain: str,
    *,
    docs: str = _API10_SPEC,
) -> ErrorCode:
    """A Public API 1.0 code whose check raises it (the ``planned`` field of
    :func:`_planned` cleared when the check landed)."""
    return ErrorCode(code, slug, kind, title, fix, explain.strip(), docs)


CATALOGUE: tuple[ErrorCode, ...] = (
    _entry(
        "SANDLOOM-T015",
        "inspection-inputs",
        "test",
        "module context or declared architecture inspection cannot use its inputs",
        "read the diagnostic kind; select valid inputs, adjust the budget "
        "or explicitly run sandloom generate",
        "Context and graph read a generated index without booting the app. "
        "Missing/changed layouts, unsupported schemas, unsafe files, "
        "incomplete wire definitions or mandatory content over budget fail "
        "without partial output. JSON clients use typed diagnostic kinds. "
        "Exit 3 means strict source verification is unavailable. "
        "Generation explicitly writes files; review its diff. "
        "A verified source basis is not live topology.",
        docs="docs/guides/module-context.md",
    ),
    # ------------------------------------------------------------------ checks
    _entry(
        "SANDLOOM-C001",
        "module-boundaries",
        "check",
        "a business module imports a sibling business module",
        "talk through a typed event (an sandloom.sdk.events Event subclass, "
        "published with publish_event and consumed with @on(...)) or a typed "
        "contract (Contract(X_Y_V1) from sandloom.sdk.contracts); never import the "
        "other module",
        """
Modules are atomic and isolated: each one can be deployed as its own service.
An import of another business module (any spelling: absolute, relative,
importlib) couples the two at the code level, so the second module would have
to ship in every process that loads the first.

Fix: publish an Event subclass and react to it with an @on(EventClass)
consumer in the other module, or call its public sync contract through
Contract(X_Y_V1) (the generated reference in sandloom.shared.contracts); the
module-level reference declares the dependency (consumes= is derived).
""",
        "AGENTS.md#the-one-rule-that-matters",
    ),
    _entry(
        "SANDLOOM-C002",
        "framework-imports",
        "check",
        "import outside the framework public API",
        (
            "import only sandloom.sdk.* (the public API), "
            "sandloom.shared.* and your own package"
        ),
        """
A business module may import only the public API (sandloom.sdk.*: web, data,
events, contracts, authz, testing, ...), sandloom.shared.* and its own package.
Importing a kernel module (sandloom.core.*) couples the module to code that
changes without notice; every name a module needs is re-exported by sandloom.sdk.
""",
    ),
    _entry(
        "SANDLOOM-C003",
        "raw-provider",
        "check",
        "provider escape hatch (raw_client / raw_backend / raw) in module code",
        "use the portable capability surface (cache.get/set, taskqueue.enqueue, ...)",
        """
raw_client() / raw_backend() hand out the backend SDK object. Module code that
uses it only runs on that backend, which defeats the pluggable infrastructure
layer. Use the operations of the capability contract instead.
""",
    ),
    _entry(
        "SANDLOOM-C004",
        "infra-imports",
        "check",
        "infra-internal module, legacy global or backend SDK imported",
        "use DbSession / Database(name) / di.cache() / publish_event / Capability",
        """
Modules know infrastructure only by logical name. Backend SDKs (boto3, motor,
neo4j, ...), the legacy globals and the infra-internal packages are blocked;
the drivers construct every backend. `sandloom.sdk.drivers` is public for driver
packages and blocked in modules: backend code belongs in a separate package.
""",
    ),
    _entry(
        "SANDLOOM-C005",
        "kernel-handles",
        "check",
        "kernel handle reached by attribute in module code",
        "use the public verbs of sandloom.sdk: Event.publish() / @on(E) for events, "
        "cache() / objectstore() / capability() from sandloom.sdk.infra, and "
        "Contract(REF) for contracts",
        """
core.event_bus, core.infra, core.service_registry and request.app.state.core
are kernel internals. A module reaches kernel services only through their
public verbs.
""",
    ),
    _entry(
        "SANDLOOM-C006",
        "uow-boundary",
        "check",
        "db.commit() / db.rollback() in module code",
        "remove the call: the Unit of Work commits on success and rolls back on "
        "any exception; use flush() and begin_nested()",
        """
DbSession is one transaction per request (or consumer delivery / task). The
outbox, the audit row and the on-commit hooks commit with it. A manual commit
splits that transaction: an event could be published for a change that is
later rolled back.
""",
    ),
    _entry(
        "SANDLOOM-C007",
        "best-effort-effects",
        "check",
        "publish_event / record_audit downgraded to best-effort without a marker",
        "drop required=False and run inside a Unit of Work, or justify it with "
        "`# sandloom: best-effort <reason>` on the call",
        """
required=False publishes straight to the bus (no outbox, no retry) or skips
the audit row when no UoW is active. That is a deliberate weakening, so it
must be visible in review.
""",
    ),
    _entry(
        "SANDLOOM-C008",
        "get-core-await",
        "check",
        "get_core(request) called without await",
        "`await get_core(request)` or the sync core_from_request(request)",
        "get_core is async since 0.6.0; a missing await yields a coroutine.",
    ),
    _entry(
        "SANDLOOM-C009",
        "endpoints-outside-api",
        "check",
        "HTTP endpoint outside the module's api/ layer",
        "move the route into the module's api/ package",
        "define_module mounts api/'s router; endpoints elsewhere are not mounted "
        "by convention and escape the api-layer rules.",
    ),
    _entry(
        "SANDLOOM-C010",
        "orm-leakage",
        "check",
        "ORM model / SQLAlchemy in an endpoint file",
        "map to pydantic DTOs in services and keep persistence out of api/",
        "The api/ layer stays persistence-free: endpoints take DbSession, call "
        "services and return schemas/ DTOs.",
    ),
    _entry(
        "SANDLOOM-C011",
        "schema-orm-imports",
        "check",
        "schemas/ imports ORM models or SQLAlchemy",
        "keep DTOs persistence-free",
        "schemas/ are the module's wire contracts; they must not depend on "
        "the persistence layer.",
    ),
    _entry(
        "SANDLOOM-C012",
        "contract-dtos",
        "check",
        "a contract DTO the generator cannot copy, or a hand-edited / stale "
        "generated copy",
        "declare the DTO at module level in the provider's own package (its "
        "schemas module) inside the closed set of wire shapes, then run "
        "`sandloom generate`",
        """
A provider authors its contract DTOs (pydantic models, Enums) in its own
module, by convention its schemas module. `sandloom generate` writes a
wire-shape copy of every DTO a provided contract signature names (nested ones
included) into the shared DTO package, next to the stub, which names the
copy: every caller process imports the same generated class, and a call
returns it locally and over RPC alike. A copy carries the fields, their types
and constraints, defaults, aliases, Literal / Enum values, nested models and
the model_config options; not methods, properties or validators (a validator
is the provider's: it runs where the provider binds an argument or builds
its result).

Refused, with the field path and the fix: a DTO outside the closed set of
wire shapes (the event set: see events.md, What an event field may be) or
one the copy cannot carry (a parametrised generic, a default or
default_factory it cannot write, a custom constraint, a callable
discriminator) when @contract decorates the provider; a DTO of ANOTHER
module's package or a function-local one when the module registers the
contract (use the other module's generated copy from the shared DTO
package); a missing copy (`sandloom check`, contract-dtos); a hand-edited or
stale copy (`sandloom test`: run `sandloom generate`).
""",
    ),
    _entry(
        "SANDLOOM-C013",
        "test-imports",
        "check",
        "module test imports outside its whitelist",
        "use sandloom.sdk.testing (the client fixture, as_user, deliver, published, "
        "fake_contract, fake_access_fact, ...) and other modules' HTTP API / "
        "contracts / events",
        """
Module tests import only the public API (sandloom.sdk.*, the test kit
sandloom.sdk.testing included), sandloom.shared and their own package. Seed another
module's state through its HTTP API, contracts or events.
""",
    ),
    _entry(
        "SANDLOOM-C014",
        "tenant-scoped-models",
        "check",
        "a per-tenant table is not TenantScoped",
        "declare `class X(TenantScoped, Base)` (drop the own tenant column) and "
        "ship enable_tenant_rls(op, <table>), or list a deliberate global table "
        "under tenant_scope_exempt in architecture_rules.yml with a reason",
        """
A model or Core Table with a tenant-like column (tenant_id, organization_id,
org_id, workspace_id, account_id) holds per-tenant rows. Without TenantScoped
the ORM tenant guard never filters it, so one forgotten
where(organization_id == ...) leaks another tenant's rows, and RLS is never
enabled for it.

Tables that are deliberately keyed by organisation but global to the system
(the tenancy module's own organisations/memberships, billing's customer
records) are listed under tenant_scope_exempt with a written reason. A stale
exemption (no matching table any more) also fails.
""",
        "docs/reference/security-model.md",
    ),
    _entry(
        "SANDLOOM-C015",
        "unsafe-tenant-sql",
        "check",
        "runtime raw SQL / Core table access can bypass the tenant guard",
        "use ORM entities, or an explicit unscoped() block for trusted system work",
        "text(), exec_driver_sql and Model.__table__ statements are not "
        "tenant-filtered by the ORM guard.",
    ),
    ErrorCode(
        "SANDLOOM-C016",
        "route-operation-class",
        "check",
        "a route declares no ADR-0003 operation class (retired)",
        "none: the rule was dropped before release",
        "Retired. A static rule for ADR-0003 operation classes was planned and "
        "dropped: the route-policy model that replaces operation classes "
        "(deny-by-default auth, explicit public(reason=...), risk tags on the "
        "owning effect) gets its own checks. The code is never reused.",
        retired=True,
    ),
    _entry(
        "SANDLOOM-C017",
        "event-schema-channels",
        "check",
        "publish_event names a channel the module registers no schema for",
        "declare the event as an Event subclass in the module's events.py "
        "(its channel and schema are derived), or fix the channel name",
        """
Every versioned channel needs a registered schema in every profile: the event
is refused before it reaches the outbox otherwise. An sandloom.sdk.events Event
subclass carries both; the static rule flags a channel spelled as a string
(or module-level constant) that no Event class or schema of the module
declares. Channels owned by another module are published by that module
only.
""",
        "docs/reference/event-contracts.md",
    ),
    _entry(
        "SANDLOOM-C018",
        "guard-order",
        "check",
        "a guard or tenant-dependent gate is declared before what it needs",
        "list the authentication (and TenantScope()) before idempotency_guard / "
        "require(...) gates, or put auth in the router dependencies",
        """
FastAPI solves a route's dependencies in declaration order: route-decorator
dependencies=[...] first, then the endpoint parameters. An idempotency_guard
that runs before authentication keys anonymous retries; an authz gate that
needs an organisation (SubscriptionActive, Credits, OrgRole, ...) fails with
"organization context required" when it runs before TenantScope(). This rule
flags the statically visible cases; create_app refuses the rest at startup
(SANDLOOM-B002, SANDLOOM-B007).
""",
    ),
    _entry(
        "SANDLOOM-C019",
        "consumed-contracts",
        "check",
        "get_service names a contract the module does not declare in consumes=",
        "call the contract through Contract(REF) (its requirement is derived), "
        'or add ContractRequirement("<name>", required=...) to '
        "define_module(..., consumes=[...])",
        """
consumes= is the module's dependency manifest. The loader checks each entry
at startup (provided locally or bound in SERVICE_BINDINGS); an undeclared
service(...) / get_service call is only discovered as a 501 at request time in
the deployment that lacks the provider.
""",
    ),
    _entry(
        "SANDLOOM-C020",
        "db-dependency-scope",
        "check",
        'Depends(get_db) without scope="function"',
        "use db() from sandloom.sdk.data (the lazy request unit) or DbSession from "
        'sandloom.sdk.web; a raw Depends(get_db) needs scope="function"',
        """
Without scope="function" FastAPI keeps the dependency alive until the response
is sent: it opens a second Unit of Work and commits it after the response, so
a client can act on a 200 before the write is visible.
""",
    ),
    # Public API 1.0 checks (spec Appendix A): reserved, implemented at H2.
    _landed(
        "SANDLOOM-C021",
        "module-local-imports",
        "check",
        "a function-local import in a business module, example or template",
        "import at module level; a genuinely optional dependency keeps its "
        "local import with `# sandloom: optional-import <reason>`; typing-only "
        "imports go under `if TYPE_CHECKING:`",
        """
Function-local imports in modules hide dependencies and get copied by agents.
Their causes are gone in 1.0: import-time settings are lazy (spec §2.4) and
discovery replaces lazy setup imports. What remains is a marked optional
dependency (for example qrcode).
""",
    ),
    _landed(
        "SANDLOOM-C022",
        "untagged-sensitive",
        "check",
        "an effect that looks sensitive carries no risk tag (warning)",
        "tag the owning effect with its risk (money / admin / security) at "
        "the source, or declare sensitive(risk) on the route",
        """
ADR-0009 derives a route's sensitivity from the risk of the effects it
reaches. An effect that moves money, changes roles or touches credentials
without a risk tag makes its routes look ordinary; the check warns.

The route form is a heuristic: a write route that raises 403 in its body looks
like a hand-written authority decision. If the refusal is one, declare the
risk, or better, move it to a `require(...)` policy. If it is a business rule,
such as a used-up quota, it is not an authority decision: answer with a status
that says so (409 or 422), and the warning goes away.
""",
    ),
    _landed(
        "SANDLOOM-C023",
        "multi-store-waivers",
        "check",
        "lists every allow_multi_db_write opt-out for review",
        "keep each waiver's reason current; remove a waiver whose route no "
        "longer writes to a second store",
        """
allow_multi_db_write(secondaries=, reason=) is the reviewed best-effort 1PC
opt-out of the one-writer rule (spec §4.4 rule 4). The check lists every
waiver with its reason so a review sees all of them in one place.
""",
    ),
    _landed(
        "SANDLOOM-C024",
        "unsafe-tenant-query",
        "check",
        "a raw query on a non-relational store outside unscoped()",
        "use the store's tenant-scoped operations, or run deliberate "
        "cross-tenant work inside `with unscoped():`",
        """
Non-relational stores have no row-level security: a raw query there reads
every tenant's data. The check flags raw queries outside an explicit
unscoped() block (the relational counterpart is SANDLOOM-C015).
""",
    ),
    _landed(
        "SANDLOOM-C025",
        "event-view-compat",
        "check",
        "an event view is incompatible with (or unverified against) its "
        "producer, or a view calls .publish()",
        "make the view's fields a compatible subset of the producer schema; "
        "only the producing module publishes a channel",
        """
A view (an Event class for a foreign channel) is checked against the producer
schema found in the manifest or the exported schema file; with neither it is
reported as unverified (warning). .publish() on a view is refused: only the
producer owns a channel.
""",
    ),
    _landed(
        "SANDLOOM-C026",
        "contract-union-hint",
        "check",
        "a contract signature uses an ambiguous union (information)",
        "optional: use a discriminated type so the fully typed codec 2 applies; "
        "the union keeps working through codec 1",
        """
Ambiguous unions such as UUID | str stay supported (codec 1, tagged); this
hint only points at the faster fully typed codec (owner decision Q3).
""",
    ),
    _landed(
        "SANDLOOM-C027",
        "account-route-mismatch",
        "check",
        "a route with an organization parameter gates billing with of=Account.user()",
        "use of=Account.org(param=...) for an organization route, or state "
        'Account.user(reason="...") when the personal account is intended',
        """
A route under /orgs/{org_id} that checks the caller's personal subscription
or credits almost always meant the organization's account. The check refuses
the combination unless the atom states why (spec §7.3).
""",
    ),
    # ------------------------------------------------------------- boot time
    _entry(
        "SANDLOOM-B001",
        "module-load-failed",
        "boot",
        "a module failed to load",
        "fix the error shown for the module; a module that may legitimately "
        "be absent declares define_module(..., optional=True)",
        """
Modules are required by default: a module that fails to import or to
register aborts startup, so its routes can never silently disappear (a test
would otherwise see 404 on its own endpoints). Optionality is explicit:
define_module(__name__, ..., optional=True), or a composition root's
default_optional_modules.
""",
    ),
    _entry(
        "SANDLOOM-B002",
        "guard-order-startup",
        "boot",
        "idempotency_guard resolves before the route's authentication",
        "declare the authenticator (CurrentUser / current_user_id from "
        "sandloom.sdk.web) before the guard, or authenticate the whole router "
        "(module_router)",
        "See SANDLOOM-C018. create_app checks the solved dependency order "
        "(GuardOrderError).",
    ),
    ErrorCode(
        "SANDLOOM-B003",
        "financial-guard",
        "boot",
        "a class F route used a response-storing or fail-open guard (retired)",
        "none: operation classes were removed (ADR-0009)",
        'Retired with the operation classes. A money route (sensitive("money")) '
        "with a response-storing or fail-open guard is refused as SANDLOOM-B019, and "
        "every money effect needs a dedupe record (SANDLOOM-R009) or the client's "
        "Idempotency-Key (SANDLOOM-R010). The code is never reused.",
        retired=True,
    ),
    ErrorCode(
        "SANDLOOM-B004",
        "operation-class-contradiction",
        "boot",
        "a route's operation-class declarations contradicted each other (retired)",
        "none: operation classes were removed (ADR-0009)",
        "Retired with the operation classes: the route policy is derived "
        "(deny-by-default auth, SANDLOOM-B008; risk at its source) and pinned by the "
        "route snapshot (SANDLOOM-T001). The code is never reused.",
        retired=True,
    ),
    _entry(
        "SANDLOOM-B005",
        "contract-binding",
        "boot",
        "a required consumed contract is neither provided nor bound",
        "load the providing module in this process, bind the contract in "
        "SERVICE_BINDINGS, or reference it as Contract(X_Y_V1, optional=True) "
        "and degrade",
        "The loader checks every contract a module references (its derived "
        "consumes=) at startup (ContractBindingError).",
    ),
    _entry(
        "SANDLOOM-B006",
        "module-registration",
        "boot",
        "a module's registration is invalid",
        "fix the reported registration (contract members must be "
        "@contract_method with wire-typed signatures and no session parameter; "
        "service / channel names versioned .vN; no duplicate registrations)",
        """
Module registration is transactional: the whole draft is validated before
anything is applied (ModuleRegistrationConflict, ContractTypeError,
ServiceRegistryError). The module then fails to load (SANDLOOM-B001).
""",
    ),
    _entry(
        "SANDLOOM-B007",
        "gate-order-startup",
        "boot",
        "an authz gate that needs an organisation runs before TenantScope()",
        "declare TenantScope() before the gate: move require(...) from the "
        "decorator dependencies into the endpoint signature after the "
        "TenantScope() parameter, or put TenantScope() in the router dependencies",
        """
Gates such as SubscriptionActive, Credits, Entitled and OrgRole resolve the
organisation from the bound tenant. FastAPI solves decorator dependencies
before endpoint parameters, so `dependencies=[Depends(require(...))]` next to
a TenantScope() parameter runs the gate with no tenant bound and every call
fails with "organization context required".
""",
    ),
    # Public API 1.0 boot refusals (spec Appendix A): reserved.
    _landed(
        "SANDLOOM-B008",
        "route-policy",
        "boot",
        "a route is neither authenticated nor public(), is both, or gives no reason",
        "authenticate the route (module_router() does it, or CurrentUser / "
        'CurrentService), or declare public(reason="..."); never both',
        """
Model E (ADR-0009) denies by default: every route is authenticated unless it
says public(reason=...). A route with both an authenticator and public(), or
public() without a reason, is ambiguous and refused at startup.
""",
    ),
    _landed(
        "SANDLOOM-B009",
        "write-store-ambiguous",
        "boot",
        "a route injects several writable sessions without writes() or an opt-out",
        'declare writes("<database>") on the route, or '
        "allow_multi_db_write(secondaries=..., reason=...)",
        """
The write store is inferred statically: DbSession / db() means the module's
primary database, one Database("x") means x. With several writable sessions
the route must name its write store, which also holds its idempotency,
audit and outbox records (spec §6.1).
""",
    ),
    _entry(
        "SANDLOOM-B010",
        "guarantee-unsupported",
        "boot",
        "the write store lacks a feature the route defaults need",
        "configure a write store whose driver provides the feature, or opt the "
        "route out of the default that needs it",
        """
The default route guarantees (durable idempotency, audit, outbox) need
features of the write store; a store without them cannot hold the records in
the same transaction, so startup refuses instead of a request failing later.
""",
    ),
    _entry(
        "SANDLOOM-B011",
        "underived-store-write",
        "boot",
        "a non-transactional store is injected into a route with defaults",
        "write through the route's write store and project into the other "
        "store, or declare the reviewed opt-out",
        """
A store without transactions cannot commit with the request unit, so its
writes would carry none of the route's derived guarantees.
""",
    ),
    _landed(
        "SANDLOOM-B012",
        "projection-contract",
        "boot",
        "a projection breaks its convergence contract",
        "give the target the conditional_write feature, start the key with "
        "tenant_id, let one source group own each column, and use delta "
        "mappers only for an in-database projection under the durable inbox",
        """
Projections converge only under the model of spec §6.4: per-aggregate
versions, one owning source group per column, full-state events, and delta
events only where the durable inbox makes them exactly once.
""",
    ),
    _landed(
        "SANDLOOM-B013",
        "multi-store-waiver",
        "boot",
        "an invalid allow_multi_db_write declaration",
        "list only secondary databases the route actually writes, never the "
        "write store itself, and give a reason",
        """
allow_multi_db_write is the reviewed best-effort 1PC opt-out (spec §4.4
rule 4): secondaries commit first and the write store last.
""",
    ),
    _landed(
        "SANDLOOM-B014",
        "money-effect-outside-write-store",
        "boot",
        "a money effect sits in an opted-out secondary store",
        "move the money effect into the route's write store",
        """
An opted-out secondary has no atomicity and no loss guarantee; a money effect
there could commit without its dedupe record or be lost.
""",
    ),
    _landed(
        "SANDLOOM-B015",
        "event-definition",
        "boot",
        "an invalid Event class",
        "use a versioned channel (<module>.<event>.vN), fields from the closed "
        "set of wire shapes (the error names the field path and the allowed "
        "alternative), one producer class per channel, defined in the "
        "producing module; rename a field called channel, meta, publish or "
        "wire_schema, or keep the wire key with an alias: via: str = "
        'Field(alias="channel")',
        """
Event classes are checked when they are defined and registered at startup:
the channel must be versioned, a channel has one producer and the producer
lives in the module that owns the channel (spec §5.2).
An event payload is plain data from a closed set of wire shapes, checked at
every depth when the class is defined: str, int, finite float, bool, None,
UUID, datetime, date, time, timedelta, Decimal, an Enum, a Literal, Any (an
opaque JSON value); X | None; unions of Literal / Enum members none of which
reads another's published value; list, tuple, set / frozenset of scalars and
dict[str, ...]; pydantic BaseModel DTOs; unions of DTOs only discriminated by
a Literal tag field (Field(discriminator="kind")). Refused, with the field
path and the fix: a union of scalars whose members read the same value
(int | str, UUID | str, float | int: use one type, or a discriminated DTO),
an undiscriminated union holding a DTO or a container (add a discriminator),
a callable discriminator, a DTO overriding __init__ or __new__ (use
@model_validator(mode="after")), mode="before" / "wrap" validators over a DTO
and mode="plain" validators (use mode="after"), custom serializers
(SerializeAsAny included) and computed fields, a wire name that does not
round-trip (serialization_alias or validation_alias alone, AliasChoices,
AliasPath, an empty one: use Field(alias=...)), two fields with one wire name
(or a field named like another's wire name where the model reads by name), a
model_config option that changes the published JSON or its reading
(ser_json_*, val_json_bytes, val_temporal_unit, coerce_numbers_to_str,
regex_engine, use_enum_values, polymorphic_serialization, json_encoders,
json_schema_mode_override, allow_inf_nan=True, or any option the closed set
does not list: drop it), a float that may be NaN or infinite (a DTO's needs
allow_inf_nan=False), an excluded field without a default, dataclasses,
TypedDicts, NamedTuples, RootModels, unparametrised generics, and non-JSON
scalars (bytes, URLs, paths: use str). Each refused shape breaks the reader:
it ignores a newer producer's key at every depth (the same .vN may gain an
optional field) and must return what a constructed event holds, with the
same value and type.
The names channel, meta, publish and wire_schema are the Event API and are
refused as field names at class definition. A field may carry a wire name
(Field(alias=...)): publish() emits the alias, the exported schema names it,
and every consumer, view or projection reads it back by the alias.
The wire-schema annotation "x-sandloom-default" is the framework's mark of a
field with a default (the compatibility check trusts it): a json_schema_extra
that sets it, on a field or a nested model, is refused.
""",
    ),
    _landed(
        "SANDLOOM-B016",
        "consumer-definition",
        "boot",
        "an invalid @on consumer",
        "take exactly the event as the only parameter, use a unique name per "
        "module, and bind the tenant from a UUID tenant field",
        """
@on(E) registers through register_event_consumer with all its options; a
handler whose parameter is not E, a duplicate consumer name or a tenant field
that does not exist on E is refused (spec §5.4).
""",
    ),
    _landed(
        "SANDLOOM-B017",
        "authz-subject-missing",
        "boot",
        "a billing atom has no of= subject",
        "state the account: of=Account.user(), of=Account.org(param=...) "
        "(a route under /orgs/{...}) or of=Account.bound_tenant()",
        """
Subscribed, SubscriptionActive, Credits, Feature and WithinLimit take a
required of=: a default could check another billing account than the one the
route means (owner decision 1, spec §7.2).
""",
    ),
    _landed(
        "SANDLOOM-B018",
        "contract-definition",
        "boot",
        "an invalid @contract class",
        "keep the provider stateless (no __init__, no instance attributes), "
        "make members async, give every @command an idempotency_key, and "
        "inherit only from bases inside the module package",
        """
Contract providers are stateless classes registered with register_service;
the same class serves local and remote calls (spec §5.3).
""",
    ),
    _landed(
        "SANDLOOM-B019",
        "capability-replay",
        "boot",
        "a token issuer, intent or money route has the wrong idempotency guard "
        "or an intent route a request session",
        "give token-issuer and operation-intent routes an explicit "
        "idempotency_guard(scope=..., fail_closed=True, store_response=False) "
        "(mode X); an intent route takes no request-session dependency; a "
        'money route (sensitive("money")) uses durable=True or that mode X guard',
        """
A token or a bearer URL must never be replayed from storage: those routes
re-run authorization on every retry (mode X) and the effect's own record
keeps the effect single (spec §4.6, §4.7). A money route never relies on a
replay-only or fail-open guard: an ambiguous COMMIT would re-run the effect
(the check that replaced the class-F one, ADR-0009).
""",
    ),
    _entry(
        "SANDLOOM-B020",
        "ambiguous-module",
        "boot",
        "a requested module name has two providers (two installed packages, "
        "or an installed package and a local module)",
        "uninstall the package or remove (rename) the local module; with two "
        "installed packages, uninstall one of them",
        """
A module's name keys its tables, migration chain, channels and contracts:
the name of its `sandloom.modules` entry point for an installed package, else
the last segment of its import path. SANDLOOM_MODULES may name a module by
either, so one name must have one provider. The loader resolves the list
before anything loads and refuses a requested name that two installed
distributions register, or that an installed package and an app-local module
(`app.modules.identity` next to `sandloom-identity`) both carry. The message
names both providers. An unrequested duplicate is reported only when an
application asks for it.
""",
        "docs/concepts/modules.md",
    ),
    # --------------------------------------------------------------- runtime
    _entry(
        "SANDLOOM-R001",
        "missing-tenant",
        "runtime",
        "a tenant-scoped operation ran with no tenant bound",
        "add a TenantScope() parameter to the endpoint (after authentication), "
        "use bind_tenant(id) in workers, or unscoped() for system work",
        """
TenantScoped models fail closed: a read, update or delete without a bound
tenant raises instead of returning every tenant's rows. The message names
the model and the route that hit it.
""",
    ),
    _entry(
        "SANDLOOM-R002",
        "unit-of-work-required",
        "runtime",
        "db(), publish_event or record_audit ran outside a Unit of Work",
        "call it inside a request handler, a contract method, an @on consumer "
        "or a task; elsewhere open a unit with `async with open_uow(...)` "
        "(sandloom.sdk.data)",
        """
db() returns the session of the innermost open unit; outside any unit there
is none. The outbox row commits with the business change; without a
transaction there is nothing to commit with, so the guarantee cannot hold and
the call raises. required=False (with a `# sandloom: best-effort` marker) is the
explicit lossy form of an effect.
""",
    ),
    _entry(
        "SANDLOOM-R003",
        "event-schema-missing",
        "runtime",
        "an event was published on a channel with no registered schema",
        "declare an Event class for the channel in the module's events.py",
        "Every versioned channel needs a schema in every profile (dev = prod). "
        "See SANDLOOM-C017 for the static check.",
        "docs/reference/event-contracts.md",
    ),
    _entry(
        "SANDLOOM-R004",
        "event-payload-invalid",
        "runtime",
        "an event payload does not match its channel's schema",
        "fix the payload (every violation is listed) or roll the channel to a "
        "new version when the shape changes",
        "publish_event validates the payload inside the transaction, so an "
        "off-schema event never reaches the outbox.",
        "docs/reference/event-contracts.md",
    ),
    _entry(
        "SANDLOOM-R005",
        "service-unavailable-in-deployment",
        "runtime",
        "a contract is neither provided locally nor bound remotely",
        "declare it in consumes= (the loader then reports it at startup), bind "
        "it in SERVICE_BINDINGS, or degrade explicitly",
        (
            "core.get_service raises 501 "
            "ServiceUnavailableInDeployment; see SANDLOOM-C019."
        ),
    ),
    _entry(
        "SANDLOOM-R006",
        "unhandled-exception",
        "runtime",
        "an endpoint raised an unhandled exception",
        "read the original exception shown with this error",
        """
Production answers an opaque 500 internal_error. The test client re-raises the
original exception (type, message, fix) instead, so the assertion shows the
cause; tests that assert a 500 opt out with
`@pytest.mark.sandloom_allow_server_errors`.
""",
    ),
    # Public API 1.0 runtime errors (spec Appendix A): reserved, H1 (WS-A).
    _landed(
        "SANDLOOM-R007",
        "multi-store-write",
        "runtime",
        "a second unit of the request tried to write",
        "do all of the request's writes in its write store unit; a second "
        "store is written only under allow_multi_db_write(...)",
        """
One writer per request: the first unit that writes becomes the writer and a
write by any other unit (later, concurrent, nested or in a child task) is
refused before it reaches the cursor (spec §4.4).
""",
    ),
    _landed(
        "SANDLOOM-R008",
        "nested-or-concurrent-unit",
        "runtime",
        "a unit, read scope or connection started where the unit rules forbid it",
        "finish the open unit before starting another (or before "
        "external_call / release_check); open no unit while an external call "
        "or the release check runs",
        """
Nested committing units, overlapping units in an operation-intent route, and
any unit, read scope or checkout during an external_call() or
release_check() are refused, so no transaction spans a PSP call or the
authority re-read (spec §4.4 rule 5, §4.6). A window also refuses to open
(503, Retry-After) after a remote contract call of the request got no answer
from its provider for an attempt (a timeout, a transport failure): its
provider unit may still be running. Retry the request with the same
Idempotency-Key; the command replays or waits for the running one.
""",
    ),
    _landed(
        "SANDLOOM-R009",
        "money-effect-without-dedupe",
        "runtime",
        "a money effect ran without an effect dedupe record",
        "key the effect with its own dedupe record (ledger key, intent key, "
        "command log) in the same unit",
        "ADR-0009 item 11: a money effect must be single by its own record, "
        "not only by the request's idempotency key.",
        docs="docs/adr/0009-route-policy-and-session-freshness.md",
    ),
    _landed(
        "SANDLOOM-R010",
        "idempotency-key-required",
        "runtime",
        "a sensitive write was called without an Idempotency-Key (428)",
        "send an Idempotency-Key header with the request",
        "A money write whose effect has no dedupe record answers 428 without "
        "an Idempotency-Key, so a retry can never repeat it (spec §4.2).",
    ),
    _landed(
        "SANDLOOM-R011",
        "step-up-required",
        "runtime",
        "a security-sensitive route needs a fresh authentication (401, RFC 9470)",
        "re-authenticate (step-up) and retry with the new token",
        "security routes require a recent authentication; the 401 carries the "
        "RFC 9470 challenge (spec §4.2, ADR-0009).",
    ),
    _landed(
        "SANDLOOM-R012",
        "intent-release-unchecked",
        "runtime",
        "an operation-intent route answered 2xx outside the two release "
        "cases, or release_check() lacked the authority",
        "call release_check(account, authority=...) after the last external "
        "call, then commit exactly one unit with one write (issue), or return "
        "the stored value without an external call (issued retry)",
        """
The release rule is enforced at response time: a 2xx that matches neither the
issue case nor the issued-retry case is discarded, so a bearer URL never
leaves without the re-check. An organization account needs the authority
re-read (spec §4.6).
""",
    ),
    _landed(
        "SANDLOOM-R013",
        "concurrent-session-use",
        "runtime",
        "db() (or the unit's session) was used from a task that does not own the unit",
        "open the child task's own unit: `async with open_uow() as session:`",
        """
db() returns the session of the innermost unit and refuses a task that does
not own it (complete rule). Passing a session object to another task is
unsupported; session methods, sync_session methods, attribute assignments and
cursor executions from such a task are detected on a best-effort basis
(spec §4.3).
""",
    ),
    # ------------------------------------------------------- tests/snapshots
    _entry(
        "SANDLOOM-T001",
        "route-snapshot",
        "test",
        "a module's routes.snapshot.json does not match its routes",
        "run `sandloom routes snapshot --write` and review the diff (it is the "
        "approval of every route's operation class)",
        """
Each module commits routes.snapshot.json: method, path, auth (public or
authenticated) and the policy object of every route it mounts. The diff is
the review of a route's exposure and policy. Kernel-mounted routes (ops
endpoints, the platform console's /api/v1/platform/<module>/*) are decided by
the kernel and are not part of module snapshots.

`sandloom generate` refreshes an L1 module's snapshot, but never over a WEAKER
policy (authentication removed or the route made public, a risk removed,
idempotency removed / no longer fail-closed / no longer durable, the session
check weakened, the automatic audit dropped, step-up removed or relaxed, an
authorization dependency removed): it fails before writing anything. Review
the change, then approve it explicitly with `sandloom routes snapshot --write`,
which names each weakening it approves; the snapshot diff is the record.
`sandloom test` only checks (a stale snapshot: run `sandloom generate`).
""",
        "docs/reference/routes.md",
    ),
    _entry(
        "SANDLOOM-T002",
        "migration-chain",
        "test",
        "a table or index is not created by its owning migration chain",
        "add a revision to the owning module's chain: "
        '`sandloom migrate new <module> "<message>"` (row-level security included)',
        """
The test harness builds tables from the models, production from the chains.
The owning chain of a table is the chain whose revisions create it; a table
no chain creates, or an index missing from its chain, passes the suite and
breaks production. The check reads the revisions' source: a revision names
each table and index literally (or a table through a module-level string
constant), as `sandloom migrate new` writes it; a name built at runtime, such as
an f-string in a loop, is not recognised.
""",
    ),
    _entry(
        "SANDLOOM-T004",
        "contract-snapshot",
        "test",
        "a module's contracts.snapshot.json does not match its contracts",
        "run `sandloom generate` and review the diff; a breaking change is a new "
        "contract version (.v2), not an edit of .v1",
        """
Each module commits contracts.snapshot.json: the describe document of every
sync contract it provides. `sandloom generate` writes it after comparing it
with the committed one (a breaking change fails and nothing is written);
`sandloom test` only checks. Callers compare a bound provider's live describe
with it (rolling upgrades); `sandloom contracts check --against <snapshot>`
reports breaking changes.
""",
    ),
    _entry(
        "SANDLOOM-T003",
        "module-not-deployed",
        "test",
        "a module is not deployed in a deploy profile (warning)",
        "`sandloom add deploy <module> --service <name>` writes the service, "
        "gateway route and bindings together",
        "A module absent from deploy/compose/microservices.yml is "
        "unreachable there; this is reported as a warning, not a failure.",
        "deploy/README.md",
    ),
    # Public API 1.0 test gates (spec Appendix A): reserved, H2 (WS-B).
    _landed(
        "SANDLOOM-T005",
        "event-schema-compat",
        "test",
        "a breaking change to an event schema on the same .vN",
        "add only optional fields to a published channel; anything else is a "
        "new version (.v2, see the event-versioning guide)",
        "Compatibility is checked against the committed snapshot before "
        "`sandloom generate` writes anything, and by `sandloom test` (spec "
        "§5.2, §5.8), at every "
        "depth and in both directions (producers and consumers of one .vN "
        "deploy independently: a new reader accepts what an old producer "
        "sends, an old reader what a new producer sends). Any changed "
        "validation constraint is breaking: a narrowed one (a raised minimum, "
        "a new pattern) rejects an old producer's payload, a loosened one (a "
        "longer maxLength, a removed bound) a new producer's; so is a removed "
        "field, open model or not.",
    ),
    _landed(
        "SANDLOOM-T006",
        "migration-drift",
        "test",
        "the models differ from the head of their migration chain",
        'run `sandloom migrate new <module> "<message>"` and review the revision',
        "sandloom test compares the module's models with its chain head (spec §5.6).",
    ),
    _landed(
        "SANDLOOM-T007",
        "contract-stub-stale",
        "test",
        "a generated contract stub is out of date",
        "run `sandloom generate` to regenerate it (a compatible change) or make "
        "an incompatible change a new contract version",
        "Contract stubs in the shared DTO package are written by `sandloom "
        "generate` and never hand-edited; `sandloom test` fails on a stale one "
        "(spec §5.3, §5.9).",
    ),
    _landed(
        "SANDLOOM-T008",
        "public-api-baseline",
        "test",
        "the public API surface or a reference block changed",
        "review the change and refresh public_api_baseline.json "
        "(`uv run python tools/packaging/check_public_api.py --update`)",
        "public_api_baseline.json records every public name and signature "
        "(spec §2.1 rule 5, ledger gap G-KERNEL-11). Today the public-api "
        "pre-commit hook runs the same gate without this code.",
    ),
    _landed(
        "SANDLOOM-T009",
        "beginner-tier",
        "test",
        "generated code or an example uses an advanced name without a marker",
        "use the beginner-tier name, or mark the line `# sandloom: advanced <reason>`",
        "Generators and examples teach the 29 beginner names (spec §2.2).",
    ),
    _entry(
        "SANDLOOM-T010",
        "periodic-tasks-need-a-beat",
        "test",
        "a module with periodic tasks has no beat in a deploy profile",
        "give the module's service its worker and beat: `sandloom add deploy "
        "<module> --service <name>` for an undeployed module, `sandloom add task "
        '<module> <name> --periodic "<cron>"` or `sandloom add deploy --refresh` '
        "for a deployed one (each writes <service>-worker / <service>-beat); "
        "in a hand-written profile, add one beat per broker and a worker "
        "consuming it",
        "A schedule (setup_periodic_tasks in <module>/tasks.py) runs only "
        "where a celery beat loads the module, and exactly one may: none "
        "and the task never runs, two and it runs twice. sandloom test's static "
        "schema & deploy step checks every deploy profile.",
        "deploy/README.md",
    ),
    _entry(
        "SANDLOOM-T011",
        "route-index",
        "test",
        "the route dispatch index disagrees with the linear scan, or one "
        "bucket holds half the routes",
        "a disagreement is a kernel bug in sandloom.core.http.route_index (report "
        "it); an oversized bucket means routes of several modules share a "
        "literal prefix longer than route_index.MAX_DEPTH (a kernel change) "
        "or one module owns half the app's routes under one prefix (give it "
        "distinct literal prefixes)",
        """
The app's router scans only the routes whose leading literal path segments
(up to MAX_DEPTH) can match the request. Dispatch must be identical to
Starlette's linear scan; a bucket that holds most routes makes the index
pointless for the paths it serves (performance, not correctness).
""",
    ),
    _entry(
        "SANDLOOM-T012",
        "redundant-index",
        "test",
        "a plain index duplicates the leading column(s) of a unique key",
        "drop `index=True` / the Index from the model and add a revision that "
        'drops it (`sandloom migrate new <module> "<message>"`); the TenantScoped '
        "tenant_id index is dropped for you when a unique key leads with "
        "tenant_id, so keep per-tenant keys tenant-first",
        """
A unique index on (a, b) serves every lookup a plain index on (a) would, so
the plain one is pure write cost. sandloom test's static `schema & deploy` step
reports it (sandloom.core.testing.static_checks), with no database.
""",
    ),
    _entry(
        "SANDLOOM-T013",
        "tenant-natural-key",
        "test",
        "a TenantScoped table has a key without tenant_id (warning)",
        'lead the key with tenant_id (UniqueConstraint("tenant_id", ...), or a '
        "surrogate id primary key); a key that is unique across tenants on "
        "purpose (a UUID aggregate id of another module, an operation key) "
        'carries info={"sandloom_global_unique": "<reason>"}',
        """
A key of a per-tenant table without tenant_id is unique across tenants: the
same user in two organizations collides, the tenant-filtered read misses the
other tenant's row and the insert fails on the key (a consumer dead-letters).
sandloom test's static schema & deploy step warns; a globally unique id is safe,
which no static check can tell.
""",
    ),
    _entry(
        "SANDLOOM-T014",
        "split-profile",
        "test",
        "the microservices profile breaks a split invariant",
        "`sandloom add deploy --refresh` re-derives the generated blocks (services, "
        "bindings, RPC_ALLOWED_CALLS, databases, gateway routes) from what the "
        "modules provide and consume; a problem outside them (a shared setting "
        "set per service, an issuer-only key elsewhere) is fixed by hand in "
        "x-common-env / the service's anchor",
        """
deploy/compose/microservices.yml must hold what the framework's own
profile holds: shared settings identical in every process (one CACHE_URL),
signing material on the token issuer only, a database and a migrate one-shot
per service, the durable bus, SERVICE_BINDINGS exactly the contracts each
service consumes from another one (+ the issuer's remote claims),
RPC_ALLOWED_CALLS derived from them, its own RPC keys per service, liveness
never fed by peers, and a gateway that routes every route a service's app
serves (root-level `/` and `/{slug}` included: `location = /` and the
catch-all `location /`) to that service and never /_sandloom, /diagnostics or
/metrics. sandloom test's static schema & deploy step checks it
(sandloom.core.testing.deploy_profile); `sandloom add deploy` writes a profile that
passes, and refuses two services claiming the same route prefix, `/` or the
root-level parameter paths (deploy those modules together, or give them
distinct prefixes).
""",
        "docs/guides/split-into-services.md",
    ),
)

_BY_CODE = {e.code: e for e in CATALOGUE}
_BY_SLUG = {e.slug: e for e in CATALOGUE}

#: Exception class name -> code, for exceptions defined in packages that do
#: not carry an ``sandloom_code`` attribute themselves.
EXCEPTION_CODES: dict[str, str] = {
    "ModuleLoadError": "SANDLOOM-B001",
    "ModuleRegistrationError": "SANDLOOM-B006",
    "ModuleRegistrationConflict": "SANDLOOM-B006",
    "ContractTypeError": "SANDLOOM-B006",
    "GuardOrderError": "SANDLOOM-B002",
    "ContractBindingError": "SANDLOOM-B005",
    "StoreCapabilityError": "SANDLOOM-B010",
    "GateOrderError": "SANDLOOM-B007",
    "MissingTenantError": "SANDLOOM-R001",
    "UnitOfWorkRequiredError": "SANDLOOM-R002",
    "ServiceUnavailableInDeployment": "SANDLOOM-R005",
}


def lookup(key: str) -> ErrorCode | None:
    """The entry for a code (``SANDLOOM-B002``, ``B002``, ``b002``) or a slug."""
    raw = key.strip()
    if raw in _BY_SLUG:
        return _BY_SLUG[raw]
    code = raw.upper()
    if not code.startswith("SANDLOOM-"):
        code = f"SANDLOOM-{code}"
    return _BY_CODE.get(code)


def get(code: str) -> ErrorCode:
    """The entry for ``code`` (KeyError for an unknown code: a framework bug)."""
    entry = lookup(code)
    if entry is None:
        raise KeyError(code)
    return entry


def code_for_exception(exc: BaseException) -> str | None:
    """The catalogue code of ``exc`` (its ``sandloom_code``, else by class name)."""
    own = getattr(exc, "sandloom_code", None)
    if isinstance(own, str):
        return own
    for cls in type(exc).__mro__:
        if cls.__name__ in EXCEPTION_CODES:
            return EXCEPTION_CODES[cls.__name__]
    return None


def documentation_url(code: str) -> str:
    """Canonical catalogue permalink; accepts the same aliases as ``get``.

    Pure formatting: error reporting stays usable without network access.
    Retired codes keep their anchors, just as they keep their catalogue entries.
    """
    return f"https://sandloom.dev/docs/reference/error-codes/#{get(code).code.lower()}"


def render(
    code: str,
    what: str,
    *,
    where: str | None = None,
    fix: str | None = None,
    severity: str = "error",
) -> str:
    """One diagnostic in the standard format (see the module docstring).

    ``where`` is ``path:line`` (clickable), optionally followed by a
    route / model / symbol; ``fix`` defaults to the catalogue's fix.
    """
    entry = get(code)
    lines = [f"{severity}[{entry.code}]: {what}"]
    if where:
        lines.append(f"  --> {where}")
    lines.append(f"  fix: {fix or entry.fix}")
    lines.append(f"  docs: {documentation_url(entry.code)}")
    lines.append(f"  help: sandloom explain {entry.code}")
    return "\n".join(lines)


def fix_line(code: str) -> str:
    """``"fix: ... (sandloom explain SANDLOOM-xxxx)"`` — appended to exception
    messages.
    """
    entry = get(code)
    return (
        f"fix: {entry.fix} (sandloom explain {entry.code}; "
        f"{documentation_url(entry.code)})"
    )


def explain_text(entry: ErrorCode) -> str:
    """The long form ``sandloom explain`` prints."""
    lines = [
        f"{entry.code} [{entry.slug}] ({entry.kind})",
        "",
        entry.title,
        "",
        entry.explain,
        "",
        f"Fix: {entry.fix}",
        f"Docs: {documentation_url(entry.code)}",
    ]
    if entry.docs:
        lines.append(f"Source reference: {entry.docs}")
    if entry.planned:
        lines.append(
            f"Status: planned, nothing raises it yet; the check lands at "
            f"{entry.planned}."
        )
    return "\n".join(lines)


def render_catalogue_markdown(entries: Iterable[ErrorCode] = CATALOGUE) -> str:
    """The generated reference page (``docs/reference/error-codes.md``)."""
    out = [
        "# Error codes",
        "",
        "<!-- Generated from sandloom.core.error_codes at docs build (also"
        " `sandloom explain --markdown`). -->",
        "",
        "Every framework diagnostic carries a stable code. "
        "`sandloom explain <code>` prints the long form; `sandloom explain --list` "
        "lists them.",
        "",
        "| Code | Id | Kind | What | Fix |",
        "|---|---|---|---|---|",
    ]
    for e in entries:
        if e.retired:
            continue
        fix = e.fix.replace("|", "\\|")
        title = e.title.replace("|", "\\|")
        kind = f"{e.kind} (planned: {e.planned})" if e.planned else e.kind
        out.append(
            f"| [`{e.code}`]({documentation_url(e.code)}) | `{e.slug}` | "
            f"{kind} | {title} | {fix} |"
        )
    return "\n".join(out) + "\n"


def _validate() -> None:
    seen: set[str] = set()
    for e in CATALOGUE:
        if not _CODE_RE.match(e.code):
            raise ValueError(f"malformed error code {e.code!r}")
        if e.code in seen or list(_BY_SLUG).count(e.slug) > 1:
            raise ValueError(f"duplicate error code / slug {e.code} {e.slug}")
        seen.add(e.code)
        if e.planned and e.retired:
            raise ValueError(f"{e.code} cannot be both planned and retired")
    for name, code in EXCEPTION_CODES.items():
        if code not in _BY_CODE:
            raise ValueError(f"{name} maps to unknown code {code}")


_validate()


def planned_codes() -> tuple[ErrorCode, ...]:
    """Reserved codes whose check has not landed yet (``planned`` set)."""
    return tuple(e for e in CATALOGUE if e.planned)


__all__ = [
    "CATALOGUE",
    "EXCEPTION_CODES",
    "ErrorCode",
    "code_for_exception",
    "documentation_url",
    "explain_text",
    "fix_line",
    "get",
    "lookup",
    "planned_codes",
    "render",
    "render_catalogue_markdown",
]
