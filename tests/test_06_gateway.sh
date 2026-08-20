#!/usr/bin/env bash
# System integration test: kb-gateway authorization matrix.
# Uses the admin key (role=admin) + the shared-agent key (role=user). Both
# identities come from the keys via the gateway (tamper-proof). Exercises:
#   - personal write: agent adds to user:<agent> -> 200
#   - read-all: search/episodes span all discovered groups -> 200
#   - cross-user deny: agent forgets user:<admin> -> 403
#   - admin override: admin forgets user:<agent> -> 200
#   - spoof/claim deny: agent add --group user:<admin> -> 403 (ownership enforced
#     server-side; the client cannot write to another user's group by claiming it)
#   - shared-group deny: agent add --group unknown-shared -> 403 (no shared
#     write groups; only the caller's own personal group is writable)
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up
require_env OPENWEBUI_ADMIN_API_KEY OPENWEBUI_USER_API_KEY || { finish; exit 1; }

G="http://localhost:${GRAPHITI_HOST_PORT:-8000}"
ADMIN="Authorization: Bearer ${OPENWEBUI_ADMIN_API_KEY}"
USER="Authorization: Bearer ${OPENWEBUI_USER_API_KEY}"
CT="Content-Type: application/json"

whoami_email() {  # whoami_email <auth-header> -> email
  curl -s "$G/mem/whoami" -H "$1" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("email",""))' 2>/dev/null
}
gwcode() {  # gwcode <auth-header> <method> <path> [json-body]
  local auth="$1" method="$2" path="$3" body="${4:-}"
  if [ -n "$body" ]; then
    curl -s -o /dev/null -w '%{http_code}' -X "$method" "$G$path" -H "$auth" -H "$CT" -d "$body"
  else
    curl -s -o /dev/null -w '%{http_code}' -X "$method" "$G$path" -H "$auth"
  fi
}

ADMIN_EMAIL=$(whoami_email "$ADMIN")
AGENT_EMAIL=$(whoami_email "$USER")
if [ -z "$ADMIN_EMAIL" ] || [ -z "$AGENT_EMAIL" ]; then
  fail "could not resolve admin/agent emails via whoami"; finish; exit 1
fi
AGENT_GROUP="user:${AGENT_EMAIL}"
ADMIN_GROUP="user:${ADMIN_EMAIL}"
pass "identities resolved: admin=${ADMIN_EMAIL} agent=${AGENT_EMAIL}"

section "personal write (agent -> own group)"
code=$(gwcode "$USER" POST /mem/add "{\"text\":\"t06 probe $(date +%s)\",\"name\":\"t06\"}")
[ "$code" = 200 ] && pass "agent add to ${AGENT_GROUP} -> 200" || fail "agent add -> $code (want 200)"

section "read-all (agent search + episodes across all groups)"
code=$(gwcode "$USER" POST /mem/search '{"query":"t06 probe","k":5}')
[ "$code" = 200 ] && pass "agent search -> 200" || fail "agent search -> $code (want 200)"
code=$(gwcode "$USER" GET /mem/episodes)
[ "$code" = 200 ] && pass "agent episodes -> 200" || fail "agent episodes -> $code (want 200)"

section "cross-user deny (agent cannot forget admin's personal group)"
code=$(gwcode "$USER" POST /mem/forget "{\"group\":\"${ADMIN_GROUP}\"}")
[ "$code" = 403 ] && pass "agent forget ${ADMIN_GROUP} -> 403" || fail "agent forget ${ADMIN_GROUP} -> $code (want 403)"

section "spoof/claim deny (agent add --group admin's personal -> 403)"
code=$(gwcode "$USER" POST /mem/add "{\"text\":\"spoof\",\"name\":\"s\",\"group\":\"${ADMIN_GROUP}\"}")
[ "$code" = 403 ] && pass "agent add to ${ADMIN_GROUP} -> 403" || fail "agent add to ${ADMIN_GROUP} -> $code (want 403)"

section "shared-group deny (agent add --group unknown-shared -> 403)"
code=$(gwcode "$USER" POST /mem/add '{"text":"x","name":"x","group":"no-such-shared-group"}')
[ "$code" = 403 ] && pass "agent add to unknown shared group -> 403" || fail "agent add unknown shared -> $code (want 403)"

section "admin override (admin forgets agent's group -> 200)"
code=$(gwcode "$ADMIN" POST /mem/forget "{\"group\":\"${AGENT_GROUP}\"}")
[ "$code" = 200 ] && pass "admin forget ${AGENT_GROUP} -> 200" || fail "admin forget ${AGENT_GROUP} -> $code (want 200)"

# Cleanup: ensure the agent group is empty (idempotent).
gwcode "$ADMIN" POST /mem/forget "{\"group\":\"${AGENT_GROUP}\"}" >/dev/null

finish