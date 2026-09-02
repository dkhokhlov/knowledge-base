#!/usr/bin/env bash
# System integration test: gateway-mediated POST /retrieve (Caddy -> api-gateway
# -> OWUI). Proves:
#   - the Caddyfile @retrieve route reaches the api-gateway (validation matrix:
#     no key -> 401, non-UUID kb_id -> 400, bad mode -> 400, empty query -> 400);
#   - all three modes (hybrid/lexical/vector) -> 200 + the full response validated
#     against a synthetic fixture KB (schema + the fixture chunk at rank 0). The
#     fixture is one small .txt with a rare synthetic token, so every mode that
#     returns anything must return that chunk. pgvector is the only backend
#     (Chroma removed), so all three modes are content-validated on every run.
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
  fail "check: docker logs ${OWUI_CONTAINER:-kb-openwebui} | tail -50"
  finish; exit 1
fi

# --- all three modes -> 200 + full response validated against the fixture -----
# The fixture KB holds ONE chunk containing FIXTURE_TOKEN, so every mode that
# returns anything must return that chunk (rank 0, the only hit). Per mode we
# validate the FULL response -- not just a 200, not just ">=1 hit": the schema
# (mode / kb_id / k / score_order / hits) AND that the fixture chunk is returned
# at rank 0. score_order: hybrid + lexical rank by RRF score (desc); vector by
# cosine distance (asc). pgvector is the only backend (Chroma removed), so all
# three modes are content-validated on every run.
section "POST /retrieve all three modes -> 200 + response validated"
for mode in hybrid lexical vector; do
    case "$mode" in hybrid|lexical) want_order=desc ;; vector) want_order=asc ;; esac
    bf="$(mktemp)"
    code=$(curl -s -o "$bf" -w '%{http_code}' -X POST "$G/retrieve" "${RD[@]}" -H "$CT" \
      -d "{\"kb_id\":\"${KB_ID}\",\"query\":\"${FIXTURE_TOKEN}\",\"k\":10,\"mode\":\"${mode}\"}")
    if [ "$code" != 200 ]; then
      fail "mode=$mode -> HTTP $code (want 200): $(head -c 200 "$bf" 2>/dev/null)"
      rm -f "$bf"; continue
    fi
    verdict=$(python3 -c '
import sys, json
mode, kb_id, want_order, tok = sys.argv[1:5]
try:
    d = json.load(sys.stdin)
except Exception as ex:
    print("FAIL non-JSON: %s" % ex); sys.exit(0)
errs = []
if d.get("mode") != mode:
    errs.append("mode=%r want %s" % (d.get("mode"), mode))
if d.get("kb_id") != kb_id:
    errs.append("kb_id=%r want %s" % (d.get("kb_id"), kb_id))
if d.get("k") != 10:
    errs.append("k=%r want 10" % (d.get("k"),))
if d.get("score_order") != want_order:
    errs.append("score_order=%r want %s" % (d.get("score_order"), want_order))
hits = d.get("hits")
if not isinstance(hits, list):
    errs.append("hits=%r want list" % (hits,))
elif len(hits) == 0:
    errs.append("hits empty (mode returned no chunks for the fixture token)")
else:
    h0 = hits[0] or {}
    if tok not in (h0.get("text") or ""):
        errs.append("rank0 text missing the fixture token (text=%r)" % (h0.get("text"),))
    for i, h in enumerate(hits):
        if not isinstance(h, dict) or "text" not in h or "distance" not in h:
            errs.append("hit[%d] not a flattened chunk (keys=%r)" % (i, list((h or {}).keys())))
            break
if errs:
    print("FAIL " + "; ".join(errs))
else:
    print("OK n=%d score_order=%s rank0-has-token" % (len(hits), d.get("score_order")))
' "$mode" "$KB_ID" "$want_order" "$FIXTURE_TOKEN" < "$bf" 2>/dev/null || echo "FAIL validator crashed")
    rm -f "$bf"
    case "$verdict" in
      OK*) pass "mode=$mode -> 200 + ${verdict#OK }" ;;
      *)   fail "mode=$mode -> ${verdict}" ;;
    esac
  done

finish