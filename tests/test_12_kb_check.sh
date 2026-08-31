#!/usr/bin/env bash
# Body-only e2e for make kb-check on pgvector: drive the tool against REAL
# isolated DBs (OWUI SQLite + Postgres document_chunk) on the NAMED throwaway
# stack provisioned by the e2e_env_named("kbcheck") fixture.
#
# Target class: 1 (ghost_rows, TIER_SAFE). A ghost is a file row that is
# completed + knowledge_id-tagged but NO longer in the knowledge_file junction.
# It is the backend-INDEPENDENT leak (reads webui.db only; no vector-store
# concept), so it reproduces on pgvector (unlike the old class-3 orphan
# file-{id} Chroma collection, which is Chroma-only and patch 4 cleaned on
# pgvector). The purge is TIER_SAFE: OWUI REST DELETE the ghost file (patch 4
# cleans its KB vectors), no maintenance window.
#
# Reproduction is deterministic: upload 2 files, wait for completed+linked,
# then DELETE one file's junction row directly (sqlite on the running OWUI).
# Its file row stays (completed + KB-tagged) with no junction -> ghost. No
# reliance on any uncertain OWUI delete-cascade. This is the e2e equivalent of
# the unit test's FakeStores injection (tests/test_kb_check.py): real DBs + a
# direct SQL nudge create the exact inconsistency the tool detects + repairs.
#
# The fixture (tests/conftest.py e2e_env_named) owns e2e_isolate + e2e_provision
# + teardown; this script is the BODY ONLY. It runs with cwd=clone in a CLEAN
# child env (only the iso vars + PATH/HOME/LANG/TERM; no operator BASH_ENV, no
# live KB_HOST/KB_API_KEY). The clone .env has VECTOR_DB=pgvector (the
# .env.template default; no sed), so make kb-check takes the pgvector branch.
# Synthetic fixtures only (no gdrive, no PII).
set -u

# Source the test helpers (pass/fail/section/finish). lib.sh does `cd "$KB_ROOT"`
# at SOURCE time, where KB_ROOT resolves from BASH_SOURCE to the CLONE root
# (this script runs with cwd=clone). load_env reads ./.env + ./.env.local
# relative to cwd=$E2E_CLONE.
. "$(dirname "$0")/lib.sh"

load_env
require_env OPENWEBUI_ADMIN_API_KEY || { finish; exit 1; }
# OWUI_CONTAINER is iso-env-only (compose-level; not in .env) -- the fixture
# passes it in the clean child env. make kb-check + the junction-delete docker
# exec both need it.
require_env OWUI_CONTAINER || { finish; exit 1; }

AK="$OPENWEBUI_ADMIN_API_KEY"
H="$(kb_host)"
ADM=(-H "Authorization: Bearer $AK")
# Marker token embedded in every synthetic file (convention; not asserted).
MARKER="kbcheck-fixture-marker-9c2d1"

# --- 1. create a throwaway fixture KB ---------------------------------------
section "create fixture KB"
KB_ID=$(curl -s -X POST "$H/api/v1/knowledge/create" "${ADM[@]}" -H 'Content-Type: application/json' \
  -d "{\"name\":\"kbcheck-fixture\",\"description\":\"isolated e2e for make kb-check pgvector ($MARKER)\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
[ -n "$KB_ID" ] && pass "KB id: $KB_ID" || { fail "KB create failed"; finish; exit 1; }

# --- 2. upload 2 synthetic .txt files (OWUI background task extracts+embeds+links) ---
section "upload 2 synthetic files + wait for drain"
fixture_dir="$E2E_CLONE/.kbcheck-fixtures"
mkdir -p "$fixture_dir"
upload_one() {
  local name="$1" body="$2"
  local path="$fixture_dir/$name"
  printf '%s\n' "$body" > "$path"
  local hsh
  hsh=$(sha256sum "$path" | cut -d' ' -f1)
  # POST /api/v1/files/ multipart: field 'file' (raw bytes, octet-stream) +
  # field 'metadata' (JSON {knowledge_id, file_hash, directory_id}). Mirrors
  # gateway owui.upload_file exactly (same part Content-Types).
  local meta="{\"knowledge_id\":\"$KB_ID\",\"file_hash\":\"$hsh\",\"directory_id\":\"\"}"
  local resp fid
  # Runs in a command-substitution subshell (`fN=$(...)`): stdout is captured
  # into $fid, so diagnostics MUST go to stderr. On failure emit a stderr hint
  # + return 1 with NO stdout; the CALLER records the `fail`.
  resp=$(curl -s -X POST "$H/api/v1/files/" "${ADM[@]}" \
    -F "file=@$path;filename=$name;type=application/octet-stream" \
    -F "metadata=$meta;type=application/json" 2>/dev/null || true)
  fid=$(printf '%s' "$resp" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(0)
print(d.get("id",""))' 2>/dev/null || true)
  if [ -z "$fid" ]; then
    echo "  (upload $name: no file id; OWUI response: $(printf '%s' "$resp" | head -c 200))" >&2
    return 1
  fi
  echo "$fid"
}

f1=$(upload_one "alpha.txt" "$MARKER alpha document one. The quick brown fox.")  || { fail "upload alpha.txt failed"; finish; exit 1; }
f2=$(upload_one "beta.txt"   "$MARKER beta document two. Lazy dog jumps over.") || { fail "upload beta.txt failed"; finish; exit 1; }
pass "uploaded: $f1 $f2"

# Poll each file's data.status until completed/failed (OWUI background drain).
# .txt extracts without OCR; embedding needs the shared Ollama (warm = seconds).
# GET /api/v1/files/?content=false (LIST) returns item.data.status (the proven
# progress signal; the /knowledge/{id}/files join view defers data, status reads
# null there). 2 fixture files fit on page 1 (PAGE_SIZE=50).
file_status() {
  curl -s "$H/api/v1/files/?content=false&page=1" "${ADM[@]}" 2>/dev/null \
    | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    fid=sys.argv[1]
    for it in (d.get("items") or []):
        if it.get("id")==fid:
            print((it.get("data") or {}).get("status","")); break
except Exception:
    print("")' "$1" 2>/dev/null || true
}
wait_s="${KBCHECK_WAIT:-180}"
deadline=$(( $(date +%s) + wait_s ))
all_done=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  all_done=1
  for fid in $f1 $f2; do
    st="$(file_status "$fid")"
    case "$st" in completed|failed) ;; *) all_done=0;; esac
  done
  [ "$all_done" = "1" ] && break
  sleep 3
done
declare -A FINAL
for fid in $f1 $f2; do FINAL[$fid]="$(file_status "$fid")"; done
completed_n=0
for fid in $f1 $f2; do [ "${FINAL[$fid]}" = "completed" ] && completed_n=$((completed_n+1)); done
if [ "$completed_n" -eq 2 ]; then
  pass "drain: 2/2 completed (f1=${FINAL[$f1]} f2=${FINAL[$f2]})"
else
  fail "drain did not complete 2/2: f1=${FINAL[$f1]} f2=${FINAL[$f2]} (waited ${wait_s}s)"
  finish; exit 1
fi

# Sanity: the fixture KB now has 2 linked files (knowledge_file junction rows).
# Use GET /knowledge/{id}/files .total (the junction count); GET /knowledge/{id}
# returns a bare KnowledgeModel with NO file_count (always 0 -> false-fail).
fc=$(curl -s "$H/api/v1/knowledge/$KB_ID/files" "${ADM[@]}" 2>/dev/null \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("total",0))' 2>/dev/null || echo 0)
[ "${fc:-0}" -eq 2 ] && pass "fixture KB linked files=2 (junction)" \
  || { fail "fixture KB linked files=$fc (expected 2; link step may have failed)"; finish; exit 1; }

# --- 3. delete alpha's JUNCTION row -> alpha is a ghost (class 1) -----------
section "delete alpha junction row (-> ghost, class 1)"
ghost_fid="$f1"
# Direct sqlite DELETE of the junction row on the running OWUI (WAL: a one-row
# delete is safe under the 30s busy timeout). The file ROW stays (completed +
# knowledge_id set), so alpha is now completed + KB-tagged + NOT in the junction
# -> a class-1 ghost. beta stays linked (the control).
changed=$(docker exec -i "$OWUI_CONTAINER" python3 - "$ghost_fid" <<'PY'
import sqlite3, sys
fid = sys.argv[1]
c = sqlite3.connect("/app/backend/data/webui.db", timeout=30)
c.execute("DELETE FROM knowledge_file WHERE file_id=?", (fid,))
c.commit()
print(c.total_changes)
PY
)
[ "${changed:-0}" -ge 1 ] && pass "junction row for $ghost_fid deleted ($changed row)" \
  || { fail "junction delete changed $changed rows (expected 1)"; finish; exit 1; }
pass "file row stays (completed + KB-tagged) -> $ghost_fid is a ghost (class 1); beta ($f2) linked control"

# --- 4. make kb-check (audit): detect the ghost -----------------------------
section "make kb-check (audit, detect ghost)"
audit_json=$(JSON=1 make kb-check 2>/dev/null)
# Parse the audit JSON. A parse failure (make kb-check returned non-JSON) must
# fail with a clear message + the raw output, NOT bash "integer expression
# expected" noise from `[ "?" -ge 1 ]`. Validate the count is a non-negative int.
ghost_count=$(printf '%s' "$audit_json" | python3 -c 'import sys,json;print(json.load(sys.stdin)["classes"]["ghost_rows"]["count"])' 2>/dev/null || true)
case "$ghost_count" in
  *[!0-9]*|"") fail "audit JSON parse failed (ghost_count='$ghost_count'); make kb-check output:"; printf '%s\n' "$audit_json" | tail -8 >&2; finish; exit 1;;
esac
ghost_samples=$(printf '%s' "$audit_json" | python3 -c 'import sys,json;print(" ".join(json.load(sys.stdin)["classes"]["ghost_rows"]["samples"]))' 2>/dev/null || true)
if [ "$ghost_count" -ge 1 ] && printf '%s' "$ghost_samples" | grep -q "$ghost_fid"; then
  pass "ghost_rows=$ghost_count (includes $ghost_fid)"
else
  fail "ghost_rows=$ghost_count samples='$ghost_samples' (expected $ghost_fid)"
  finish; exit 1
fi

# --- 5. PURGE=1 make kb-check: export + OWUI REST DELETE the ghost ----------
section "PURGE=1 make kb-check (purge ghost + export)"
purge_out=$(PURGE=1 make kb-check 2>&1)
# The export dir lives under the clone's DATA_ROOT/openwebui/check-exports/<ts>/
# (DATA_ROOT from .env; default ./data, relative to cwd=$E2E_CLONE -- strip a
# leading ./ so it joins $E2E_CLONE cleanly). On pgvector the ghost's file-{id}
# collection has 0 chunks (vectors live in the KB collection_name), so the
# export JSONL is empty -- the manifest still records the purged_collections
# entry (the OWUI REST DELETE of the ghost file is the meaningful step).
_dr="${DATA_ROOT:-./data}"; _dr="${_dr#./}"
export_root="$E2E_CLONE/$_dr/openwebui/check-exports"
latest_export=$(ls -1d "$export_root"/*/ 2>/dev/null | tail -1)
if [ -n "$latest_export" ] && [ -f "$latest_export/manifest.json" ]; then
  purged_n=$(python3 -c 'import sys,json;print(len(json.load(open(sys.argv[1]))["purged_collections"]))' "$latest_export/manifest.json" 2>/dev/null || echo 0)
  pass "export written: $latest_export (purged_collections=$purged_n)"
  [ "${purged_n:-0}" -ge 1 ] || { fail "manifest purged_collections=$purged_n (expected >=1)"; finish; exit 1; }
else
  fail "no export dir / manifest.json found under $export_root"
  echo "$purge_out" | tail -5
  finish; exit 1
fi

# --- 6. re-audit: the ghost is gone (class 1 back to 0) ---------------------
section "make kb-check (re-audit: ghost purged)"
re_json=$(JSON=1 make kb-check 2>/dev/null)
re_ghost=$(printf '%s' "$re_json" | python3 -c 'import sys,json;print(json.load(sys.stdin)["classes"]["ghost_rows"]["count"])' 2>/dev/null || true)
case "$re_ghost" in
  *[!0-9]*|"") fail "re-audit JSON parse failed (re_ghost='$re_ghost'); make kb-check output:"; printf '%s\n' "$re_json" | tail -8 >&2; finish; exit 1;;
esac
if [ "$re_ghost" -eq 0 ]; then
  pass "re-audit ghost_rows=0 ($ghost_fid purged)"
else
  fail "re-audit ghost_rows=$re_ghost (expected 0)"
  finish; exit 1
fi

# Sanity: the purge deleted alpha's file row (OWUI REST DELETE); beta is still
# linked (the purge only touched the ghost). Same /files .total endpoint.
fc2=$(curl -s "$H/api/v1/knowledge/$KB_ID/files" "${ADM[@]}" 2>/dev/null \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("total",0))' 2>/dev/null || echo 0)
[ "${fc2:-0}" -eq 1 ] && pass "fixture KB linked files=1 (beta intact; alpha purged)" \
  || { fail "fixture KB linked files=$fc2 (expected 1; purge affected beta)"; finish; exit 1; }

finish