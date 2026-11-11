# Run the authorization stand on the current kernel

`authzstand` and `authzpa2` use module-local generated atoms. Both now run the
shipped implementation; their names preserve the historical route sets, not
an implementation difference. The `authzpa3` prototype, historical switches
and stored results remain research artifacts; they are not a current-kernel
performance comparison.

Run from the repository root. Use a dedicated, fresh database and Redis
instance (or allocate non-overlapping Redis database ranges). The stand uses
Redis databases 3–6 and, for `counts`, 14–15, plus `KB_REDIS_DB_OFFSET`.
There is no automatic Redis flush. Do not share a database with another
benchmark seed: these fixtures reserve their UUIDs and role codes.

Export `DATABASE_URL` (owner DSN), `CACHE_URL` and `BROKER_URL`. Seed the
standard platform, then load `seed_billing.sql` and `seed_subjects.sql` with
`psql` against that same database:

```sh
PYTHONPATH=benchmarks uv run python -m benchsuite.platform seed
psql "$PG_DSN" -v ON_ERROR_STOP=1 -f design/kernel-boundary/poc/authz/seed_billing.sql
psql "$PG_DSN" -v ON_ERROR_STOP=1 -f design/kernel-boundary/poc/authz/seed_subjects.sql
```

`PG_DSN` is the same owner DSN with the `postgresql://` scheme understood by
`psql`. The additional fixtures provide the admin role and permission, an
ordinary user with credits, a member, an outsider, a past-due subscriber,
and an organization with an inactive subscription. Tokens are minted from
those real authority records through `benchsuite.claims_mint`; a hand-built
JWT is rejected by the current token policy.

Regenerate the consumers after changing their declarations or the providers:

```sh
PYTHONPATH=design/kernel-boundary/poc/authz/stand:benchmarks \
SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main \
SANDLOOM_MODULES='["sandloom_identity","sandloom_tenancy","sandloom_billing","authzstand","authzpa2"]' \
uv run sandloom generate --root design/kernel-boundary/poc/authz authzstand authzpa2
```

First check decisions without Cachegrind, then build the ladder image and
measure a selected authenticated route:

```sh
uv run python design/kernel-boundary/poc/authz/stand.py decisions \
  --times 1 --out /tmp/authz-decisions.json
docker build -t sandloom-ladder-cg:3.13 benchmarks/ladder/cg
CG_K=200 CG_LOGS=/tmp/authz-cg KB_REDIS_DB_OFFSET=32 \
  design/kernel-boundary/poc/authz/cg_poc.sh bound.orgrole_and_subactive
```

Both N=0 and N=K runs warm up and assert the route's expected HTTP status.
Docker errors or missing Cachegrind counts fail the command, so a failed
request cannot turn into a zero instruction count. `CG_ROOT` selects a
checkout to mount; the older `S` plus `SIDE` selection still works. The
standard composition package and migrations are mounted read-only alongside
its source and benchmark packages. Use the same seed, root and Redis offset
for both runs; use another range for a simultaneous run.

Harness regression checks require no database or Docker daemon:

```sh
uv run python -m unittest discover -s design/kernel-boundary/poc/authz -p test_stand.py
```
