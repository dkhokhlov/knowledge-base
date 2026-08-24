#!/usr/bin/env bash
# System integration test: Open WebUI REST auth + chat completion.
# Exercises the signin/JWT path and the Open WebUI -> Ollama LLM path.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up
require_env OPENWEBUI_FIRST_USER OPENWEBUI_FIRST_PASSWORD || { finish; exit 1; }

# OWUI REST is at the KB_HOST root (/api/* via Caddy catch-all -> openwebui:8080).
O="$(kb_host)"
MODEL="${MODEL_NAME:-qwen2.5:14b}"

section "open webui signin (auth + JWT)"
jwt=$(curl -s -X POST "$O/api/v1/auths/signin" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"${OPENWEBUI_FIRST_USER}\",\"password\":\"${OPENWEBUI_FIRST_PASSWORD}\"}" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin).get("token", ""))' 2>/dev/null)
if [ -n "$jwt" ]; then
  pass "signin -> JWT obtained"
else
  fail "signin -> no JWT (check OPENWEBUI_FIRST_USER/OPENWEBUI_FIRST_PASSWORD)"
fi

section "open webui chat completion (-> Ollama ${MODEL})"
if [ -z "$jwt" ]; then fail "skipped: no JWT"; finish; exit 1; fi
resp=$(curl -s -X POST "$O/api/chat/completions" \
  -H "Authorization: Bearer $jwt" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the single word: ok\"}],\"stream\":false}")
content=$(printf '%s' "$resp" | python3 -c 'import sys, json
try:
    d = json.load(sys.stdin)
    print((d.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip())
except Exception:
    print("")' 2>/dev/null)
if [ -n "$content" ]; then
  pass "chat -> response: $(printf '%s' "$content" | head -c 60)"
else
  fail "chat -> empty (raw: $(printf '%s' "$resp" | head -c 200))"
fi

finish