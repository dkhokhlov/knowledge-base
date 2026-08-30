#!/usr/bin/env bash
# System integration test: rag-config.sh re-asserts the hybrid-retrieval config
# (ENABLE_RAG_HYBRID_SEARCH, HYBRID_BM25_WEIGHT, TOP_K_RERANKER) over webui.db,
# and the OWUI container sees VECTOR_DB=pgvector. OWUI persists rag.* in webui.db
# on first boot and ignores later env changes, so the script is the reconcile
# path; this test proves it sticks and that the pgvector backend is active.
# Read-then-write via the admin key. Idempotent (safe to re-run).
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up
require_env OPENWEBUI_ADMIN_API_KEY OPENWEBUI_USER_API_KEY || { finish; exit 1; }
# Hybrid keys are .env single-source (no literal defaults); rag-config.sh fails
# loudly if any is missing. Fail here too so the test does not mask an unset var.
require_env ENABLE_RAG_HYBRID_SEARCH RAG_HYBRID_BM25_WEIGHT RAG_TOP_K_RERANKER VECTOR_DB \
  || { finish; exit 1; }

G="$(kb_host)"
AUTH="Authorization: Bearer ${OPENWEBUI_ADMIN_API_KEY}"
CT="Content-Type: application/json"
OWUI_CTN="${OWUI_CONTAINER:-kb-openwebui}"

section "rag-config.sh re-asserts hybrid keys over webui.db"
out=$(bash scripts/rag-config.sh 2>&1)
rc=$?
if [ "$rc" -ne 0 ]; then
  fail "rag-config.sh exited $rc: $out"
  finish; exit 1
fi
printf '%s\n' "$out" | grep -q 'HYBRID_BM25_WEIGHT' \
  && pass "rag-config.sh reported hybrid config" \
  || fail "rag-config.sh output missing the hybrid line"

section "GET /api/v1/retrieval/config matches .env hybrid keys"
# OWUI /retrieval/config drops the RAG_ prefix for the latter two keys
# (HYBRID_BM25_WEIGHT, TOP_K_RERANKER); ENABLE_RAG_HYBRID_SEARCH keeps its name.
# Compare in python so bool/int vs string do not cause false mismatches.
body=$(curl -s "$G/api/v1/retrieval/config" -H "$AUTH")
python3 - "$body" <<'PY'
import json, os, sys
body = sys.argv[1]
try:
    d = json.loads(body)
except (TypeError, ValueError) as e:
    print("FAIL  GET /api/v1/retrieval/config invalid JSON: %s" % e)
    sys.exit(1)
env_hybrid = os.environ["ENABLE_RAG_HYBRID_SEARCH"].strip().lower() in ("1", "true", "yes", "on")
env_bw = float(os.environ["RAG_HYBRID_BM25_WEIGHT"])
env_tkr = int(os.environ["RAG_TOP_K_RERANKER"])
ok = True
if d.get("ENABLE_RAG_HYBRID_SEARCH") is not env_hybrid:
    print("FAIL  ENABLE_RAG_HYBRID_SEARCH=%r (want %r)" % (d.get("ENABLE_RAG_HYBRID_SEARCH"), env_hybrid)); ok = False
if float(d.get("HYBRID_BM25_WEIGHT")) != env_bw:
    print("FAIL  HYBRID_BM25_WEIGHT=%r (want %r)" % (d.get("HYBRID_BM25_WEIGHT"), env_bw)); ok = False
if int(d.get("TOP_K_RERANKER")) != env_tkr:
    print("FAIL  TOP_K_RERANKER=%r (want %r)" % (d.get("TOP_K_RERANKER"), env_tkr)); ok = False
if ok:
    print("OK    hybrid keys match .env: HYBRID=%s BM25_W=%s TOP_K_RERANKER=%s"
          % (env_hybrid, env_bw, env_tkr))
else:
    sys.exit(1)
PY
[ $? -eq 0 ] && pass "hybrid config matches .env" || fail "hybrid config drift from .env"

section "OWUI container runs the pgvector backend"
vd=$(docker exec "$OWUI_CTN" printenv VECTOR_DB 2>/dev/null | tr -d '\r\n')
[ "$vd" = "pgvector" ] \
  && pass "VECTOR_DB=pgvector in the OWUI container" \
  || fail "VECTOR_DB in OWUI container='$vd' (want pgvector)"

finish