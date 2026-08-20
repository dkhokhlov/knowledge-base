#!/usr/bin/env bash
# System integration test: stack liveness, dev-mode docs, Neo4j exposure,
# and the kb-gateway auth gate (via Caddy). No auth required.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up

G="http://localhost:${GRAPHITI_HOST_PORT:-8000}"
O="http://localhost:${OPENWEBUI_HOST_PORT:-3000}"

section "health endpoints (ungated)"
# :8000 is Caddy -> kb-gateway /health (which reflects the OWUI identity dep).
code=$(http_code "$G/health")
[ "$code" = 200 ] && pass "kb-gateway /health (via Caddy) -> 200" || fail "kb-gateway /health -> $code (want 200)"
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
# Nothing should listen on host 7474 (Neo4j is graph_internal only).
if curl -s -o /dev/null --connect-timeout 2 "http://localhost:7474" 2>/dev/null; then
  fail "neo4j :7474 is reachable on the host (should be internal-only)"
else
  pass "neo4j :7474 is not reachable on the host"
fi

section "kb-gateway auth gate (via Caddy)"
# A gateway authed endpoint without Authorization must be rejected with 401.
code=$(curl -s -o /dev/null -w '%{http_code}' "$G/mem/whoami")
[ "$code" = 401 ] && pass "mem/whoami without key -> 401" || fail "mem/whoami without key -> $code (want 401)"

finish