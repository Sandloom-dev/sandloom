# Execution planner: production contract

The planner executes a module's declared reads and returns its declared
Pydantic DTOs. `PLANNER_OPTIMIZED_EXECUTOR=true` is the default. Setting it to
`false` and restarting selects the general ORM executor without changing module
code or database schema. Ordinary `DbSession` queries are unaffected.

```python
from sandloom.sdk.data import ReadOperation

item_by_id = ReadOperation(entity=Item, result=ItemOut, by=("id",))

# In an endpoint with PlannedReads (sandloom.sdk.data) and the usual auth/tenant guards:
item = await reads.one(item_by_id, id=item_id)
```

Register operations with `core.register_operations(...)` during module setup.
The startup task validates and prepares them. `/diagnostics/core` lists each
registered operation's executor and selection reason. Invalid declarations fail
startup; unsupported optimization uses the general executor.

## Selection and compatibility

The optimized executor is restricted to PostgreSQL + asyncpg, plain column DTOs,
single-table mappings and declared FK joins. Single-row keys must prove at most
one row through an unconditional primary/unique key, including the tenant
predicate where applicable. Nonunique single-row operations use the ORM's
cardinality check; they never silently select the first of several matches.
A `JoinRead` may be keyed on the joined row only (`by=()`, `join_by` on a
unique key of `join`) when its `via` column is itself unique on the entity
(a one-to-one detail row, e.g. a customer's credit balance found by the
customer's unique tenant column); a join that could return several rows is
refused at planning.

Referenced column types must have equivalent native driver semantics. Custom
`TypeDecorator` types, enums, JSON/arrays, numeric conversions and unknown types
use SQLAlchemy, preserving bind/result processing. Inheritance, polymorphic
mappings (including a discriminator root with no subclasses), deferred columns
and eager ORM loading policies also use the general executor. Boolean result
columns and exact `True`/`False` constants remain eligible. Dynamic Boolean
keys/cursors and constants requiring coercion use the ORM: SQLAlchemy accepts
integer `0`/`1` inputs that raw asyncpg rejects.

Application ORM execute/load/session listeners and engine execution/transaction
listeners select the general executor. These checks also guard prepared plans:
installing a policy after startup cannot leave a cached raw query bypassing it.
The kernel tenant listener is recognized because both executors implement that
same tenant predicate. A policy-triggered fallback remains general for that
adapter until restart, and logs its reason once when the cached plan changes.

Optimization is decided before execution. A failed query is not silently
re-executed through another executor. The existing single retry only applies
when the driver proves that the first transaction message was never sent.

## Transactions and isolation

An optimized read transaction executes:

1. `BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY` and the transaction-local
   tenant setting, in one protocol message;
2. generated, parameterized `SELECT` statements;
3. `COMMIT`.

This also applies when `tenant_rls` is disabled: a view can call a volatile SQL
function, so a SELECT by itself does not prove that a read cannot write.
Connections are leased exclusively. Errors and cancellation terminate and
invalidate uncertain connections. Failed read-unit setup and cancelled
connection return also release their leases.

PostgreSQL pool reconnection is admitted separately from query execution.
Transport failures use exponential backoff with equal jitter (initial ceiling
50 ms, capped at 2 s) and one recovery probe per process/pool. Connection setup
times out after 5 s; admission waiting is bounded by `DB_POOL_TIMEOUT`.
`DB_CONNECT_TIMEOUT_SECONDS`, `DB_RECONNECT_BASE_SECONDS` and
`DB_RECONNECT_MAX_SECONDS` tune these bounds. A caller makes one establishment
attempt and gets 503 if it fails; SQL and transactions are never replayed by
this backoff. Healthy connection creation remains concurrent.

`PlannedReads` prohibits framework writes, events, audit and on-commit hooks.
Authentication, live block/revocation checks and tenant binding remain the
endpoint's normal guards. Tenant operations require a bound tenant before I/O;
generated SQL filters every tenant-owned side of a join and the transaction
sets the RLS GUC. Production strict profile continues to verify the database
role and FORCE RLS policies independently.

An ordinary read scope uses READ COMMITTED. Consecutive optimized reads share
one lazy read-only transaction. A transition to a general read ends and releases
that transaction before opening an ORM read unit; subsequent reads share that
unit. READ COMMITTED gives each statement a fresh snapshot in either case.
One-shot reads outside a shared scope have individual transactions. Use `SnapshotReads`
or `read_scope(..., isolation="repeatable_read")` for one consistent snapshot:
all reads then use the general read unit. `UowReads` and `session_reads(db)`
always use the existing writable UoW, preserving autoflush, read-your-writes,
locking and commit/rollback semantics.

## Lists and schema requirements

`ListOperation` uses bounded keyset pages and a cursor signed for its operation,
filters and tenant. Limits are mandatory and capped. Ordering columns must be
NOT NULL; the primary key supplies the final tie-breaker. Nullable ordering is
refused rather than silently losing rows after a NULL cursor position.
Each order value is read once per row: an order column the DTO already
projects (a plain column of the entity) is taken from its DTO position
(`Plan.order_index`); only the others are appended to the select list
(`Plan.order_width`). The row shape is the same on both executors, and the
cursor is the same bytes either way.

Optional filters: `optional={argument: column}` applies `column = argument`
and `prefix={argument: string column}` applies `column LIKE argument || '%'`
(`ILIKE` with `prefix_ignore_case=True`), each only when the call's argument
is not `None` (`None` means "no filter", never `IS NULL`). A prefix is matched
literally: `%`, `_` and `\` in it are escaped (PostgreSQL's default `LIKE`
escape character) by both executors alike. Every call passes every argument;
the cursor is bound to all of them, so a cursor reused with other filters is
refused. Each combination of present filters is its own statement (at most
four optional + prefix filters, 16 statements, built on first use) — never a
`$n IS NULL OR …` predicate. They are residual predicates: the covering-index
rule (equality filters, then the order columns) is unchanged, a page walks
that index in order and skips non-matching rows. A `citext` prefix column or
a dynamic Boolean filter uses the general executor, as for keys.

Covering-index and uniqueness proofs ignore partial indexes unless a future
implementation proves their predicate. A partial unique index does not make
the entire table unique. Declared read joins remain within a module; the planner
does not permit cross-module SQL joins.

## Rollout and verification

Use the same image and database to compare both executors. Check correctness,
foreign-tenant refusals, cancellation/connection cleanup and the operation
selection reasons before comparing throughput. Keep the rollback flag available.
The production qualification fixture exercises ordinary ORM requests, planned
reads, real identity/tenancy/billing operations and infrastructure failures;
the reproducible fixtures are in
[the qualification directory](https://github.com/Sandloom-dev/sandloom/tree/main/benchmarks/qualification).
Historical result reports remain in the original private repository and are
not bundled as public release evidence. Run the fixtures against the release
revision before making throughput claims.
