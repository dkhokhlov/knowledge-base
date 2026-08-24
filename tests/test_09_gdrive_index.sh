#!/usr/bin/env bash
# System integration test: gdrive indexing via kb-gateway (stateless, no sidecar).
#
# POSTs /index (admin key) to reconcile ./gdrive into the OWUI "gdrive" KB,
# then polls GET /status (read key) until extraction drains (pending=0), and
# asserts indexed_count >= source_count - per-file errors. Best-effort semantic
# search over the KB (a hit is a bonus, not a hard requirement — name/content
# matching is not guaranteed for arbitrary docs).
#
# /index is idempotent: re-running on an indexed KB reports unmodified + adds
# nothing, so this test is safe to run repeatedly.
#
# Tolerant: SKIPs (passes with a notice) when gdrive indexing is not provisioned
# — GDRIVE_KB_ID unset in .env.local, or ./gdrive has no allowlisted files — so
# `make test` still runs clean in a bare environment.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up

O="$(kb_host)"
# Allowlist must match gateway DEFAULT_ALLOW (gateway/app.py). find's default
# Emacs regex treats (a|b) as LITERAL (matches 0 files), so every -iregex call
# below MUST use -regextype posix-extended for the alternation to work.
ALLOW_RE='[.](docx|pdf|pptx|xlsx|txt|md|html|json|log|tex)$'

# --- skip conditions ---------------------------------------------------------
if [ -z "${GDRIVE_KB_ID:-}" ]; then
  section "gdrive index"
  pass "SKIP: GDRIVE_KB_ID not set in .env.local (run: make gdrive-index-bootstrap)"
  finish
  exit 0
fi
src_count=$(find gdrive -type f -regextype posix-extended -iregex ".*${ALLOW_RE}" 2>/dev/null | wc -l)
if [ "${src_count:-0}" -eq 0 ]; then
  section "gdrive index"
  pass "SKIP: ./gdrive has no allowlisted files to index (run: make gdrive-sync)"
  finish
  exit 0
fi

require_env OPENWEBUI_ADMIN_API_KEY OPENWEBUI_USER_API_KEY || { finish; exit 1; }
AK="$OPENWEBUI_ADMIN_API_KEY"
UK="$OPENWEBUI_USER_API_KEY"
ADM=(-H "Authorization: Bearer $AK")
RD=(-H "Authorization: Bearer $UK")

# --- POST /index (admin): reconcile ./gdrive into the KB ---------------------
section "POST /index (kb-gateway)"
idx_resp=$(curl -sS --max-time 1200 -X POST \
  "$O/index?source=gdrive&kb_id=${GDRIVE_KB_ID}" \
  "${ADM[@]}" -H 'Content-Type: application/json' -d '{}' 2>&1)
read -r added modified deleted unmodified errn < <(printf '%s' "$idx_resp" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("0 0 0 0 0"); sys.exit(0)
if not isinstance(d, dict) or "ok" not in d:
    print("0 0 0 0 0"); sys.exit(0)
print(d.get("added", 0), d.get("modified", 0), d.get("deleted", 0),
      d.get("unmodified", 0), len(d.get("errors") or []))
' 2>/dev/null || echo "0 0 0 0 0")
if [ "$added" = "0" ] && [ "$modified" = "0" ] && [ "$unmodified" = "0" ] && [ "$errn" = "0" ]; then
  fail "POST /index returned no parseable result: ${idx_resp}"
  finish
  exit 1
fi
pass "/index: added=${added} modified=${modified} deleted=${deleted} unmodified=${unmodified} errors=${errn}"
if [ "${errn:-0}" -gt 0 ]; then
  fail "/index reported ${errn} per-file error(s):"
  printf '%s' "$idx_resp" | python3 -c '
import sys, json
d = json.load(sys.stdin)
for e in (d.get("errors") or [])[:20]:
    print("    " + str(e.get("filename") or e.get("file_id") or "?") +
          ": " + str(e.get("status", "")) + " - " + str(e.get("error", "")))
'
fi

# --- poll GET /status until extraction drains (pending=0) -------------------
# Extraction + embedding run in OWUI's per-upload background task (queued by
# POST /files/ with metadata.knowledge_id), not via a gateway batch_process
# call. They drain async. Poll /status (read key) for pending=0 up to
# GDRIVE_TEST_WAIT (default 600s).
section "poll GET /status (pending drain)"
wait_s="${GDRIVE_TEST_WAIT:-600}"
deadline=$(( $(date +%s) + wait_s ))
pending=""
indexed=""
while :; do
  st=$(curl -sS "$O/status?source=gdrive&kb_id=${GDRIVE_KB_ID}&json=1" "${RD[@]}" 2>/dev/null || true)
  pending=$(printf '%s' "$st" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin); p = d.get("pending")
    print("%d" % p if isinstance(p, int) else "")
except Exception:
    print("")
')
  indexed=$(printf '%s' "$st" | python3 -c '
import sys, json
try:
    print(json.load(sys.stdin).get("indexed_count") or 0)
except Exception:
    print(0)
')
  if [ "$pending" = "0" ]; then break; fi
  if [ "$(date +%s)" -ge "$deadline" ]; then break; fi
  sleep 10
done
if [ "$pending" = "0" ]; then
  pass "pending=0 (extraction drained); indexed=${indexed:-0} source=${src_count}"
else
  fail "pending=${pending:-<unavailable>} after ${wait_s}s (indexed=${indexed:-0}); extraction did not drain"
  fail "check: docker logs kb-openwebui + docker logs kb-markitdown-ocr"
fi

# --- indexed count assertion ------------------------------------------------
# The upload-idempotency + path-aware-dedup patches make duplicate-content 400s
# not occur, so a healthy run has errors=0 and indexed reaches source. Any
# per-file error lowers the bar by exactly its count (the errors explain the
# gap). An unexplained gap (indexed < source - errors) is a real failure.
section "indexed vs source"
idx_n="${indexed:-0}"
expected_min=$(( src_count - ${errn:-0} ))
if [ "${idx_n}" -ge "${expected_min}" ] && [ "${idx_n}" -gt 0 ]; then
  pass "KB indexed=${idx_n} >= source(${src_count}) - errors(${errn:-0}) = ${expected_min}"
else
  fail "KB indexed=${idx_n} < source(${src_count}) - errors(${errn:-0}) = ${expected_min} (unexplained gap)"
  fail "re-run: make gdrive-sync; then docker logs kb-openwebui / kb-markitdown-ocr"
fi

# --- best-effort semantic search --------------------------------------------
section "semantic search over gdrive KB (best-effort)"
qfile=$(find gdrive -type f -regextype posix-extended -iregex ".*${ALLOW_RE}" 2>/dev/null | head -1)
q=$(basename "$qfile" | sed -E 's/\.[A-Za-z0-9]+$//' | tr -c '[:alnum:]' ' ' | tr -s ' ' | sed -E 's/^ | $//g')
if [ -z "$q" ]; then q="verification plan"; fi
hits=$(curl -s -X POST "$O/api/v1/retrieval/query/collection" "${RD[@]}" -H 'Content-Type: application/json' \
  -d "{\"collection_names\":[\"${GDRIVE_KB_ID}\"],\"query\":$(python3 -c 'import sys,json;print(json.dumps(sys.argv[1]))' "$q"),\"k\":4,\"hybrid\":true}" \
  | python3 -c '
import sys,json
d=json.load(sys.stdin)
docs=[]
if isinstance(d,list): docs=d
elif isinstance(d,dict):
    if "documents" in d: docs=[t for sub in d["documents"] for t in (sub if isinstance(sub,list) else [sub])]
    else: docs=d.get("files") or d.get("results") or d.get("docs") or []
print(len(docs))
' 2>/dev/null || true)
if [ "${hits:-0}" -gt 0 ]; then
  pass "search q=\"$q\" -> ${hits} hit(s)"
else
  pass "search q=\"$q\" -> 0 hits (best-effort; not a failure — name/content matching is not guaranteed)"
fi

finish