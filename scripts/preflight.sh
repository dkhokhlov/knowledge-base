#!/usr/bin/env bash
# Read-only checks: docker compose plugin, .env.local + both secrets, ./data
# tree, host Ollama reachability, and required models. Exits non-zero on FAIL.
set -u

cd "$(dirname "$0")/.."

fail() { printf 'FAIL  %s\n' "$1" >&2; exit 1; }
ok()   { printf 'ok    %s\n' "$1"; }

command -v docker >/dev/null 2>&1 || fail "docker not found"
docker compose version >/dev/null 2>&1 || fail "docker compose plugin not found"
ok "docker compose available"

[ -f .env.local ] || fail ".env.local missing (run: make bootstrap)"
grep -qE '^WEBUI_SECRET_KEY=.+$' .env.local || fail "WEBUI_SECRET_KEY empty in .env.local (run: make bootstrap)"
grep -qE '^GRAPHITI_API_TOKEN=.+$' .env.local || fail "GRAPHITI_API_TOKEN empty in .env.local (set it before make start)"
ok "secrets present in .env.local"

[ -d data/neo4j/data ] && [ -d data/neo4j/logs ] && [ -d data/openwebui ] \
  || fail "./data tree incomplete (run: make bootstrap)"
ok "./data tree exists"

# Load config-of-record (non-secret).
set -a; . ./.env; set +a

OLLAMA="${OLLAMA_BASE_URL:-http://host.docker.internal:11434}"
# Probe the configured Ollama from the host side (it must be reachable from
# the host AND from the containers; compose points both services at $OLLAMA).
TAGS="$(curl -sf --connect-timeout 3 "${OLLAMA}/api/tags" 2>/dev/null)" \
  || fail "Ollama not reachable at ${OLLAMA} (is it running? check OLLAMA_BASE_URL in .env)"
ok "Ollama reachable at ${OLLAMA}"

MODEL_NAME="${MODEL_NAME:-qwen2.5:14b}"
echo "$TAGS" | grep -q "\"${MODEL_NAME}\"" \
  || fail "model '${MODEL_NAME}' not pulled in host Ollama (run: make pull-models)"
ok "Ollama has LLM model '${MODEL_NAME}'"
echo "$TAGS" | grep -qE '"nomic-embed-text(:|")' \
  || fail "model 'nomic-embed-text' not pulled in Ollama (run: make pull-models)"
ok "Ollama has embedder 'nomic-embed-text'"

printf '\nPreflight OK. Next: make start && make health\n'