#!/bin/bash
# kernel tests with no battery importable: $1 = base|contracts, $2 = db suffix, $3 = redis base
cd $(dirname $0)/k-$1
export SANDLOOM_TEST_OWNER=kb-poc-contracts-k$1
export SANDLOOM_TEST_DATABASE_URL=postgresql+asyncpg://poc:poc@127.0.0.1:39633/poc_k$2_test
export SANDLOOM_TEST_CACHE_URL=redis://127.0.0.1:39680/$3
export SANDLOOM_TEST_BROKER_URL=redis://127.0.0.1:39680/$(( $3 + 1 ))
.venv/bin/python -c "import importlib.util as u; print('batteries importable:', [m for m in ('sandloom_identity','sandloom_tenancy','sandloom_billing','sandloom_admin') if u.find_spec(m)]); import sandloom.shared as s; print('sandloom.shared.contracts importable:', bool(u.find_spec('sandloom.shared.contracts')))"
.venv/bin/python -m pytest src/sandloom/core/tests tests/sdk tests/architecture -q -p no:randomly -n 6 -o pythonpath= 2>&1 | grep -E "^(FAILED|ERROR)|passed|failed| error" | cut -c1-260
