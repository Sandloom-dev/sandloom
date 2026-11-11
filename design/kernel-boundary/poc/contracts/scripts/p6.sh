#!/bin/bash
SC=/Users/admin/Documents/dev_projects/sandloom/design/kernel-boundary/poc/contracts/scripts
export DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:1/x_test CACHE_URL=redis://127.0.0.1:1/9 BROKER_URL=redis://127.0.0.1:1/10 JWT_SECRET=kb-poc-contracts-not-a-secret-0123456789
G=src/sandloom_billing/_generated/contracts
run() { uv run --no-sync python $SC/p6_gen_without_provider.py "$@" 2>&1 | grep -vE "WARNING|plugin_loader|^\s*$" ; echo "[exit ${PIPESTATUS[0]}]"; }
echo "## a) committed copy present and unedited"
echo "\$ sandloom generate (tenancy absent)"; run
echo "\$ sandloom generate --check (tenancy absent)"; run --check
echo "\$ git status --short $G"; git status --short $G | grep -v "^A  " || echo "(no change)"
echo; echo "## c) committed copy edited by hand (a field default changed)"
cp $G/tenancy_dto.py /tmp/kbpocc-dto.bak
sed -i '' 's/    version: int$/    version: int = 0/' $G/tenancy_dto.py
echo "\$ sandloom generate --check (tenancy absent)"; run --check
echo "\$ sandloom generate (tenancy absent)"; run
cp /tmp/kbpocc-dto.bak $G/tenancy_dto.py
echo; echo "## b) no committed copy"
mv src/sandloom_billing/_generated /tmp/kbpocc-gen.bak
echo "\$ sandloom generate (tenancy absent)"; run
echo "\$ ls src/sandloom_billing/_generated"; ls src/sandloom_billing/_generated 2>&1
rm -rf src/sandloom_billing/_generated; mv /tmp/kbpocc-gen.bak src/sandloom_billing/_generated
echo; echo "## restore: provider importable again"
echo "\$ sandloom generate --check"; uv run --no-sync sandloom generate --check 2>&1 | tail -1
