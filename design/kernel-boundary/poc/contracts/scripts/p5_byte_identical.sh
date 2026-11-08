#!/bin/bash
# kb-poc-contracts proof 5: byte-identical generation across hash seeds and
# module orders, then a generator-format bump with unchanged declarations.
# Run from the candidate worktree root.
set -u
# dummy infrastructure URLs: nothing connects during generate
export DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:1/x_test CACHE_URL=redis://127.0.0.1:1/9 BROKER_URL=redis://127.0.0.1:1/10 JWT_SECRET=kb-poc-contracts-not-a-secret-0123456789
snap() {  # sha256 of every consumer copy and module-side snapshot
  find src -path '*/_generated/*' -type f -o -name '*.snapshot.json' -type f | sort | xargs shasum -a 256 | shasum -a 256 | cut -c1-16
}
regen() {  # $1 = label; remaining = env
  local label=$1; shift
  rm -rf src/sandloom_billing/_generated src/sandloom_admin/_generated
  env "$@" uv run --no-sync sandloom generate > /tmp/kbpocc-gen.log 2>&1
  local rc=$?
  local mods=$(env "$@" uv run --no-sync python -c "import sandloom.main as m; print(','.join(m.core.loaded_modules))" 2>/dev/null | tail -1)
  echo "$label: exit $rc; modules [$mods]; digest $(snap) over $(find src -path '*/_generated/*' -type f -o -name '*.snapshot.json' -type f | wc -l | tr -d ' ') files"
}
regen "PYTHONHASHSEED=0      default order" PYTHONHASHSEED=0
regen "PYTHONHASHSEED=12345  default order" PYTHONHASHSEED=12345
regen "PYTHONHASHSEED=random default order" PYTHONHASHSEED=random
regen "PYTHONHASHSEED=0      reversed order" PYTHONHASHSEED=0 SANDLOOM_MODULES='["tenancy","identity","billing"]'
regen "PYTHONHASHSEED=999    reversed order" PYTHONHASHSEED=999 SANDLOOM_MODULES='["tenancy","identity","billing"]'
echo
echo "\$ sandloom generate --check   (GENERATOR_FORMAT patched 2 -> 3, declarations unchanged)"
uv run --no-sync python -c "
import sandloom.core.cli.consumer_copies as c
c.GENERATOR_FORMAT = 3
from sandloom.core.cli.generated import main
raise SystemExit(main(['--check']))
" 2>&1 | grep -v WARNING
echo "[exit ${PIPESTATUS[0]}]"
