#!/usr/bin/env bash
# Read-only checks: docker compose plugin, .env.local + WEBUI_SECRET_KEY,
# ./data tree, host Ollama reachability, and required models. Exits non-zero
# on FAIL.
set -euo pipefail

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
  && [ -d data/postgres ] \
  || fail "./data tree incomplete (run: make bootstrap)"
ok "./data tree exists"

# Load config-of-record (non-secret). Capture a `make preflight
# OCR_ENABLED=<val>` override before sourcing .env (which would clobber it);
# restored so the OCR block below honors the override.
_OCR_ENABLED_OVR="${OCR_ENABLED:-}"
set -a; . ./.env; set +a
# .env's OCR_ENABLED (post-source, before the override is restored) for the
# COMPOSE_PROFILES consistency guard below. The guard asserts .env internal
# consistency (bootstrap drift/migration), independent of a transient
# `make preflight OCR_ENABLED=<val>` override, so it must compare the persisted
# .env value against COMPOSE_PROFILES -- not the override (which does not, and
# must not, rewrite COMPOSE_PROFILES).
_ENV_OCR_ENABLED="${OCR_ENABLED:-true}"
if [ -n "$_OCR_ENABLED_OVR" ]; then export OCR_ENABLED="$_OCR_ENABLED_OVR"; fi

# --- pgvector config drift guards (.env values are literal: no ${} expansion) --
# VECTOR_DB switches the OWUI vector store to Postgres+pgvector. The openwebui
# compose service fails loud on a missing value (:?); preflight mirrors that and
# adds two consistency checks the literal .env cannot express with ${}:
#   1. PGVECTOR_DB_URL must match PGVECTOR_USER/PASSWORD/DB (hand-kept in sync).
#   2. PGVECTOR_INITIALIZE_MAX_VECTOR_LENGTH must equal EMBEDDER_DIMENSIONS (the
#      vector column width is fixed at table creation; a mismatch zero-pads or
#      rejects inserts).
# RAG_TOP_K_RERANKER must be >= KB_RETRIEVE_K_MAX or a large-k /retrieve request
# is truncated by the reranker candidate cap.
[ "${VECTOR_DB:-}" = "pgvector" ] || fail "VECTOR_DB must be pgvector in .env (got '${VECTOR_DB:-<unset>}'; Chroma was removed, pgvector is the only backend)"
: "${PGVECTOR_USER:?PGVECTOR_USER required in .env}"
: "${PGVECTOR_PASSWORD:?PGVECTOR_PASSWORD required in .env}"
: "${PGVECTOR_DB:?PGVECTOR_DB required in .env}"
: "${PGVECTOR_DB_URL:?PGVECTOR_DB_URL required in .env}"
: "${PGVECTOR_INITIALIZE_MAX_VECTOR_LENGTH:?PGVECTOR_INITIALIZE_MAX_VECTOR_LENGTH required in .env}"
: "${KB_RETRIEVE_K_MAX:?KB_RETRIEVE_K_MAX required in .env}"
: "${RAG_TOP_K_RERANKER:?RAG_TOP_K_RERANKER required in .env}"
_want="postgresql://${PGVECTOR_USER}:${PGVECTOR_PASSWORD}@postgres:5432/${PGVECTOR_DB}"
[ "${PGVECTOR_DB_URL}" = "$_want" ] \
  || fail "PGVECTOR_DB_URL does not match PGVECTOR_USER/PASSWORD/DB (.env values are literal; edit both)"
ok "PGVECTOR_DB_URL agrees with PGVECTOR_USER/PASSWORD/DB"
[ "${PGVECTOR_INITIALIZE_MAX_VECTOR_LENGTH}" = "${EMBEDDER_DIMENSIONS:-}" ] \
  || fail "PGVECTOR_INITIALIZE_MAX_VECTOR_LENGTH=${PGVECTOR_INITIALIZE_MAX_VECTOR_LENGTH} != EMBEDDER_DIMENSIONS=${EMBEDDER_DIMENSIONS:-<unset>} (must equal the embedder dim)"
ok "PGVECTOR_INITIALIZE_MAX_VECTOR_LENGTH == EMBEDDER_DIMENSIONS (${PGVECTOR_INITIALIZE_MAX_VECTOR_LENGTH})"
[ "${RAG_TOP_K_RERANKER}" -ge "${KB_RETRIEVE_K_MAX}" ] \
  || fail "RAG_TOP_K_RERANKER=${RAG_TOP_K_RERANKER} < KB_RETRIEVE_K_MAX=${KB_RETRIEVE_K_MAX} (large-k /retrieve requests would be truncated)"
ok "RAG_TOP_K_RERANKER >= KB_RETRIEVE_K_MAX (${RAG_TOP_K_RERANKER} >= ${KB_RETRIEVE_K_MAX})"

# --- RAG embedding concurrency (herd bound) ---
# RAG_EMBEDDING_BATCH_SIZE packs N chunks per /api/embed. Default 1 fired one
# request per chunk and killed Ollama's embed runner under a 5195-chunk file.
# RAG_EMBEDDING_CONCURRENT_REQUESTS caps in-flight batches per file (0 = unlimited
# thundering herd). Both must be >= 1; 0 reproduces the runner EOF.
: "${RAG_EMBEDDING_BATCH_SIZE:?RAG_EMBEDDING_BATCH_SIZE required in .env}"
[ "${RAG_EMBEDDING_BATCH_SIZE}" -ge 1 ] \
  || fail "RAG_EMBEDDING_BATCH_SIZE=${RAG_EMBEDDING_BATCH_SIZE} must be >= 1 (default 1 fired one request per chunk)"
ok "RAG_EMBEDDING_BATCH_SIZE=${RAG_EMBEDDING_BATCH_SIZE}"
: "${RAG_EMBEDDING_CONCURRENT_REQUESTS:?RAG_EMBEDDING_CONCURRENT_REQUESTS required in .env}"
[ "${RAG_EMBEDDING_CONCURRENT_REQUESTS}" -ge 1 ] \
  || fail "RAG_EMBEDDING_CONCURRENT_REQUESTS=${RAG_EMBEDDING_CONCURRENT_REQUESTS} must be >= 1 (0 = unlimited thundering herd)"
ok "RAG_EMBEDDING_CONCURRENT_REQUESTS=${RAG_EMBEDDING_CONCURRENT_REQUESTS}"
: "${ENABLE_ASYNC_EMBEDDING:?ENABLE_ASYNC_EMBEDDING required in .env}"
ok "ENABLE_ASYNC_EMBEDDING=${ENABLE_ASYNC_EMBEDDING}"
: "${THREAD_POOL_SIZE:?THREAD_POOL_SIZE required in .env}"
ok "THREAD_POOL_SIZE=${THREAD_POOL_SIZE}"
: "${AIOHTTP_CLIENT_SESSION_SSL:?AIOHTTP_CLIENT_SESSION_SSL required in .env}"
ok "AIOHTTP_CLIENT_SESSION_SSL=${AIOHTTP_CLIENT_SESSION_SSL}"
: "${RAG_RERANKING_BATCH_SIZE:?RAG_RERANKING_BATCH_SIZE required in .env}"
ok "RAG_RERANKING_BATCH_SIZE=${RAG_RERANKING_BATCH_SIZE}"

: "${OLLAMA_HOST:?OLLAMA_HOST is required (set in shell env or uncomment in .env; see .env.template)}"
OLLAMA="${OLLAMA_HOST%/}"
# Probe the configured Ollama from the host side (it must be reachable from
# the host AND from the containers; compose points both services at $OLLAMA).
TAGS="$(curl -sf --connect-timeout 3 "${OLLAMA}/api/tags" 2>/dev/null)" \
  || fail "Ollama not running at ${OLLAMA} (is it up? check OLLAMA_HOST in shell env or .env)"
ok "Ollama running at ${OLLAMA}"

GRAPHITI_MODEL="${GRAPHITI_MODEL:-qwen2.5:14b-ctx8192}"
echo "$TAGS" | grep -q "\"${GRAPHITI_MODEL}\"" \
  || fail "model '${GRAPHITI_MODEL}' not pulled in host Ollama (run: make pull-models)"
ok "Ollama has LLM model '${GRAPHITI_MODEL}'"
# Verify the ctx variant's num_ctx matches OLLAMA_MODEL_CONTEXT. The /v1
# endpoint ignores options.num_ctx in requests, so num_ctx is baked into the
# model via a Modelfile (PARAMETER num_ctx) at make pull-models. A wrong or
# absent value means the model loads at the default 32k (~53 GB) and spills
# to CPU (extraction crawls) — so this is a hard fail, not a warning.
EXP_CTX="${OLLAMA_MODEL_CONTEXT:-8192}"
GOT_CTX="$(curl -sf --connect-timeout 3 "${OLLAMA}/api/show" -d "{\"name\":\"${GRAPHITI_MODEL}\"}" 2>/dev/null \
  | python3 -c 'import sys,json,re; p=json.load(sys.stdin).get("parameters",""); m=re.search(r"(?m)^\s*num_ctx\s+(\d+)", p); print(m.group(1) if m else "")' 2>/dev/null)" || true
[ -n "$GOT_CTX" ] \
  || fail "model '${GRAPHITI_MODEL}' has no num_ctx (not a ctx variant — run: make pull-models)"
[ "$GOT_CTX" = "$EXP_CTX" ] \
  || fail "model '${GRAPHITI_MODEL}' num_ctx=${GOT_CTX} != OLLAMA_MODEL_CONTEXT=${EXP_CTX} (re-run: make pull-models)"
ok "model '${GRAPHITI_MODEL}' num_ctx=${GOT_CTX}"
echo "$TAGS" | grep -qE '"nomic-embed-text(:|")' \
  || fail "model 'nomic-embed-text' not pulled in Ollama (run: make pull-models)"
ok "Ollama has embedder 'nomic-embed-text'"

# --- RAG embedding URL sync (read webui.db directly; works pre-start) --------
# Open WebUI persists rag.ollama.base_url in webui.db on first boot and ignores
# later OLLAMA_HOST changes, so the embedder can drift to a stale host
# while chat still works. Read the persisted value straight from the DB (no OWUI
# up / admin key needed) and compare to .env. WARN, not a hard fail: a stale URL
# does not block `make start` (only embedding), and the fix (make config-rag)
# needs OWUI up. First boot (no webui.db / row absent) -> nothing to check.
emb_warn=0
if emb_msg="$(python3 - 2>&1 <<'PY'
import sqlite3, json, os, re, sys
env_url = (os.environ.get("OLLAMA_HOST") or "http://host.docker.internal:11434").rstrip("/")
# OWUI persists the CONTAINER-reachable URL (host.docker.internal, not
# localhost) in webui.db, so apply the same localhost->host.docker.internal
# translation the container entrypoint shim (scripts/ollama-host.sh) and
# config-rag apply before comparing. Without this, a DB holding
# host.docker.internal vs OLLAMA_HOST=localhost reads as a false STALE.
env_url = re.sub(r'(https?://)(localhost|127\.0\.0\.1)([:/]|$)', r'\1host.docker.internal\3', env_url)
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
  warn "       after 'make start', run: make config-rag  (syncs rag.ollama.base_url to OLLAMA_HOST)"
  emb_warn=1
fi

if [ "$emb_warn" -eq 1 ]; then
  printf '\nPreflight OK (with the embedding-URL warning above). Next: make start && make config-rag && make health\n'
else
  printf '\nPreflight OK. Next: make start && make health\n'
fi

# --- markitdown-ocr (OCR_ENABLED) -----------------------------------------
# OCR_ENABLED (default true; overridable via `make preflight OCR_ENABLED=<val>`)
# gates the OCR prereq checks. When enabled, HARD-FAIL if the OCR vision model
# is not pulled (a half-provisioned engine silently orphans image docs), and
# WARN on rag.content_extraction_engine drift (OWUI not routed to the engine).
# When disabled, skip (the sidecar is not part of the stack).
#
# COMPOSE_PROFILES (the ocr sidecar compose profile) is derived from OCR_ENABLED
# by `make bootstrap` and read by `docker compose` from .env for EVERY command.
# Assert the two agree so a stale/migrated .env (an existing deployment that
# pulled this commit without re-running `make bootstrap`) or a hand-edit cannot
# silently drop the sidecar or start it with no token/routing. The old start.sh
# re-derived --profile ocr every run; this guard is the replacement self-heal --
# it fails loud instead of starting a broken stack.
case ",${COMPOSE_PROFILES:-}," in
  *,ocr,*) _cp_ocr=1 ;; *) _cp_ocr=0 ;;
esac
if [ "$_ENV_OCR_ENABLED" = "true" ] && [ "$_cp_ocr" -ne 1 ]; then
  fail "OCR_ENABLED=true but COMPOSE_PROFILES lacks 'ocr' (markitdown-ocr would be dropped on start). Run: make bootstrap"
elif [ "$_ENV_OCR_ENABLED" != "true" ] && [ "$_cp_ocr" -eq 1 ]; then
  fail "OCR_ENABLED!=true but COMPOSE_PROFILES=ocr (markitdown-ocr would start with no token/routing). Run: make bootstrap"
fi
ok "COMPOSE_PROFILES matches OCR_ENABLED (${_ENV_OCR_ENABLED} -> ${COMPOSE_PROFILES:-<empty>})"
if [ "${OCR_ENABLED:-true}" = "true" ]; then
  OCR_MODEL="${OCR_MODEL:-deepseek-ocr}"
  # Match "deepseek-ocr" or "deepseek-ocr:latest" (ollama pull appends :latest;
  # the bare "\"${OCR_MODEL}\"" pattern misses the :latest form -> false fail).
  echo "$TAGS" | grep -qE "\"${OCR_MODEL}(:|\")" \
    || fail "OCR model '${OCR_MODEL}' not pulled in host Ollama (OCR_ENABLED=true; run: make pull-models)"
  ok "Ollama has OCR model '${OCR_MODEL}'"
  if ocr_msg="$(python3 - 2>&1 <<'PY'
import sqlite3, json, os, sys
db = "./data/openwebui/webui.db"
if not os.path.exists(db):
    print("rag.content_extraction_engine: webui.db not present (first boot) - nothing to check")
    sys.exit(0)
try:
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    row = con.execute("SELECT value FROM config WHERE key='rag.content_extraction_engine'").fetchone()
    con.close()
except Exception as e:
    print("rag.content_extraction_engine: unreadable webui.db (%s) - skipping" % e)
    sys.exit(0)
if not row or not row[0]:
    print("rag.content_extraction_engine: not yet persisted (first boot) - nothing to check")
    sys.exit(0)
try:
    val = json.loads(row[0])
except Exception:
    val = row[0]
if val == "external":
    print("rag.content_extraction_engine=external (in sync)")
    sys.exit(0)
print("rag.content_extraction_engine DRIFT: persisted=%r != 'external' (OWUI will not use the engine)" % (val,))
sys.exit(1)
PY
)"; then
    ok "$ocr_msg"
  else
    warn "$ocr_msg"
    warn "       after 'make start', run: make config-ocr  (re-asserts CONTENT_EXTRACTION_ENGINE=external)"
  fi
else
  ok "OCR disabled (OCR_ENABLED=false) — skipping OCR checks"
fi