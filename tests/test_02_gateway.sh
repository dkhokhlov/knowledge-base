#!/usr/bin/env bash
# System integration test: kb-gateway read path (replaces the old direct-MCP
# test). Exercises Caddy -> kb-gateway -> graphiti-mcp -> Neo4j over REST with
# the admin KB_API_KEY: whoami (identity from key), groups (Neo4j discovery),
# search (read-all facts), episodes, status. Read-only; no graph writes.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up
require_env OPENWEBUI_ADMIN_API_KEY || { finish; exit 1; }

G="http://localhost:${GRAPHITI_HOST_PORT:-8000}"
AUTH="Authorization: Bearer ${OPENWEBUI_ADMIN_API_KEY}"
CT="Content-Type: application/json"

gw() {  # gw <method> <path> [json-body]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -s -X "$method" "$G$path" -H "$AUTH" -H "$CT" -d "$body"
  else
    curl -s -X "$method" "$G$path" -H "$AUTH"
  fi
}
code() {  # code <method> <path> [json-body]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -s -o /dev/null -w '%{http_code}' -X "$method" "$G$path" -H "$AUTH" -H "$CT" -d "$body"
  else
    curl -s -o /dev/null -w '%{http_code}' -X "$method" "$G$path" -H "$AUTH"
  fi
}

section "kb-gateway whoami (identity from key, via OWUI)"
code=$(code GET /mem/whoami)
if [ "$code" = 200 ]; then
  body=$(gw GET /mem/whoami)
  role=$(printf '%s' "$body" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("role",""))' 2>/dev/null)
  [ "$role" = "admin" ] && pass "whoami -> admin role" || fail "whoami -> role=$role (want admin)"
else
  fail "whoami -> HTTP $code (want 200)"
fi

section "kb-gateway groups (Neo4j group discovery)"
code=$(code GET /mem/groups)
[ "$code" = 200 ] && pass "groups -> 200" || fail "groups -> HTTP $code (want 200)"

section "kb-gateway search (read-all facts across all groups)"
code=$(code POST /mem/search '{"query":"bootstrap test probe","k":5}')
[ "$code" = 200 ] && pass "search -> 200" || fail "search -> HTTP $code (want 200)"

section "kb-gateway episodes (read-all)"
code=$(code GET /mem/episodes)
[ "$code" = 200 ] && pass "episodes -> 200" || fail "episodes -> HTTP $code (want 200)"

section "kb-gateway status (graphiti server + DB, global)"
body=$(gw GET /mem/status)
if printf '%s' "$body" | grep -q '"status"'; then
  pass "status -> result"
else
  fail "status -> no result (raw: $(printf '%s' "$body" | head -c 200))"
fi

section "bad key -> 401"
code=$(curl -s -o /dev/null -w '%{http_code}' "$G/mem/whoami" -H "Authorization: Bearer not-a-real-key")
[ "$code" = 401 ] && pass "whoami with bad key -> 401" || fail "whoami with bad key -> $code (want 401)"

finish