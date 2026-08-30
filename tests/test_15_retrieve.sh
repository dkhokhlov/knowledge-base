#!/usr/bin/env bash
# System integration test: gateway-mediated POST /retrieve (Caddy -> api-gateway
# -> OWUI). Proves:
#   - the Caddyfile @retrieve route reaches the api-gateway (validation matrix:
#     no key -> 401, non-UUID kb_id -> 400, bad mode -> 400, empty query -> 400);
#   - all three modes (hybrid/lexical/vector) -> 200 against a synthetic fixture KB;
#   - acceptance: mode=lexical returns a rare exact-token chunk (the point of the
#     pgvector-FTS redesign — pure FTS finds terse keyword chunks pure-vector
#     misses). Gated on VECTOR_DB=pgvector (lexical is not pure FTS on Chroma).
#
# Self-contained: the temp KB is created with the admin key, granted '*' read so
# the agent (user) key can retrieve, and deleted on EXIT (its files too). The
# fixture is one small .txt with a rare synthetic token (no PII).
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up
require_env OPENWEBUI_ADMIN_API_KEY KB_API_KEY || { finish; exit 1; }

G="$(kb_host)"
AK="$OPENWEBUI_ADMIN_API_KEY"
UK="$KB_API_KEY"
ADM=(-H "Authorization: Bearer $AK")
RD=(-H "Authorization: Bearer $UK")
CT="Content-Type: application/json"
KB_ID=""
FID=""
FIXTURE_TOKEN="retrieve-fixture-marker-4c2f9"
# A valid UUID shape the gateway accepts past _is_uuid; OWUI then 403s on an
# unknown collection, which the gateway maps to 502. Used only for the no-key
# 401 path (auth runs before validation) and the bad-input 400 paths.
DUMMY_UUID="550e8400-e29b-41d4-a716-446655440000"

cleanup() {
  if [ -n "$FID" ]; then
    curl -sf -X DELETE "$G/api/v1/files/${FID}" "${ADM[@]}" >/dev/null 2>&1 || true
  fi
  if [ -n "$KB_ID" ]; then
    curl -sf -X DELETE "$G/api/v1/knowledge/${KB_ID}/delete" "${ADM[@]}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# --- validation matrix (fixture-free; proves the Caddy route + gateway gate) ---
section "POST /retrieve validation (via Caddy)"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$G/retrieve" -H "$CT" \
  -d "{\"kb_id\":\"${DUMMY_UUID}\",\"query\":\"x\",\"mode\":\"hybrid\"}")
[ "$code" = 401 ] && pass "no key -> 401" || fail "no key -> $code (want 401)"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$G/retrieve" "${RD[@]}" -H "$CT" \
  -d '{"kb_id":"not-a-uuid","query":"x","mode":"hybrid"}')
[ "$code" = 400 ] && pass "non-UUID kb_id -> 400" || fail "non-UUID kb_id -> $code (want 400)"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$G/retrieve" "${RD[@]}" -H "$CT" \
  -d "{\"kb_id\":\"${DUMMY_UUID}\",\"query\":\"x\",\"mode\":\"fuzzy\"}")
[ "$code" = 400 ] && pass "bad mode -> 400" || fail "bad mode -> $code (want 400)"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$G/retrieve" "${RD[@]}" -H "$CT" \
  -d "{\"kb_id\":\"${DUMMY_UUID}\",\"query\":\"   \",\"mode\":\"hybrid\"}")
[ "$code" = 400 ] && pass "blank query -> 400" || fail "blank query -> $code (want 400)"

# --- create temp fixture KB + grant '*' read so the user key can retrieve -----
section "create temp fixture KB for /retrieve"
KB_ID=$(curl -s -X POST "$G/api/v1/knowledge/create" "${ADM[@]}" -H "$CT" \
  -d '{"name":"retrieve-route-test","description":"integration test: /retrieve route"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
[ -n "$KB_ID" ] && pass "KB id: $KB_ID" || { fail "KB create failed"; finish; exit 1; }

curl -s -X POST "$G/api/v1/knowledge/${KB_ID}/access/update" "${ADM[@]}" -H "$CT" \
  -d "{\"access_grants\":[{\"resource_type\":\"knowledge\",\"resource_id\":\"${KB_ID}\",\"principal_type\":\"user\",\"principal_id\":\"*\",\"permission\":\"read\"}]}" >/dev/null 2>&1
pass "granted '*' read on temp KB"

# --- upload one fixture text file with a rare exact token --------------------
# POST /api/v1/files/ with metadata.knowledge_id queues OWUI's per-upload
# background task (extract -> embed -> link), so no separate /process call.
section "upload fixture doc (rare exact token)"
tmpf="$(mktemp).txt"
cat > "$tmpf" <<EOF
Register definition. ${FIXTURE_TOKEN} is the scheduling-policy control register
at offset 0x1c05. It engages the weighted round-robin vector DMA_WRR_VEC. The
field CAP_ENGAGE latches the policy on a rising edge of the bus clock.
EOF
FID=$(python3 - "$tmpf" "$KB_ID" "$AK" "$G" <<'PY'
import sys, hashlib, json, urllib.request, uuid, os
path, kb_id, ak, base = sys.argv[1:5]
with open(path, "rb") as f:
    data = f.read()
meta = {"knowledge_id": kb_id, "file_hash": hashlib.sha256(data).hexdigest(),
        "directory_id": "test"}
boundary = uuid.uuid4().hex
body = bytearray()
body += ("--%s\r\n" % boundary).encode()
body += ('Content-Disposition: form-data; name="file"; filename="%s"\r\n'
         % os.path.basename(path)).encode()
body += b"Content-Type: application/octet-stream\r\n\r\n"
body += data + b"\r\n"
body += ("--%s\r\n" % boundary).encode()
body += b'Content-Disposition: form-data; name="metadata"\r\n'
body += b"Content-Type: application/json\r\n\r\n"
body += json.dumps(meta).encode() + b"\r\n"
body += ("--%s--\r\n" % boundary).encode()
req = urllib.request.Request(base + "/api/v1/files/", data=bytes(body),
    headers={"Authorization": "Bearer " + ak,
             "Content-Type": "multipart/form-data; boundary=%s" % boundary},
    method="POST")
with urllib.request.urlopen(req, timeout=60) as r:
    print(json.loads(r.read().decode()).get("id", ""))
PY
)
rm -f "$tmpf"
[ -n "$FID" ] && pass "fixture file id: $FID" || { fail "fixture upload failed"; finish; exit 1; }

# --- poll the fixture file drain until the background task completes ----------
section "poll fixture file drain"
wait_s="${RETRIEVE_FIXTURE_WAIT:-240}"
deadline=$(( $(date +%s) + wait_s ))
status=""
while :; do
  status=$(curl -s "$G/api/v1/files/${FID}?content=false" "${ADM[@]}" 2>/dev/null \
    | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    # The terminal-status patch writes file.data.status (the data column),
    # NOT meta.data.status (which only holds knowledge_id). The gateway /status
    # route reads the same field via GET /files/.
    print((d.get("data") or {}).get("status",""))
except Exception:
    print("")' 2>/dev/null)
  [ "$status" = "completed" ] && break
  [ "$status" = "failed" ] && break
  [ "$(date +%s)" -ge "$deadline" ] && break
  sleep 4
done
if [ "$status" = "completed" ]; then
  pass "fixture file drain completed"
else
  fail "fixture file drain not completed after ${wait_s}s (status=${status:-none})"
  fail "check: docker logs kb-openwebui | tail -50"
  finish; exit 1
fi

# --- all three modes -> 200 against the fixture KB ---------------------------
section "POST /retrieve all three modes -> 200"
for mode in hybrid lexical vector; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$G/retrieve" "${RD[@]}" -H "$CT" \
    -d "{\"kb_id\":\"${KB_ID}\",\"query\":\"${FIXTURE_TOKEN}\",\"k\":10,\"mode\":\"${mode}\"}")
  [ "$code" = 200 ] && pass "mode=$mode -> 200" || fail "mode=$mode -> $code (want 200)"
done

# --- acceptance: mode=lexical returns the rare exact token (pgvector FTS) -----
# The lexical mode is the point of the redesign: pure FTS (ts_rank_cd) returns
# the exact-token chunk, where pure-vector can miss terse keyword chunks.
# Requires VECTOR_DB=pgvector + the OWUI P7/P8/P9 patches. On a non-pgvector
# backend lexical is not pure FTS, so SKIP (not a regression).
section "acceptance: mode=lexical returns the exact-token chunk"
if [ "${VECTOR_DB:-}" != "pgvector" ]; then
  pass "SKIP: VECTOR_DB!=pgvector (lexical is not pure FTS on this backend)"
else
  body=$(curl -s -X POST "$G/retrieve" "${RD[@]}" -H "$CT" \
    -d "{\"kb_id\":\"${KB_ID}\",\"query\":\"${FIXTURE_TOKEN}\",\"k\":10,\"mode\":\"lexical\"}")
  rank0=$(printf '%s' "$body" | python3 -c 'import sys,json
tok=sys.argv[1]
try:
    d=json.load(sys.stdin)
    hits=d.get("hits") or []
    print(str(len(hits)>0 and tok in (hits[0].get("text") or "")).lower())
except Exception:
    print("error")' "$FIXTURE_TOKEN" 2>/dev/null)
  if [ "$rank0" = "true" ]; then
    pass "mode=lexical returns the exact-token chunk at rank 0"
  else
    fail "mode=lexical did not return the exact-token chunk at rank 0 (rank0=$rank0)"
  fi
fi

finish