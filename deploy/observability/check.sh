#!/usr/bin/env sh
# Validate the observability bundle: dashboards regenerate identically, the
# Prometheus config and rules parse, and the rule unit tests pass.
# Needs python3 and Docker (promtool runs in the pinned Prometheus image). The
# files are streamed into the container (no bind mount), so this also works
# where the Docker daemon cannot see this checkout (act, remote daemons).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
image=${PROMETHEUS_IMAGE:-prom/prometheus:v3.5.0}

python3 "$here/build_dashboards.py" --check
for f in "$here"/grafana/dashboards/*.json; do
  python3 -m json.tool "$f" >/dev/null
done

COPYFILE_DISABLE=1 tar -C "$here" -cf - prometheus | docker run --rm -i --entrypoint sh "$image" -euc '
  mkdir -p /tmp/b && cd /tmp/b && tar -xf -
  echo check-only-token >/tmp/b/metrics_token
  sed -e "s#/etc/prometheus/rules/#/tmp/b/prometheus/rules/#" \
      -e "s#/run/secrets/metrics_token#/tmp/b/metrics_token#" \
    prometheus/prometheus.yml >prometheus/prometheus.check.yml
  promtool check config prometheus/prometheus.check.yml
  promtool check rules prometheus/rules/*.yml
  cd prometheus/tests && promtool test rules sandloom-rules.test.yml
'
echo "observability bundle OK"
