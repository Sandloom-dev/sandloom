#!/bin/bash
# run_pair.sh <provider-venv> <variant> <consumer-venv> <compat> [consumer args...]
PC=/private/tmp/claude-501/-Users-admin-Documents-dev-projects-sandloom/16c003c1-5645-4e86-b158-8b561d6700f3/scratchpad/pc
SC=/Users/admin/Documents/dev_projects/sandloom/design/kernel-boundary/poc/contracts/scripts
PV=$1; VAR=$2; CV=$3; COMPAT=$4; shift 4
$PC/$PV/bin/python -I $SC/provider.py --keys $PC/keys.json --port 39701 --variant $VAR > $PC/prov.log 2>&1 &
PID=$!
for i in $(seq 50); do grep -q "^provider" $PC/prov.log 2>/dev/null && break; sleep 0.2; done
echo "\$ [$PV] provider.py --variant $VAR"; head -1 $PC/prov.log
echo "\$ [$CV] consumer.py --compat $COMPAT $*"
(cd /tmp && $PC/$CV/bin/python -I $SC/consumer.py --keys $PC/keys.json --provider-port 39701 --compat $COMPAT "$@" 2>&1 | grep -vE "INTERNAL_API_TOKEN|JWT_SECRET is|dev mode|^\s*$")
kill $PID 2>/dev/null; wait $PID 2>/dev/null
