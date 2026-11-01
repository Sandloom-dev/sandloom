---
title: Database
description: The relational interface behind db() and the unit of work, its sqlalchemy driver for PostgreSQL and what another driver would have to provide.
type: concept
---

# Database

The database is your system of record: it backs the unit of work, the outbox and audit.

## What a module calls

`db()` is the session of the running unit on the module's database
([Units of work](../modules/database/units.md)). A named database is
`Annotated[AsyncSession, Database("x")]` in a route, or `open_uow("x")` in a block. Each
database has its own unit and its own outbox, and no transaction spans two of them. A route
that writes to several needs `writes(...)` or `allow_multi_db_write(...)`
([Write admission](../modules/guarantees/write-admission.md)).

## Drivers

### `sqlalchemy`

PostgreSQL through asyncpg, and the only relational backend. Set `DATABASE_URL`. The URL must
name its driver, `postgresql+asyncpg://`; never rely on a bare `postgresql://`.

| Option | Meaning |
| --- | --- |
| `url` | the connection URL; falls back to `DATABASE_URL` |
| `tenant_rls` | turns on PostgreSQL row-level security ([Multi-tenancy](../modules/database/multi-tenancy.md)) |
| `system_url` | the separate login role for framework cross-tenant work; needs `tenant_rls` |
| `require_system_channel` | fails startup when no system channel is configured |

## The relational paradigm

`database` is the **relational** capability: the SQL-shaped system of record
that backs the unit of work, the outbox and audit. The framework abstracts
engine construction and pooling; the query dialect is SQLAlchemy, used only
in `models/` and `services/`. Sibling paradigms (`document_store`,
`graph_store`, ...) have their own contracts and weaker guarantees, so no
universal "database" abstraction has to span them. The relational guarantees
are features (`transactional_outbox`, `audit`), not assumptions: a driver
that cannot provide them does not advertise them. No distributed transaction
spans backends.

The relational backend is not freely swappable today: the only driver is
`sqlalchemy`, the unit of work, outbox, audit, tenancy and migrations are
SQLAlchemy code in the kernel, and the settings require a
`postgresql+asyncpg://` URL. In practice it is PostgreSQL.

Sandloom isolates business code from connecting to and operating infrastructure;
the full set of transactional guarantees is implemented and verified on
PostgreSQL through the SQLAlchemy driver. The unit guarantees (one writer
per request, SANDLOOM-R007; conservative write detection at the statement
boundary; refusal of an exited unit, SANDLOOM-R002; the lazy request unit;
unflushed changes counted as writes) are driver obligations, not kernel
code. A database driver advertises them as the `write_admission` feature
(with `transactions`), implements `LazyUnitOfWorkContract` (`open_lazily` /
`close`) and `DatabaseProvider.has_pending_changes`, and `create_app`
refuses (SANDLOOM-B010) a database that hosts request units without
`write_admission`. Another relational driver must implement these features
to host request units; a driver conformance suite for them is planned.
