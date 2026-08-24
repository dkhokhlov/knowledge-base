#!/usr/bin/env bash
# System integration test: gdrive indexing via kb-gateway (stateless, no sidecar).
#
# POSTs /index (admin key) to reconcile ./gdrive into the OWUI "gdrive" KB.
# The gateway uploads via POST /files/ (process_in_background=True) and does NOT
# link files itself — OWUI's per-upload background task is the sole linker
# (extract via markitdown-ocr -> embed into the KB collection -> link). That
# drain is async, so this test polls GET /status for the REAL drain terminal
# state, then audits failures + runs a deterministic semantic search.
#
# /status reads file.data.status (via GET /files/?content=false): pending =
# in extraction (OCR/GPU), processing = embedding + linking, completed =
# searchable, failed = error. Drain is terminal when pending+processing == 0
# AND completed+failed covers the source.
#
# Failure audit: a "Failed to link file ... to knowledge ..." error means a
# second link insert collided with an existing one (a double-link race) and
# marks a successfully-extracted file as failed. This must not occur (hard
# fail) — the background task is the sole linker. Other failures (empty
# content, OCR timeout) are genuine source-file issues: surfaced as a notice,
# not a hard fail (re-run /index to re-trigger them).
#
# Deterministic semantic search: query the filename stem of the first COMPLETED
# file against the KB collection and require >=1 hit. A completed file has
# vectors, so its own stem (usually present in its content) must retrieve a
# document. 0 hits on a completed file's stem means vectors are not searchable.
#
# /index is idempotent (re-run reports unmodified + re-triggers any failed).
#
# Tolerant: SKIPs (passes with a notice) when gdrive indexing is not provisioned
# — GDRIVE_KB_ID unset in .env.local, or ./gdrive has no allowlisted files — so
# `make test` still runs clean in a bare environment.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up

# Not part of `make test` (slow: up to GDRIVE_TEST_WAIT of GPU OCR over the
# whole live rclone-synced corpus). `make test` covers the gdrive index path
# fast + deterministically via test_11_gdrive_index_fixture.sh. This full
# real-gdrive drain runs under `make test-e2e` (which invokes it by path).
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
read -r added modified deleted unmodified retried errn < <(printf '%s' "$idx_resp" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("0 0 0 0 0 0"); sys.exit(0)
if not isinstance(d, dict) or "ok" not in d:
    print("0 0 0 0 0 0"); sys.exit(0)
print(d.get("added", 0), d.get("modified", 0), d.get("deleted", 0),
      d.get("unmodified", 0), d.get("retried", 0), len(d.get("errors") or []))
' 2>/dev/null || echo "0 0 0 0 0 0")
if [ "$added" = "0" ] && [ "$modified" = "0" ] && [ "$unmodified" = "0" ] && [ "$errn" = "0" ]; then
  fail "POST /index returned no parseable result: ${idx_resp}"
  finish
  exit 1
fi
pass "/index: added=${added} modified=${modified} deleted=${deleted} unmodified=${unmodified} retried=${retried} errors=${errn}"
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

# --- poll GET /status until the drain reaches a terminal state ---------------
# Real completion = no pending AND no processing AND completed+failed covers the
# source (every uploaded file reached a terminal state). /status reads
# file.data.status via GET /files/. Poll up to GDRIVE_TEST_WAIT (default 600s).
# Admin key: /status uses the gateway's held admin key for the file scan
# internally; any valid key authorizes the call.
section "poll GET /status (real drain)"
wait_s="${GDRIVE_TEST_WAIT:-600}"
deadline=$(( $(date +%s) + wait_s ))
completed=0; pending=0; processing=0; failed=0; status_json=""
while :; do
  status_json=$(curl -sS "$O/status?source=gdrive&kb_id=${GDRIVE_KB_ID}&json=1" "${ADM[@]}" 2>/dev/null || true)
  read -r completed pending processing failed < <(printf '%s' "$status_json" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get("indexed_count",0), d.get("pending",0), d.get("processing",0), d.get("failed",0))
except Exception:
    print("0 0 0 0")
')
  in_flight=$(( pending + processing ))
  accounted=$(( completed + failed ))
  if [ "$in_flight" = "0" ] && [ "$accounted" -ge "$src_count" ]; then break; fi
  if [ "$(date +%s)" -ge "$deadline" ]; then break; fi
  sleep 10
done
in_flight=$(( pending + processing ))
accounted=$(( completed + failed ))
if [ "$in_flight" = "0" ] && [ "$accounted" -ge "$src_count" ]; then
  pass "drain terminal: completed=${completed} failed=${failed} pending=${pending} processing=${processing} source=${src_count}"
else
  fail "drain did not terminate after ${wait_s}s: completed=${completed} failed=${failed} pending=${pending} processing=${processing} source=${src_count} (accounted=${accounted})"
  fail "check: docker logs kb-openwebui + docker logs kb-markitdown-ocr"
  finish
  exit 1
fi

# --- failure audit: "Failed to link" = double-link race (must be 0) -----------
section "failure audit"
link_fails=$(printf '%s' "$status_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(sum(1 for f in (d.get("failed_files") or []) if "Failed to link" in (f.get("error") or "")))
' 2>/dev/null || echo 0)
if [ "${link_fails:-0}" -gt 0 ]; then
  fail "${link_fails} file(s) failed with 'Failed to link' (a double-link race — the background task must be the sole linker):"
  printf '%s' "$status_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
for f in (d.get("failed_files") or [])[:20]:
    if "Failed to link" in (f.get("error") or ""):
        print("    " + str(f.get("filename") or "?") + ": " + str(f.get("error") or "")[:100])
'
  finish
  exit 1
fi
# Surface genuine (non-link) failures as a notice, not a hard fail.
genuine_out=$(printf '%s' "$status_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
gf = [f for f in (d.get("failed_files") or []) if "Failed to link" not in (f.get("error") or "")]
print(len(gf))
for f in gf[:10]:
    print("    " + str(f.get("filename") or "?") + ": " + str(f.get("error") or "")[:100])
' 2>/dev/null || true)
genuine_n=$(printf '%s' "$genuine_out" | head -1)
if [ "${genuine_n:-0}" -gt 0 ]; then
  pass "no 'Failed to link' errors; ${genuine_n} genuine failure(s) surfaced (re-run /index to re-trigger):"
  printf '%s' "$genuine_out" | tail -n +2
else
  pass "no failures (all ${src_count} files completed)"
fi

# --- completed vs source -----------------------------------------------------
section "completed vs source"
expected_min=$(( src_count - failed ))
if [ "$completed" -ge "$expected_min" ] && [ "$completed" -gt 0 ]; then
  pass "completed=${completed} >= source(${src_count}) - failed(${failed}) = ${expected_min}"
else
  fail "completed=${completed} < source(${src_count}) - failed(${failed}) = ${expected_min} (unexplained gap)"
  fail "re-run: make gdrive-sync; then docker logs kb-openwebui / kb-markitdown-ocr"
  finish
  exit 1
fi

# --- deterministic semantic search (vectors written + searchable) ------------
# Query the filename stem of the first COMPLETED file against the KB collection
# and require >=1 hit. A completed file has vectors, so its own stem (usually
# present in its content) must retrieve a document. 0 hits on a completed
# file's stem means vectors are not searchable (embedding/RAG config issue).
section "semantic search over gdrive KB (deterministic)"
q_stem=$(printf '%s' "$status_json" | python3 -c '
import sys, json, re
d = json.load(sys.stdin)
for f in (d.get("files") or []):
    if f.get("status") == "completed" and f.get("filename"):
        stem = re.sub(r"[^A-Za-z0-9]+", " ", f["filename"].rsplit(".", 1)[0]).strip()
        if stem:
            print(stem); break
' 2>/dev/null || true)
if [ -z "$q_stem" ]; then
  pass "SKIP semantic search: no completed file with a usable filename stem"
else
  hits=$(curl -s -X POST "$O/api/v1/retrieval/query/collection" "${RD[@]}" -H 'Content-Type: application/json' \
    -d "{\"collection_names\":[\"${GDRIVE_KB_ID}\"],\"query\":$(python3 -c 'import sys,json;print(json.dumps(sys.argv[1]))' "$q_stem"),\"k\":4,\"hybrid\":true}" \
    | python3 -c '
import sys,json
d=json.load(sys.stdin)
docs=[]
if isinstance(d,list): docs=d
elif isinstance(d,dict):
    if "documents" in d: docs=[t for sub in d["documents"] for t in (sub if isinstance(sub,list) else [sub])]
    else: docs=d.get("files") or d.get("results") or d.get("docs") or []
print(len(docs))
' 2>/dev/null || echo 0)
  if [ "${hits:-0}" -gt 0 ]; then
    pass "search q=\"$q_stem\" -> ${hits} hit(s) (vectors searchable)"
  else
    fail "search q=\"$q_stem\" -> 0 hits (completed file has vectors but none searchable — check embedding/RAG config)"
    finish
    exit 1
  fi
fi

finish