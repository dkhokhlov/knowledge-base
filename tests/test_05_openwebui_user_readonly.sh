#!/usr/bin/env bash
# System integration test: the agent (non-admin) API key is read-scoped.
#
# Guards mechanism A: a dedicated non-admin user whose API key can READ
# knowledge bases (via a '*' read grant) but cannot WRITE to or DELETE a KB
# it does not own. Contrast with the admin key, which has full write access.
#
# Self-contained: creates a temp KB with the admin key, grants '*' read,
# asserts the user key can list + search it but is denied file/add and delete,
# then cleans up (deletes the temp KB + file via the admin key).
#
# Requires `make api-keys` to have populated .env.local with:
#   OPENWEBUI_ADMIN_API_KEY, OPENWEBUI_USER_API_KEY.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up
require_env OPENWEBUI_ADMIN_API_KEY OPENWEBUI_USER_API_KEY || { finish; exit 1; }

O="http://localhost:${OPENWEBUI_HOST_PORT:-3000}"
AK="$OPENWEBUI_ADMIN_API_KEY"   # admin key (full access) — setup + cleanup
UK="$OPENWEBUI_USER_API_KEY"    # agent key (read-scoped) — subject under test
MARKER="kbrouser-9c4f1-piezoresistor"
TMP_TXT="$(mktemp)"
KB_ID=""; FID=""
A=(-H "Authorization: Bearer $AK")
U=(-H "Authorization: Bearer $UK")

cleanup() {
  [ -n "$KB_ID" ] && curl -sf -X DELETE "$O/api/v1/knowledge/${KB_ID}/delete" "${A[@]}" >/dev/null 2>&1 || true
  [ -n "$FID" ]   && curl -sf -X DELETE "$O/api/v1/files/${FID}" "${A[@]}" >/dev/null 2>&1 || true
  rm -f "$TMP_TXT"
}
trap cleanup EXIT

cat >"$TMP_TXT" <<EOF
Read-only user regression marker: ${MARKER}.
A piezoresistor is a resistor whose resistance changes under mechanical strain.
This document exists only so the integration test can verify that the agent
(non-admin) API key can read and search a knowledge base but cannot mutate it.
EOF

# --- 1. user key is a non-admin user ----------------------------------------
section "user API key authenticates as non-admin"
who=$(curl -s "$O/api/v1/auths/" "${U[@]}")
urole=$(printf '%s' "$who" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("role",""))' 2>/dev/null)
uemail=$(printf '%s' "$who" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("email",""))' 2>/dev/null)
if [ "$urole" = "user" ]; then pass "user key -> $uemail (role=user)"; else fail "user key -> role=${urole:-<none>} (expected user)"; finish; exit 1; fi

# --- 2. admin sets up a temp KB with '*' read grant --------------------------
section "admin: create KB + upload + embed + grant '*' read"
KB_ID=$(curl -s -X POST "$O/api/v1/knowledge/create" "${A[@]}" -H 'Content-Type: application/json' \
  -d '{"name":"ro-user-test","description":"integration test: read-only agent key"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
[ -n "$KB_ID" ] && pass "KB id: $KB_ID" || { fail "KB create failed"; finish; exit 1; }

up=$(curl -s -X POST "$O/api/v1/files/" "${A[@]}" -F "file=@${TMP_TXT};filename=ro_user_test.txt")
FID=$(printf '%s' "$up" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
[ -n "$FID" ] && pass "file id: $FID" || { fail "upload failed: $(printf '%s' "$up" | head -c 160)"; finish; exit 1; }

status=""
for i in $(seq 1 60); do
  status=$(curl -s "$O/api/v1/files/${FID}/process/status" "${A[@]}" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",""))' 2>/dev/null)
  [ "$status" = "completed" ] && break
  [ "$status" = "failed" ] && break
  sleep 1
done
[ "$status" = "completed" ] && pass "embedded (status=completed)" || { fail "embed status=$status"; finish; exit 1; }

curl -sf -X POST "$O/api/v1/knowledge/${KB_ID}/file/add" "${A[@]}" -H 'Content-Type: application/json' \
  -d "{\"file_id\":\"${FID}\"}" >/dev/null && pass "admin bound file to KB" || { fail "admin bind failed"; finish; exit 1; }
sleep 2

grant=$(curl -s -X POST "$O/api/v1/knowledge/${KB_ID}/access/update" "${A[@]}" -H 'Content-Type: application/json' \
  -d "{\"access_grants\":[{\"resource_type\":\"knowledge\",\"resource_id\":\"${KB_ID}\",\"principal_type\":\"user\",\"principal_id\":\"*\",\"permission\":\"read\"}]}")
if printf '%s' "$grant" | python3 -c 'import sys,json;d=json.load(sys.stdin);gs=d.get("access_grants") or [];sys.exit(0 if any(g.get("principal_id")=="*" and g.get("permission")=="read" for g in gs) else 1)' 2>/dev/null; then
  pass "granted '*' read on temp KB"
else
  fail "grant did not stick: $(printf '%s' "$grant" | head -c 160)"; finish; exit 1
fi

# --- 3. user key can READ: list KB (write_access=false) + search -------------
section "user key: read (list + search)"
ulist=$(curl -s "$O/api/v1/knowledge/" "${U[@]}" \
  | python3 -c '
import sys,json
d=json.load(sys.stdin)
kb=[k for k in d["items"] if k["id"]=="'"${KB_ID}"'"]
if not kb:
    print("VISIBLE=0 WA=")
else:
    print("VISIBLE=1 WA=%s" % kb[0].get("write_access"))
')
visible=$(printf '%s' "$ulist" | grep -oE 'VISIBLE=[01]' | cut -d= -f2)
wa=$(printf '%s' "$ulist" | grep -oE 'WA=(True|False|)' | cut -d= -f2)
if [ "$visible" = "1" ] && [ "$wa" = "False" ]; then pass "user sees temp KB, write_access=False"; else fail "user visibility: visible=${visible:-0} write_access=${wa:-?}"; finish; exit 1; fi

curl -s -X POST "$O/api/v1/retrieval/query/collection" "${U[@]}" -H 'Content-Type: application/json' \
  -d "{\"collection_names\":[\"${KB_ID}\"],\"query\":$(python3 -c 'import sys,json;print(json.dumps(sys.argv[1]))' "piezoresistor strain resistance"),\"k\":3,\"hybrid\":true}" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);docs=[t for sub in d.get("documents",[[]]) for t in sub];print("HITS=%d MARKER=%s"%(len(docs), any("'"${MARKER}"'" in t for t in docs)))' > /tmp/kbrouser.out
hits=$(grep -oE 'HITS=[0-9]+' /tmp/kbrouser.out | cut -d= -f2)
mk=$(grep -oE 'MARKER=(True|False)' /tmp/kbrouser.out | cut -d= -f2)
cat /tmp/kbrouser.out; rm -f /tmp/kbrouser.out
if [ "${hits:-0}" -gt 0 ] && [ "$mk" = "True" ]; then pass "user search -> ${hits} hit(s), marker found"; else fail "user search -> hits=${hits:-0} marker=${mk}"; finish; exit 1; fi

# --- 4. user key is DENIED write (file/add + delete) -----------------------
# Proves the KB-write access control — NOT a coincidental 404 from a bogus id.
# OWUI knowledge.py file/{id}/add: the KB write-access check runs FIRST and
# raises HTTP 400 ACCESS_PROHIBITED for a non-owner/non-admin without write
# access, BEFORE the file lookup. So:
#   - authz works  -> 400 (KB-write denied). Expected.
#   - authz broken (agent has write) -> the real $FID is found, then the
#     file-read check raises 403 (agent has no file-read grant), or 200 if
#     file-read is also broken. Either way code != 400 -> this fails.
# A bogus file_id would 400 on "file not found" even with broken authz (a
# false pass), so we use the real uploaded $FID and assert the specific 400,
# plus an unchanged KB file_count (no mutation).
section "user key: write denied (file/add 400 ACCESS_PROHIBITED + KB unchanged)"
count_before=$(curl -s "$O/api/v1/knowledge/${KB_ID}" "${A[@]}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("file_count",0))' 2>/dev/null)
code_add=$(curl -s -o /tmp/kbrouser_add.out -w '%{http_code}' -X POST "$O/api/v1/knowledge/${KB_ID}/file/add" "${U[@]}" \
  -H 'Content-Type: application/json' -d "{\"file_id\":\"${FID}\"}")
body_add=$(cat /tmp/kbrouser_add.out 2>/dev/null); rm -f /tmp/kbrouser_add.out
count_after=$(curl -s "$O/api/v1/knowledge/${KB_ID}" "${A[@]}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("file_count",0))' 2>/dev/null)
if [ "${code_add:-0}" -eq 400 ] && [ "${count_before:-0}" -eq "${count_after:-0}" ]; then
  pass "file/add denied (http=400 ACCESS_PROHIBITED; KB file_count ${count_before} unchanged)"
else
  fail "file/add: code=${code_add} file_count ${count_before}->${count_after} (expected 400 + unchanged — KB-write authz broken?)"; finish; exit 1
fi

# delete: no body id, so >=400 is the genuine KB-write denial (400
# ACCESS_PROHIBITED). Also confirm the KB still exists (no mutation).
code_del=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$O/api/v1/knowledge/${KB_ID}/delete" "${U[@]}")
exists_after=$(curl -s -o /dev/null -w '%{http_code}' "$O/api/v1/knowledge/${KB_ID}" "${A[@]}")
if [ "${code_del:-0}" -ge 400 ] && [ "${exists_after:-0}" -eq 200 ]; then
  pass "delete denied (http=${code_del}; KB still exists http=${exists_after})"
else
  fail "delete: code=${code_del} exists_after=${exists_after} (expected denial + KB still present — delete authz broken?)"; finish; exit 1
fi

# --- 5. contrast: admin key has write_access on the same KB ------------------
section "contrast: admin key has write access"
awa=$(curl -s "$O/api/v1/knowledge/${KB_ID}" "${A[@]}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("write_access"))' 2>/dev/null)
if [ "$awa" = "True" ]; then pass "admin key -> write_access=True (contrast)"; else fail "admin key -> write_access=${awa:-?} (expected True)"; fi

# --- 6. user key can RAG chat GROUNDED on the KB (model grant + grounding) -----
# Two things must work for a grounded agent RAG chat:
#   (a) the agent user has '*' read on the chat model (granted by make api-keys);
#       without it /api/chat/completions returns "Model not found".
#   (b) the KB is attached via the top-level `files` field as a collection item
#       ({"type":"collection","id":<kb-id>}). A `knowledge` field is silently
#       ignored and `metadata.knowledge` is discarded server-side, so the chat
#       would answer from training data only (confabulate).
# This test catches a grounding regression: it asks for the unique marker string
# ($MARKER), which the model cannot know unless the KB chunks are injected. If
# grounding is broken (e.g. someone reverts to a `knowledge` field), the marker
# is absent and this fails.
section "user key: RAG chat grounded on KB (model grant + files:collection)"
CHAT_MODEL="${OPENWEBUI_MODEL:-${MODEL_NAME:-gemma4:12b}}"
rag_body=$(python3 -c 'import sys,json;print(json.dumps({"model":sys.argv[1],"stream":False,"files":[{"type":"collection","id":sys.argv[2]}],"messages":[{"role":"user","content":"What is the exact regression marker string mentioned in the document? Return only the marker value."}]}))' "$CHAT_MODEL" "$KB_ID")
rag_code=$(curl -s -o /tmp/kbrouser_rag.out -w '%{http_code}' -X POST "$O/api/chat/completions" "${U[@]}" \
  -H 'Content-Type: application/json' -d "$rag_body")
rag_body_out=$(cat /tmp/kbrouser_rag.out 2>/dev/null); rm -f /tmp/kbrouser_rag.out
rag_content=$(printf '%s' "$rag_body_out" | python3 -c 'import sys,json;d=json.load(sys.stdin);print((d.get("choices",[{}])[0].get("message",{}).get("content") or "").strip())' 2>/dev/null)
if [ "${rag_code:-0}" -ne 200 ]; then
  fail "RAG chat -> http=${rag_code} (expected 200; model grant missing?): $(printf '%s' "$rag_body_out" | head -c 160)"
  finish; exit 1
fi
if printf '%s' "$rag_content" | grep -qF "$MARKER"; then
  pass "RAG chat grounded -> marker '$MARKER' present in answer"
else
  fail "RAG chat NOT grounded -> marker absent (http=200, answer: $(printf '%s' "$rag_content" | head -c 120))"
  finish; exit 1
fi

finish