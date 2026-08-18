#!/usr/bin/env bash
# System integration test: stack liveness, dev-mode docs, Neo4j exposure,
# and the Graphiti gateway token gate. No auth required.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up

G="http://localhost:${GRAPHITI_HOST_PORT:-8000}"
O="http://localhost:${OPENWEBUI_HOST_PORT:-3000}"

section "health endpoints (ungated)"
code=$(http_code "$G/health")
[ "$code" = 200 ] && pass "graphiti /health -> 200" || fail "graphiti /health -> $code (want 200)"
code=$(http_code "$O/health")
[ "$code" = 200 ] && pass "openwebui /health -> 200" || fail "openwebui /health -> $code (want 200)"

section "open webui dev-mode docs (read-only, no auth)"
code=$(http_code "$O/openapi.json")
[ "$code" = 200 ] && pass "openwebui /openapi.json -> 200" || fail "openwebui /openapi.json -> $code (want 200)"
if curl -sf "$O/openapi.json" | grep -q '"openapi"'; then
  pass "openapi.json has an openapi field"
else
  fail "openapi.json missing the openapi field"
fi

section "neo4j NOT published on the host"
# Nothing should listen on host 7474 (Neo4j is container-network only).
if curl -s -o /dev/null --connect-timeout 2 "http://localhost:7474" 2>/dev/null; then
  fail "neo4j :7474 is reachable on the host (should be internal-only)"
else
  pass "neo4j :7474 is not reachable on the host"
fi

section "graphiti gateway token gate"
# POST /mcp with no Authorization header must be rejected.
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$G/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
[ "$code" = 401 ] && pass "mcp without token -> 401" || fail "mcp without token -> $code (want 401)"

finish