#!/usr/bin/env bash
# System integration test: Open WebUI RAG embedding endpoint + upload/embed/search.
#
# Regression guard: Open WebUI persists the RAG embedding Ollama URL
# (`rag.ollama.base_url`) in webui.db on first boot. A later change to
# OLLAMA_HOST (shell env or .env) does NOT override the persisted value, so the
# embedder can keep pointing at a stale, unreachable host while chat still
# works. Symptom: file upload succeeds, /api/v1/files/{id}/process/status
# reports "failed", and /api/v1/retrieval/query/collection returns 0 hits.
#
# This test catches that by (1) probing the configured embedding URL from
# inside the Open WebUI container (where the embedder runs) and (2) running
# the full upload -> embed -> bind -> semantic-search flow and asserting hits.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up
require_env OPENWEBUI_TEST_USER OPENWEBUI_TEST_PASSWORD || { finish; exit 1; }

# OWUI REST is at the KB_HOST root (/api/* via Caddy catch-all -> openwebui:8080).
O="$(kb_host)"
OWUI_CTN="${OWUI_CONTAINER:-kb-openwebui}"
# Unique marker so this test's doc is found only by its own query.
MARKER="kbregrag-7f3a2-quasiparticle"
TMP_TXT="$(mktemp)"
trap 'rm -f "$TMP_TXT"; [ -n "${KB_ID:-}" ] && curl -sf -X DELETE "$O/api/v1/knowledge/${KB_ID}/delete" -H "Authorization: Bearer $jwt" >/dev/null 2>&1 || true; [ -n "${FID:-}" ] && curl -sf -X DELETE "$O/api/v1/files/${FID}" -H "Authorization: Bearer $jwt" >/dev/null 2>&1 || true' EXIT

cat >"$TMP_TXT" <<EOF
KnowledgeBase RAG regression marker: ${MARKER}.
An exciton-polariton is a quasiparticle that forms when a photon strongly
couples to an exciton in a semiconductor microcavity. This document exists
only so the integration test can verify that the embedding endpoint is
reachable and that vector search returns the indexed text.
EOF

# --- signin -> JWT -----------------------------------------------------------
section "open webui signin"
jwt=$(curl -s -X POST "$O/api/v1/auths/signin" -H 'Content-Type: application/json' \
  -d "{\"email\":\"${OPENWEBUI_TEST_USER}\",\"password\":\"${OPENWEBUI_TEST_PASSWORD}\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))' 2>/dev/null)
if [ -n "$jwt" ]; then pass "signin -> JWT obtained"; else fail "signin -> no JWT"; finish; exit 1; fi
AUTH=(-H "Authorization: Bearer $jwt")

# --- regression check: embedding URL reachable from the container -----------
section "rag embedding endpoint reachable (container-side)"
emb=$(curl -s "$O/api/v1/retrieval/embedding" "${AUTH[@]}")
emb_url=$(printf '%s' "$emb" | python3 -c 'import sys,json;print(json.load(sys.stdin)["ollama_config"]["url"])' 2>/dev/null)
emb_model=$(printf '%s' "$emb" | python3 -c 'import sys,json;print(json.load(sys.stdin)["RAG_EMBEDDING_MODEL"])' 2>/dev/null)
if [ -z "$emb_url" ]; then fail "could not read rag.ollama.base_url from /api/v1/retrieval/embedding"; finish; exit 1; fi
code=$(docker exec "$OWUI_CTN" sh -c "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 '${emb_url}/api/tags'" 2>/dev/null)
if [ "$code" = "200" ]; then
  pass "embedding URL reachable from container: ${emb_url} (model=${emb_model})"
else
  fail "embedding URL UNREACHABLE from container: ${emb_url} -> HTTP ${code}"
  fail "regression: persisted rag.ollama.base_url is stale. Fix: make rag-config (syncs it to OLLAMA_HOST), or wipe ./data/openwebui and 'make start'."
  finish; exit 1
fi

# --- create KB ---------------------------------------------------------------
section "create knowledge collection"
KB_ID=$(curl -s -X POST "$O/api/v1/knowledge/create" "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d '{"name":"rag-regression-test","description":"integration test: upload embed search"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
if [ -n "$KB_ID" ]; then pass "KB id: $KB_ID"; else fail "KB create failed"; finish; exit 1; fi

# --- upload text file --------------------------------------------------------
section "upload text file"
up=$(curl -s -X POST "$O/api/v1/files/" "${AUTH[@]}" -F "file=@${TMP_TXT};filename=rag_regression.txt")
FID=$(printf '%s' "$up" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
if [ -n "$FID" ]; then pass "file id: $FID"; else fail "upload failed: $(printf '%s' "$up" | head -c 200)"; finish; exit 1; fi

# --- poll process status (embeds on upload) ----------------------------------
section "embed (process/status)"
status=""
for i in $(seq 1 60); do
  status=$(curl -s "$O/api/v1/files/${FID}/process/status" "${AUTH[@]}" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",""))' 2>/dev/null)
  if [ "$status" = "completed" ]; then break; fi
  if [ "$status" = "failed" ]; then break; fi
  sleep 1
done
if [ "$status" = "completed" ]; then
  pass "embedded (status=completed)"
else
  fail "embed did not complete (status=${status})"
  fail "check OWUI logs: docker logs ${OWUI_CTN} | grep -i embed"
  finish; exit 1
fi

# --- bind file to KB ---------------------------------------------------------
section "bind file -> KB"
add=$(curl -s -X POST "$O/api/v1/knowledge/${KB_ID}/file/add" "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"file_id\":\"${FID}\"}")
if printf '%s' "$add" | grep -q "\"id\""; then pass "bound to KB"; else fail "bind failed: $(printf '%s' "$add" | head -c 200)"; finish; exit 1; fi
sleep 3  # let the KB collection ingest the embedded chunks

# --- semantic search ---------------------------------------------------------
section "semantic search via /api/v1/retrieval/query/collection"
q='exciton-polariton quasiparticle microcavity'
res=$(curl -s -X POST "$O/api/v1/retrieval/query/collection" "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"collection_names\":[\"${KB_ID}\"],\"query\":$(python3 -c 'import sys,json;print(json.dumps(sys.argv[1]))' "$q"),\"k\":4,\"hybrid\":true}")
printf '%s' "$res" | python3 -c '
import sys,json
d=json.load(sys.stdin)
# Open WebUI returns either a flat list of docs, a {"files":[...]} wrapper,
# or a Chroma-style {"documents":[[...]],"distances":[[...]],"metadatas":[[...]]}.
docs=[]
if isinstance(d,list):
    docs=d
elif isinstance(d,dict):
    if d.get("files") or d.get("results") or d.get("docs"):
        docs=d.get("files") or d.get("results") or d.get("docs")
        if isinstance(docs,dict): docs=docs.get("docs",[])
    elif "documents" in d:
        # Chroma shape: list-of-lists (one inner list per collection_name).
        docs=[t for sub in d["documents"] for t in (sub if isinstance(sub,list) else [sub])]
marker="'"${MARKER}"'"
def text(h):
    if isinstance(h,dict): return h.get("content") or h.get("text") or ""
    return str(h)
hits=len(docs)
has_marker=any(marker in text(h) for h in docs)
print(f"HITS={hits}")
print(f"MARKER_FOUND={has_marker}")
for h in docs[:4]:
    if isinstance(h,dict):
        print("DIST=%s NAME=%s" % (h.get("distance") or h.get("score"), (h.get("file_name") or h.get("metadata",{}).get("file_name") or "?")))
' > /tmp/kb_rag_search.out
hits=$(grep -oE 'HITS=[0-9]+' /tmp/kb_rag_search.out | cut -d= -f2)
marker_found=$(grep -oE 'MARKER_FOUND=(True|False)' /tmp/kb_rag_search.out | cut -d= -f2)
cat /tmp/kb_rag_search.out
rm -f /tmp/kb_rag_search.out
if [ "${hits:-0}" -gt 0 ] && [ "$marker_found" = "True" ]; then
  pass "search returned ${hits} hit(s) with the indexed marker"
else
  fail "search returned no usable hits (hits=${hits:-0}, marker_found=${marker_found})"
  fail "raw: $(printf '%s' "$res" | head -c 300)"
fi

finish