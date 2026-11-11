# Source me: the PoC's own containers and interpreters (kb-poc-authz).
export S=/private/tmp/claude-501/-Users-admin-Documents-dev-projects-sandloom/16c003c1-5645-4e86-b158-8b561d6700f3/scratchpad/poc-authz
export DATABASE_URL=postgresql+asyncpg://postgres:pw@127.0.0.1:47533/kbpoc
export CACHE_URL=redis://127.0.0.1:47536/3
export KB_PG_CONTAINER=kb-pocauthz-pg
export SANDLOOM_TEST_OWNER=kb-poc-authz
# side=base|wt  variant=authzstand|authzpa2|authzpa3  topology=mono|split|...
stand() {  # stand <side> <variant> <topology> <cmd> [args...]
  local side=$1 variant=$2 topo=$3 cmd=$4; shift 4
  local py=$S/$side/.venv/bin/python
  [ "$topo" != mono ] && py="$S/venv-consumer-$side/bin/python -I"
  KB_REPO=$S/$side KB_VARIANT=$variant KB_PROVIDER_PYTHON=$S/$side/.venv/bin/python \
    $py stand.py $cmd --topology $topo "$@"
}
# rebuild the kernel wheel of a side and reinstall it into its consumer venv
rewheel() {
  local side=${1:-wt}
  (cd $S/$side && rm -f $S/dist-$side/sandloom-0*.whl && uv build -q --wheel -o $S/dist-$side packages/sandloom) &&
  uv pip install -q --reinstall-package sandloom --python $S/venv-consumer-$side/bin/python \
    "sandloom[redis,celery] @ file://$S/dist-$side/sandloom-0.7.0a11-py3-none-any.whl"
}
