#!/bin/sh
# Create one database per service on first start of the Postgres container
# (docker-entrypoint-initdb.d runs this only when the data volume is empty).
# A service added to a running stack later gets its database from its
# migrate one-shot (sandloom migrate upgrade-all --apply --create-databases).
#
#   POSTGRES_MULTIPLE_DATABASES="sandloom_identity sandloom_tenancy sandloom_billing"
#
# Each service owns its own database (its module tables + its own transactional
# outbox / audit / consumer inbox) — modules never share tables or FKs, so this
# is the microservices model of Sandloom's per-module-per-database migrations.
set -eu

for db in ${POSTGRES_MULTIPLE_DATABASES:-}; do
    echo "init-databases: creating database ${db}"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
SELECT 'CREATE DATABASE "${db}"'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${db}')\gexec
SQL
    # citext backs case-insensitive emails (identity users, tenancy
    # invitations), pg_trgm the admin user search index; create them as the
    # superuser so migrations need no extra privilege.
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" \
        -c 'CREATE EXTENSION IF NOT EXISTS citext' \
        -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm'
done
