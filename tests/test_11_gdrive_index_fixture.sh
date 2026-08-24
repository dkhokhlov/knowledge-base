#!/usr/bin/env bash
# System integration test: kb-gateway /index `path` parameter on a small,
# deterministic, committed fixture set (fast `make test` replacement for the
# full real-gdrive drain in test_09).
#
# Indexes gdrive/.tests/ (a dot-dir the full gdrive walk skips, so it never
# contaminates the real gdrive KB) into a throwaway temp KB via
# POST /index?source=gdrive&kb_id=<temp>&path=.tests. The gateway uploads via
# POST /files/ (process_in_background=True) and does NOT link files itself;
# OWUI's per-upload background task is the sole linker (extract -> embed ->
# link). That drain is async, so this test polls GET /status?path=.tests for the
# REAL drain terminal state, audits failures, and runs a deterministic semantic
# search by a fixed marker token.
#
# Self-contained: the temp KB is created with the admin key, granted '*' read so
# the agent (user) key can search it, and deleted on EXIT (its files too). The
# committed fixture files under gdrive/.tests/ are NOT deleted (they are tracked
# in the repo). The fixture set is small text files (.txt/.md/.json) plus minimal
# binary files (.pdf/.docx/.pptx) so the binary extraction path is exercised too.
# The text fixtures extract without markitdown-ocr; the binary fixtures exercise
# the markitdown-ocr path. When OCR is not provisioned the binary fixtures fail
# extraction and surface as a genuine-failure notice (not a hard fail) — the text
# fixtures still complete and the marker search still carries the test.
#
# Tolerant: SKIPs (passes with a notice) when gdrive/.tests has no allowlisted
# files (fixtures not provisioned in this checkout) so `make test` runs clean.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up

O="$(kb_host)"
# Allowlist must match gateway DEFAULT_ALLOW (gateway/app.py). find's default
# Emacs regex treats (a|b) as LITERAL (matches 0 files), so every -iregex call
# below MUST use -regextype posix-extended for the alternation to work.
ALLOW_RE='[.](docx|pdf|pptx|xlsx|txt|md|html|json|log|tex)$'
MARKER="gdrive-fixture-marker-7f3a2"

# --- skip condition ----------------------------------------------------------
src_count=$(find gdrive/.tests -type f -regextype posix-extended -iregex ".*${ALLOW_RE}" 2>/dev/null | wc -l)
if [ "${src_count:-0}" -eq 0 ]; then
  section "gdrive index (fixture)"
  pass "SKIP: gdrive/.tests has no allowlisted fixture files (committed fixtures missing)"
  finish
  exit 0
fi

require_env OPENWEBUI_ADMIN_API_KEY OPENWEBUI_USER_API_KEY || { finish; exit 1; }
AK="$OPENWEBUI_ADMIN_API_KEY"
UK="$OPENWEBUI_USER_API_KEY"
ADM=(-H "Authorization: Bearer $AK")
RD=(-H "Authorization: Bearer $UK")
KB_ID=""

# --- cleanup: delete the temp KB + its files (NOT the committed fixtures) ----
cleanup() {
  local fid
  if [ -n "$KB_ID" ]; then
    # Enumerate files tagged with this KB (covers uploaded-but-unlinked orphans
    # that /knowledge/{id}/files, which lists linked files only, would miss).
    # Same filter as gateway list_file_status (meta.data.knowledge_id). One
    # page (50/page) covers a small temp KB.
    for fid in $(curl -s "$O/api/v1/files/?content=false&page=1" "${ADM[@]}" 2>/dev/null \
        | python3 -c 'import sys,json
try:
  d=json.load(sys.stdin)
except Exception:
  sys.exit(0)
kb=sys.argv[1]
for it in (d.get("items") or []):
  if ((it.get("meta") or {}).get("data") or {}).get("knowledge_id")==kb and it.get("id"):
    print(it["id"])
' "$KB_ID" 2>/dev/null); do
      curl -sf -X DELETE "$O/api/v1/files/${fid}" "${ADM[@]}" >/dev/null 2>&1 \
        || echo "  cleanup: DELETE file ${fid} failed" >&2
    done
    curl -sf -X DELETE "$O/api/v1/knowledge/${KB_ID}/delete" "${ADM[@]}" >/dev/null 2>&1 \
      || echo "  cleanup: DELETE kb ${KB_ID} failed" >&2
  fi
}
trap cleanup EXIT

# --- create a temp KB + grant '*' read so the user key can search it ---------
section "create temp fixture KB"
KB_ID=$(curl -s -X POST "$O/api/v1/knowledge/create" "${ADM[@]}" -H 'Content-Type: application/json' \
  -d '{"name":"gdrive-fixture-test","description":"integration test: /index path fixture set"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
[ -n "$KB_ID" ] && pass "KB id: $KB_ID" || { fail "KB create failed"; finish; exit 1; }

grant=$(curl -s -X POST "$O/api/v1/knowledge/${KB_ID}/access/update" "${ADM[@]}" -H 'Content-Type: application/json' \
  -d "{\"access_grants\":[{\"resource_type\":\"knowledge\",\"resource_id\":\"${KB_ID}\",\"principal_type\":\"user\",\"principal_id\":\"*\",\"permission\":\"read\"}]}")
if printf '%s' "$grant" | python3 -c 'import sys,json;d=json.load(sys.stdin);gs=d.get("access_grants") or [];sys.exit(0 if any(g.get("principal_id")=="*" and g.get("permission")=="read" for g in gs) else 1)' 2>/dev/null; then
  pass "granted '*' read on temp KB"
else
  fail "grant '*' read failed: $(printf '%s' "$grant" | head -c 160)"; finish; exit 1
fi

# --- POST /index (admin): reconcile gdrive/.tests into the temp KB -----------
section "POST /index (kb-gateway, path=.tests)"
idx_resp=$(curl -sS --max-time 1200 -X POST \
  "$O/index?source=gdrive&kb_id=${KB_ID}&path=.tests" \
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
section "poll GET /status (real drain, path=.tests)"
wait_s="${GDRIVE_FIXTURE_WAIT:-180}"
deadline=$(( $(date +%s) + wait_s ))
completed=0; pending=0; processing=0; failed=0; status_json=""
while :; do
  status_json=$(curl -sS "$O/status?source=gdrive&kb_id=${KB_ID}&path=.tests&json=1" "${ADM[@]}" 2>/dev/null || true)
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
  sleep 5
done
in_flight=$(( pending + processing ))
accounted=$(( completed + failed ))
if [ "$in_flight" = "0" ] && [ "$accounted" -ge "$src_count" ]; then
  pass "drain terminal: completed=${completed} failed=${failed} pending=${pending} processing=${processing} source=${src_count}"
else
  fail "drain did not terminate after ${wait_s}s: completed=${completed} failed=${failed} pending=${pending} processing=${processing} source=${src_count} (accounted=${accounted})"
  fail "check: docker logs kb-openwebui"
  finish
  exit 1
fi

# --- failure audit: "Failed to link" = double-link race (must be 0) ----------
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
  pass "no 'Failed to link' errors; ${genuine_n} genuine failure(s) surfaced:"
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
  finish
  exit 1
fi

# --- deterministic semantic search (vectors written + searchable) ------------
# Query the fixed marker token (present in every fixture file) and require >=1
# hit. A completed file has vectors, so the marker must retrieve a document.
# Bounded retry: a freshly completed embed may need a moment before the
# collection is queryable (cold collection).
section "semantic search over fixture KB (deterministic, marker)"
hits=0
for _attempt in 1 2 3; do
  hits=$(curl -s -X POST "$O/api/v1/retrieval/query/collection" "${RD[@]}" -H 'Content-Type: application/json' \
    -d "{\"collection_names\":[\"${KB_ID}\"],\"query\":$(python3 -c 'import sys,json;print(json.dumps(sys.argv[1]))' "$MARKER"),\"k\":4,\"hybrid\":true}" \
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
  [ "${hits:-0}" -gt 0 ] && break
  [ "$_attempt" -lt 3 ] && sleep 5
done
if [ "${hits:-0}" -gt 0 ]; then
  pass "search q=\"${MARKER}\" -> ${hits} hit(s) (vectors searchable)"
else
  fail "search q=\"${MARKER}\" -> 0 hits (completed file has vectors but none searchable — check embedding/RAG config)"
  finish
  exit 1
fi

finish