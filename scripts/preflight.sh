#!/usr/bin/env bash
# Read-only checks: docker compose plugin, .env.local + WEBUI_SECRET_KEY,
# ./data tree, host Ollama reachability, and required models. Exits non-zero
# on FAIL.
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
  unset WEBUI_SECRET_KEY
  . ./.env.local 2>/dev/null || exit 1
  [ -n "${WEBUI_SECRET_KEY:-}" ]
)
secrets_present || fail ".env.local must contain non-empty WEBUI_SECRET_KEY (run: make bootstrap)"
ok "secrets present in .env.local"

[ -d data/neo4j/data ] && [ -d data/neo4j/logs ] && [ -d data/openwebui ] \
  || fail "./data tree incomplete (run: make bootstrap)"
ok "./data tree exists"

# Load config-of-record (non-secret).
set -a; . ./.env; set +a

OLLAMA="${OLLAMA_HOST:-http://host.docker.internal:11434}"
# Probe the configured Ollama from the host side (it must be reachable from
# the host AND from the containers; compose points both services at $OLLAMA).
TAGS="$(curl -sf --connect-timeout 3 "${OLLAMA}/api/tags" 2>/dev/null)" \
  || fail "Ollama not reachable at ${OLLAMA} (is it running? check OLLAMA_HOST in shell env or .env)"
ok "Ollama reachable at ${OLLAMA}"

MODEL_NAME="${MODEL_NAME:-qwen2.5:14b-ctx8192}"
echo "$TAGS" | grep -q "\"${MODEL_NAME}\"" \
  || fail "model '${MODEL_NAME}' not pulled in host Ollama (run: make pull-models)"
ok "Ollama has LLM model '${MODEL_NAME}'"
# Verify the ctx variant's num_ctx matches OLLAMA_MODEL_CONTEXT. The /v1
# endpoint ignores options.num_ctx in requests, so num_ctx is baked into the
# model via a Modelfile (PARAMETER num_ctx) at make pull-models. A wrong or
# absent value means the model loads at the default 32k (~53 GB) and spills
# to CPU (extraction crawls) — so this is a hard fail, not a warning.
EXP_CTX="${OLLAMA_MODEL_CONTEXT:-8192}"
GOT_CTX="$(curl -sf --connect-timeout 3 "${OLLAMA}/api/show" -d "{\"name\":\"${MODEL_NAME}\"}" 2>/dev/null \
  | python3 -c 'import sys,json,re; p=json.load(sys.stdin).get("parameters",""); m=re.search(r"(?m)^\s*num_ctx\s+(\d+)", p); print(m.group(1) if m else "")' 2>/dev/null)"
[ -n "$GOT_CTX" ] \
  || fail "model '${MODEL_NAME}' has no num_ctx (not a ctx variant — run: make pull-models)"
[ "$GOT_CTX" = "$EXP_CTX" ] \
  || fail "model '${MODEL_NAME}' num_ctx=${GOT_CTX} != OLLAMA_MODEL_CONTEXT=${EXP_CTX} (re-run: make pull-models)"
ok "model '${MODEL_NAME}' num_ctx=${GOT_CTX}"
echo "$TAGS" | grep -qE '"nomic-embed-text(:|")' \
  || fail "model 'nomic-embed-text' not pulled in Ollama (run: make pull-models)"
ok "Ollama has embedder 'nomic-embed-text'"

# --- RAG embedding URL sync (read webui.db directly; works pre-start) --------
# Open WebUI persists rag.ollama.base_url in webui.db on first boot and ignores
# later OLLAMA_HOST changes, so the embedder can drift to a stale host
# while chat still works. Read the persisted value straight from the DB (no OWUI
# up / admin key needed) and compare to .env. WARN, not a hard fail: a stale URL
# does not block `make start` (only embedding), and the fix (make rag-config)
# needs OWUI up. First boot (no webui.db / row absent) -> nothing to check.
emb_warn=0
if emb_msg="$(python3 - 2>&1 <<'PY'
import sqlite3, json, os, sys
env_url = (os.environ.get("OLLAMA_HOST") or "http://host.docker.internal:11434").rstrip("/")
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
print("rag.ollama.base_url STALE: persisted=%r != OLLAMA_HOST=%r (embedder will use the stale host)" % (persisted, env_url))
sys.exit(1)
PY
)"; then
  ok "$emb_msg"
else
  warn "$emb_msg"
  warn "       after 'make start', run: make rag-config  (syncs rag.ollama.base_url to OLLAMA_HOST)"
  emb_warn=1
fi

if [ "$emb_warn" -eq 1 ]; then
  printf '\nPreflight OK (with the embedding-URL warning above). Next: make start && make rag-config && make health\n'
else
  printf '\nPreflight OK. Next: make start && make health\n'
fi