#!/usr/bin/env bash
# Read-only checks: docker compose plugin, .env.local + both secrets, ./data
# tree, host Ollama reachability, and required models. Exits non-zero on FAIL.
set -u

cd "$(dirname "$0")/.."

fail() { printf 'FAIL  %s\n' "$1" >&2; exit 1; }
ok()   { printf 'ok    %s\n' "$1"; }
warn() { printf 'WARN  %s\n' "$1"; }

command -v docker >/dev/null 2>&1 || fail "docker not found"
docker compose version >/dev/null 2>&1 || fail "docker compose plugin not found"
ok "docker compose available"

[ -f .env.local ] || fail ".env.local missing (run: make bootstrap)"
# Source-and-test (unset inherited first) so "", '' , whitespace and '"" # c'
# all read as empty — same parse the Makefile start guard uses. A grep on the
# raw text would falsely accept '"" # comment'.
secrets_present() (
  unset WEBUI_SECRET_KEY GRAPHITI_API_TOKEN
  . ./.env.local 2>/dev/null || exit 1
  [ -n "${WEBUI_SECRET_KEY:-}" ] && [ -n "${GRAPHITI_API_TOKEN:-}" ]
)
secrets_present || fail ".env.local must contain non-empty WEBUI_SECRET_KEY and GRAPHITI_API_TOKEN (run: make bootstrap)"
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

# --- RAG embedding URL sync (read webui.db directly; works pre-start) --------
# Open WebUI persists rag.ollama.base_url in webui.db on first boot and ignores
# later .env OLLAMA_BASE_URL changes, so the embedder can drift to a stale host
# while chat still works. Read the persisted value straight from the DB (no OWUI
# up / admin key needed) and compare to .env. WARN, not a hard fail: a stale URL
# does not block `make start` (only embedding), and the fix (make rag-config)
# needs OWUI up. First boot (no webui.db / row absent) -> nothing to check.
emb_warn=0
if emb_msg="$(python3 - 2>&1 <<'PY'
import sqlite3, json, os, sys
env_url = (os.environ.get("OLLAMA_BASE_URL") or "http://host.docker.internal:11434").rstrip("/")
db = "./data/openwebui/webui.db"
if not os.path.exists(db):
    print("rag.ollama.base_url: webui.db not present (first boot) - nothing to sync")
    sys.exit(0)
try:
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    row = con.execute("SELECT value FROM config WHERE key='rag.ollama.base_url'").fetchone()
    con.close()
except Exception as e:
    print("rag.ollama.base_url: unreadable webui.db (%s) - skipping" % e)
    sys.exit(0)
if not row or not row[0]:
    print("rag.ollama.base_url: not yet persisted (first boot) - nothing to sync")
    sys.exit(0)
try:
    persisted = json.loads(row[0]).rstrip("/")
except Exception:
    print("rag.ollama.base_url: unreadable value %r - skipping" % (row[0],))
    sys.exit(0)
if persisted == env_url:
    print("rag.ollama.base_url in sync: %s" % persisted)
    sys.exit(0)
print("rag.ollama.base_url STALE: persisted=%r != .env OLLAMA_BASE_URL=%r (embedder will use the stale host)" % (persisted, env_url))
sys.exit(1)
PY
)"; then
  ok "$emb_msg"
else
  warn "$emb_msg"
  warn "       after 'make start', run: make rag-config  (syncs rag.ollama.base_url to .env OLLAMA_BASE_URL)"
  emb_warn=1
fi

if [ "$emb_warn" -eq 1 ]; then
  printf '\nPreflight OK (with the embedding-URL warning above). Next: make start && make rag-config && make health\n'
else
  printf '\nPreflight OK. Next: make start && make health\n'
fi