#!/usr/bin/env bash
# Smoke test of the microservices profile THROUGH the nginx gateway.
#
# Expects `docker compose -f deploy/compose/microservices.yml up -d`
# to be running (CI job `microservices-smoke`). Proves, end to end across
# real containers:
#   1. the gateway answers /health;
#   2. register + login go through the identity service;
#   3. an organization is created on the tenancy service;
#   4. a billing endpoint gated on organization membership succeeds — billing
#      resolves the membership over internal RPC to tenancy — and refuses an
#      organization the user does not belong to;
#   5. the identity worker is health-checked and executes a task;
#   6. with billing STOPPED, identity/tenancy stay live and ready, login and
#      /orgs keep working and the gateway fails billing calls fast; a
#      recreated billing container (new IP) is reached without restarting
#      the gateway.
#
# Usage: deploy/smoke/microservices-smoke.sh [gateway-url]
# (COMPOSE_PROJECT_NAME selects a non-default stack; the compose variables
# the profile requires must be exported, as for `up`.)
set -euo pipefail

GATEWAY="${1:-http://localhost:8080}"
COMPOSE=(docker compose -f "$(dirname "$0")/../compose/microservices.yml")
TIMEOUT="${SMOKE_TIMEOUT_SECONDS:-240}"
EMAIL="smoke-$(date +%s)-$RANDOM@example.com"
PASSWORD="Sm0ke-Test-Passw0rd-$RANDOM!"

log() { printf '[smoke] %s\n' "$*"; }
fail() { printf '[smoke] FAIL: %s\n' "$*" >&2; "${COMPOSE[@]}" ps >&2 || true; exit 1; }
json() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT

# request <method> <path> [curl args...] -> prints the HTTP status; the body
# is kept in $BODY_FILE (read it with `body`; $(...) runs in a subshell).
request() {
  local method="$1" path="$2"; shift 2
  : >"$BODY_FILE"
  curl -sS -o "$BODY_FILE" -w '%{http_code}' -X "$method" "$GATEWAY$path" "$@" || true
}
body() { cat "$BODY_FILE" 2>/dev/null || true; }

# wait_for <what> <method> <path> <status-regex>
wait_for() {
  local what="$1" method="$2" path="$3" want="$4"
  local deadline=$(( $(date +%s) + TIMEOUT ))
  until request "$method" "$path" | grep -Eqx "$want"; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
      fail "$what not ready after ${TIMEOUT}s (last body: $(body))"
    fi
    sleep 3
  done
  log "$what ready"
}

wait_for "gateway" GET /health 200
wait_for "identity" GET /auth/sso/providers 200
wait_for "tenancy" GET /api/v1/orgs "401|403"  # up, unauthenticated
wait_for "billing" GET /api/v1/billing/plans 200

log "register $EMAIL via identity"
status="$(request POST /auth/register -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"full_name\":\"Smoke Test\",\"password\":\"$PASSWORD\"}")"
[ "$status" = 202 ] || fail "register answered $status: $(body)"

# Password accounts sign in only once the email is verified. The verification
# link goes to the (log) notifier; the smoke test marks the row verified.
"${COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -U sandloom -d sandloom_identity -c \
  "UPDATE users SET is_verified = true, email_verified_at = now() WHERE email = '$EMAIL'" \
  >/dev/null || fail "could not mark the smoke user verified"

log "login via identity"
status="$(request POST /auth/login -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}")"
[ "$status" = 200 ] || fail "login answered $status: $(body)"
TOKEN="$(body | json "['access_token']")"
AUTH=(-H "Authorization: Bearer $TOKEN")

log "create an organization via tenancy"
SLUG="smoke-$RANDOM$RANDOM"
status="$(request POST /api/v1/orgs "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"name\":\"Smoke Org\",\"slug\":\"$SLUG\"}")"
[ "$status" = 201 ] || fail "create org answered $status: $(body)"
ORG_ID="$(body | json "['id']")"

log "billing credits for the org (membership resolved over RPC to tenancy)"
status="$(request GET "/api/v1/billing/credits?org_id=$ORG_ID" "${AUTH[@]}")"
[ "$status" = 200 ] || fail "org credits answered $status: $(body)"
[ "$(body | json "['balance']")" = 0 ] || fail "unexpected balance: $(body)"

log "billing refuses an organization the user is not a member of"
OTHER="$(python3 -c 'import uuid; print(uuid.uuid4())')"
status="$(request GET "/api/v1/billing/credits?org_id=$OTHER" "${AUTH[@]}")"
case "$status" in
  403|404) ;;
  *) fail "foreign org credits answered $status (expected 403/404): $(body)" ;;
esac

# ---- background work: the worker is alive, sized, and executes tasks --------
log "identity worker executes a task"
worker_health="$("${COMPOSE[@]}" ps --format '{{.Health}}' identity-worker 2>/dev/null || true)"
case "$worker_health" in
  healthy|starting) ;;
  *) fail "identity-worker health is '$worker_health'" ;;
esac
TASK_ID="$("${COMPOSE[@]}" exec -T identity-worker celery -A sandloom.core.worker call \
  sandloom_identity.tasks.cleanup_expired_refresh_tokens 2>/dev/null | tail -n 1 | tr -d '\r')"
[ -n "$TASK_ID" ] || fail "could not enqueue a worker task"
deadline=$(( $(date +%s) + 60 ))
until "${COMPOSE[@]}" logs --no-color identity-worker 2>/dev/null \
    | grep -q "\[$TASK_ID\] succeeded"; do
  [ "$(date +%s)" -ge "$deadline" ] && fail "task $TASK_ID did not succeed on the worker"
  sleep 2
done

# ---- a peer outage degrades only what needs that peer ------------------------
# service_probe <service> <path> -> HTTP status of the service's own endpoint
# (bypassing the gateway, which never exposes /ready).
service_probe() {
  "${COMPOSE[@]}" exec -T "$1" python -c "
import sys, urllib.error, urllib.request
try:
    print(urllib.request.urlopen('http://localhost:2222$2', timeout=10).status)
except urllib.error.HTTPError as exc:
    print(exc.code)
" 2>/dev/null | tr -d '\r'
}

log "stop billing: identity/tenancy stay live AND ready, login still works"
"${COMPOSE[@]}" stop billing >/dev/null 2>&1
for svc in identity tenancy; do
  for probe in /health /ready; do
    got="$(service_probe "$svc" "$probe")"
    [ "$got" = 200 ] || fail "$svc $probe answered $got while billing is down"
  done
done
status="$(request POST /auth/login -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}")"
[ "$status" = 200 ] || fail "login answered $status while billing is down: $(body)"
status="$(request GET /api/v1/orgs "${AUTH[@]}")"
[ "$status" = 200 ] || fail "tenancy /orgs answered $status while billing is down"
started=$(date +%s)
status="$(request GET /api/v1/billing/plans)"
elapsed=$(( $(date +%s) - started ))
case "$status" in 502|503|504) ;; *) fail "billing down answered $status" ;; esac
[ "$elapsed" -le 5 ] || fail "billing down took ${elapsed}s at the gateway (want fail-fast)"

log "recreate billing (new container): the gateway follows it without a restart"
"${COMPOSE[@]}" up -d --no-deps --force-recreate billing >/dev/null 2>&1
wait_for "billing (recreated)" GET /api/v1/billing/plans 200

log "OK"
