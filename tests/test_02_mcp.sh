#!/usr/bin/env bash
# System integration test: Graphiti MCP Streamable HTTP session (read path).
# Exercises the gateway token gate (positive), MCP initialize, tools/list,
# and get_episodes (the graphiti -> Neo4j read path). Read-only; no graph writes.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up
require_env GRAPHITI_API_TOKEN || { finish; exit 1; }

G="http://localhost:${GRAPHITI_HOST_PORT:-8000}"
AUTH="Authorization: Bearer ${GRAPHITI_API_TOKEN}"
ACCEPT="Accept: application/json, text/event-stream"
CT="Content-Type: application/json"

section "mcp initialize"
# initialize -> 200 SSE + an Mcp-Session-Id response header.
hdrs=$(curl -s -D - -o /dev/null -w '\n%{http_code}' -X POST "$G/mcp" \
  -H "$AUTH" -H "$ACCEPT" -H "$CT" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"itest","version":"1"}}}')
code=$(printf '%s' "$hdrs" | tail -1)
sid=$(printf '%s' "$hdrs" | tr -d '\r' | awk 'tolower($0) ~ /^mcp-session-id:/ {print $2}')
if [ "$code" = 200 ] && [ -n "$sid" ]; then
  pass "initialize -> 200 with a session id"
else
  fail "initialize -> code=$code sid=[$sid]"
fi

section "mcp notifications/initialized"
# No JSON-RPC response is expected; accept 200/202/204.
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$G/mcp" \
  -H "$AUTH" -H "$ACCEPT" -H "$CT" -H "Mcp-Session-Id: $sid" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}')
case "$code" in
  200|202|204) pass "notifications/initialized -> $code" ;;
  *) fail "notifications/initialized -> $code" ;;
esac

section "mcp tools/list"
body=$(curl -s -X POST "$G/mcp" \
  -H "$AUTH" -H "$ACCEPT" -H "$CT" -H "Mcp-Session-Id: $sid" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
tools=$(printf '%s' "$body" | grep -o '"name"[[:space:]]*:[[:space:]]*"[^"]*"' | wc -l | tr -d ' ')
if [ "$tools" -ge 1 ] 2>/dev/null; then
  pass "tools/list returned $tools tool(s)"
else
  fail "tools/list returned no tools (raw: $(printf '%s' "$body" | head -c 200))"
fi

section "mcp tools/call get_episodes (graphiti -> neo4j read)"
body=$(curl -s -X POST "$G/mcp" \
  -H "$AUTH" -H "$ACCEPT" -H "$CT" -H "Mcp-Session-Id: $sid" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_episodes","arguments":{}}}')
# A success returns a JSON-RPC "result"; an error returns "error".
if printf '%s' "$body" | grep -q '"result"'; then
  pass "get_episodes -> result"
else
  fail "get_episodes -> no result (raw: $(printf '%s' "$body" | head -c 200))"
fi

finish