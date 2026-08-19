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

# --- 4. user key is DENIED write (file/add) and delete -----------------------
section "user key: write denied (file/add + delete)"
code_add=$(curl -s -o /tmp/kbrouser_add.out -w '%{http_code}' -X POST "$O/api/v1/knowledge/${KB_ID}/file/add" "${U[@]}" \
  -H 'Content-Type: application/json' -d '{"file_id":"00000000-0000-0000-0000-000000000000"}')
body_add=$(cat /tmp/kbrouser_add.out 2>/dev/null); rm -f /tmp/kbrouser_add.out
if [ "${code_add:-0}" -ge 400 ]; then pass "file/add denied (http=${code_add})"; else fail "file/add SUCCEEDED (http=${code_add}) — write not scoped!"; finish; exit 1; fi

code_del=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$O/api/v1/knowledge/${KB_ID}/delete" "${U[@]}")
if [ "${code_del:-0}" -ge 400 ]; then pass "delete denied (http=${code_del})"; else fail "delete SUCCEEDED (http=${code_del}) — delete not scoped!"; finish; exit 1; fi

# --- 5. contrast: admin key has write_access on the same KB ------------------
section "contrast: admin key has write access"
awa=$(curl -s "$O/api/v1/knowledge/${KB_ID}" "${A[@]}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("write_access"))' 2>/dev/null)
if [ "$awa" = "True" ]; then pass "admin key -> write_access=True (contrast)"; else fail "admin key -> write_access=${awa:-?} (expected True)"; fi

finish