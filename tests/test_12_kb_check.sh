#!/usr/bin/env bash
# Isolated e2e for make kb-check: provision a throwaway stack (separate compose
# project, NOT the live kb-* stack), upload synthetic files, delete one to
# create the file-{id} leak (the files.py delete() no-op), then exercise
# `make kb-check` detect -> PURGE=1 purge+export -> re-audit 0 orphans.
#
# Uses the reusable isolation in scripts/e2e-env.sh (clone + compose project +
# container-rename override + provision + teardown), so the live stack is never
# touched and the isolation logic is NOT duplicated here. See e2e-env.sh.
#
# The purge path is unit-tested in tests/test_kb_check.py (FakeStores). This
# script verifies the tool against REAL isolated DBs: it reads the clone's
# webui.db + chroma, drops a real orphan collection, reclaims a real segment
# dir, and writes a real export. Synthetic fixtures only (no gdrive, no PII).
#
# Usage: bash tests/test_12_kb_check.sh [KBCHECK_PORT=3020] [OCR_ENABLED=false]
#   KBCHECK_PORT - host port for the isolated Caddy (default 3020; must not
#                  collide with the live KB_HOST_PORT 3000 or e2e 3010).
# Requires: OLLAMA_HOST resolvable (shell env, live .env, or live stack up) and
# the locally-built open-webui overlay image present.
set -u

PORT="${KBCHECK_PORT:-3020}"
OCR="${OCR_ENABLED:-false}"
NAME="kbcheck"
# Marker token embedded in every synthetic file so a later retrieve would find
# them (the test does not rely on retrieval, but the marker is the convention).
MARKER="kbcheck-fixture-marker-9c2d1"

# Source the reusable isolation lib (sets E2E_SRC + the e2e_* functions).
# ORDER MATTERS: lib.sh (next) does `cd "$KB_ROOT"` at SOURCE time, where
# KB_ROOT resolves from BASH_SOURCE to the LIVE repo (this script is invoked
# from there). e2e_isolate below then cds INTO the clone. load_env (called after
# e2e_isolate) reads ./.env from the clone cwd. Swapping these two source lines
# would point load_env + every `make` at the live tree -- keep lib.sh before
# e2e_isolate.
. "$(cd "$(dirname "$0")/.." && pwd)/scripts/e2e-env.sh"

# Source the test helpers (pass/fail/section/finish) for consistent output.
. "$(dirname "$0")/lib.sh"

# Keep PASS/FAIL from lib.sh across the script.
AK=""  # admin key (from the clone's .env.local after provision)
KB_ID=""
DELETED_FID=""
UPLOADED_FIDS=""
ISOLATED=0  # set 1 once e2e_isolate succeeds (we own the clone); the EXIT trap
            # tears down ONLY a clone we created, never a pre-existing leftover.

cleanup() {
  local rc=$?
  # Tear down the isolated stack + remove the clone -- but ONLY if e2e_isolate
  # created it (ISOLATED=1). e2e_isolate stamps a unique clone per run, so it
  # never refuses on a leftover; ISOLATED=1 means THIS run's clone exists and the
  # trap cleans only it. The live stack is never touched (separate project +
  # container names).
  # KBCHECK_KEEP=1 keeps the stack + clone on FAILURE for inspection (mirrors
  # test-e2e-iso's E2E_KEEP); default 0 always cleans up.
  if [ "$ISOLATED" = "1" ]; then
    if [ "$rc" -ne 0 ] && [ "${KBCHECK_KEEP:-0}" = "1" ]; then
      echo "==> KEEP (KBCHECK_KEEP=1): stack left for inspection (port $PORT, project $COMPOSE_PROJECT_NAME, clone $E2E_CLONE); tear down with: make clean-test NAME=$NAME STAMP=$E2E_STAMP" >&2
    else
      e2e_down "$NAME" 2>/dev/null || true
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT

# --- 1. isolate + provision a throwaway stack -------------------------------
section "isolated stack (project kb-$NAME, port $PORT)"
e2e_resolve_ollama || { fail "OLLAMA_HOST resolution failed"; finish; exit 1; }
e2e_isolate "$NAME" "$PORT" "$OCR" || { fail "e2e_isolate failed"; finish; exit 1; }
ISOLATED=1
pass "clone + isolation env ready ($E2E_CLONE)"
# This e2e exercises the Chroma store path (orphan file-{id} collections +
# on-disk segment dirs). The live .env has VECTOR_DB=pgvector; override the
# clone's .env (seeded by e2e_isolate's bootstrap) so the isolated OWUI runs
# on Chroma. kb_check.py requires VECTOR_DB explicitly (no silent default).
sed -i 's/^VECTOR_DB=.*/VECTOR_DB=chroma/' "$E2E_CLONE/.env"
e2e_provision || { fail "e2e_provision failed (start/admin-signup/api-keys)"; finish; exit 1; }
pass "isolated stack up + admin key + ephemeral user provisioned"

# Load the clone's secrets (admin key) the standard way (lib.sh load_env, but in
# the clone cwd -- it reads ./.env + ./.env.local relative to cwd=$E2E_CLONE).
load_env
AK="$OPENWEBUI_ADMIN_API_KEY"
[ -n "$AK" ] || { fail "OPENWEBUI_ADMIN_API_KEY not set in the clone .env.local"; finish; exit 1; }
H="$(kb_host)"
ADM=(-H "Authorization: Bearer $AK")

# --- 2. create a throwaway fixture KB ---------------------------------------
section "create fixture KB"
KB_ID=$(curl -s -X POST "$H/api/v1/knowledge/create" "${ADM[@]}" -H 'Content-Type: application/json' \
  -d "{\"name\":\"kbcheck-fixture\",\"description\":\"isolated e2e for make kb-check ($MARKER)\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
[ -n "$KB_ID" ] && pass "KB id: $KB_ID" || { fail "KB create failed"; finish; exit 1; }

# Grant '*' read so a user key could retrieve (not asserted here, but kept
# consistent with the rest of the test suite's fixture KBs).
curl -sf -X POST "$H/api/v1/knowledge/${KB_ID}/access/update" "${ADM[@]}" -H 'Content-Type: application/json' \
  -d "{\"access_grants\":[{\"resource_type\":\"knowledge\",\"resource_id\":\"${KB_ID}\",\"principal_type\":\"user\",\"principal_id\":\"*\",\"permission\":\"read\"}]}" \
  >/dev/null 2>&1 && pass "granted '*' read on fixture KB" || fail "grant '*' read failed (non-fatal)"

# --- 3. upload 3 synthetic .txt files (OWUI background task extracts+embeds+links) ---
section "upload 3 synthetic files + wait for drain"
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
  local fid
  # NOTE: this function runs in a command-substitution subshell (`fN=$(...)`).
  # stdout is captured into $fid, so diagnostics MUST go to stderr (not stdout,
  # which would be swallowed into $fid and lost). On failure, emit a stderr
  # hint + return 1 with NO stdout; the CALLER records the `fail` (in the parent
  # scope, so the PASS/FAIL counter is correct).
  local resp
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

f1=$(upload_one "alpha.txt"  "$MARKER alpha document one. The quick brown fox.") || { fail "upload alpha.txt failed"; finish; exit 1; }
f2=$(upload_one "beta.txt"   "$MARKER beta document two. Lazy dog jumps over.")  || { fail "upload beta.txt failed"; finish; exit 1; }
f3=$(upload_one "gamma.txt" "$MARKER gamma document three. Sphinx of black quartz.") || { fail "upload gamma.txt failed"; finish; exit 1; }
UPLOADED_FIDS="$f1 $f2 $f3"
pass "uploaded: $UPLOADED_FIDS"

# Poll each file's data.status until completed/failed (OWUI background drain).
# .txt extracts without OCR; embedding needs the shared Ollama (warm = seconds).
#
# Status path: GET /api/v1/files/?content=false (the LIST endpoint) returns
# item.data.status -- this is the proven path the gateway's list_file_status
# uses (the /knowledge/{id}/files join view DEFERS data, status reads null
# there; the single GET /files/{id} returns full data but the list endpoint is
# the documented progress signal). 3 fixture files fit on page 1 (PAGE_SIZE=50).
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
  for fid in $UPLOADED_FIDS; do
    st="$(file_status "$fid")"
    case "$st" in completed|failed) ;; *) all_done=0;; esac
  done
  [ "$all_done" = "1" ] && break
  sleep 3
done
# Re-check the final status of each file.
declare -A FINAL
for fid in $UPLOADED_FIDS; do FINAL[$fid]="$(file_status "$fid")"; done
completed_n=0
for fid in $UPLOADED_FIDS; do [ "${FINAL[$fid]}" = "completed" ] && completed_n=$((completed_n+1)); done
if [ "$completed_n" -eq 3 ]; then
  pass "drain: 3/3 completed (f1=${FINAL[$f1]} f2=${FINAL[$f2]} f3=${FINAL[$f3]})"
else
  fail "drain did not complete 3/3: f1=${FINAL[$f1]} f2=${FINAL[$f2]} f3=${FINAL[$f3]} (waited ${wait_s}s)"
  finish; exit 1
fi

# Sanity: the fixture KB now has 3 linked files (junction) + a live KB collection
# with vectors. Linked-file count: GET /knowledge/{id}/files returns .total (the
# knowledge_file junction rows). Do NOT use GET /knowledge/{id} -- that returns a
# bare KnowledgeModel with NO file_count (file_count is only on the list
# endpoint's KnowledgeUserModel), so it would always read 0 and false-fail.
fc=$(curl -s "$H/api/v1/knowledge/$KB_ID/files" "${ADM[@]}" 2>/dev/null \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("total",0))' 2>/dev/null || echo 0)
[ "${fc:-0}" -eq 3 ] && pass "fixture KB linked files=3 (junction)" \
  || { fail "fixture KB linked files=$fc (expected 3; link step may have failed)"; finish; exit 1; }

# --- 4. delete ONE file -> the file-{id} Chroma collection LEAKS (the bug) ---
section "delete one file (triggers the file-{id} leak)"
DELETED_FID="$f2"
code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$H/api/v1/files/$DELETED_FID" "${ADM[@]}")
[ "$code" = "200" ] && pass "DELETE $DELETED_FID -> 200" \
  || { fail "DELETE $DELETED_FID -> HTTP $code"; finish; exit 1; }
# OWUI's delete removes the file row + junction (CASCADE) + KB-collection
# vectors (patch 4 filter), but the file-{id} Chroma collection stays (files.py
# delete() no-op at line 1075). So file-f2 is now an ORPHAN collection (class 3).
pass "file row + junction + KB vectors removed; file-${DELETED_FID} collection leaked (orphan)"

# --- 5. make kb-check (audit): detect the orphan + knowledge-bases NOT flagged -
section "make kb-check (audit, detect orphan)"
audit_json=$(JSON=1 make kb-check 2>/dev/null)
# Parse the audit JSON. A parse failure (make kb-check returned non-JSON -- e.g.
# the kb-check target / kb_check.py is missing in the clone, or it errored) must
# fail with a clear message + the raw output, NOT a bash "integer expression
# expected" noise from `[ "?" -ge 1 ]`. Validate the count is a non-negative int.
orphan_count=$(printf '%s' "$audit_json" | python3 -c 'import sys,json;print(json.load(sys.stdin)["classes"]["orphan_file_collections"]["count"])' 2>/dev/null || true)
case "$orphan_count" in
  *[!0-9]*|"") fail "audit JSON parse failed (orphan_count='$orphan_count'); make kb-check output:"; printf '%s\n' "$audit_json" | tail -8 >&2; finish; exit 1;;
esac
orphan_samples=$(printf '%s' "$audit_json" | python3 -c 'import sys,json;print(" ".join(json.load(sys.stdin)["classes"]["orphan_file_collections"]["samples"]))' 2>/dev/null || true)
kb_flagged=$(printf '%s' "$audit_json" | python3 -c 'import sys,json
d=json.load(sys.stdin)
# knowledge-bases must NOT appear in any class that enumerates collections
c3=d["classes"]["orphan_file_collections"]["samples"]
print("YES" if any("knowledge-bases" in s for s in c3) else "NO")' 2>/dev/null || echo "?")
if [ "$orphan_count" -ge 1 ] && printf '%s' "$orphan_samples" | grep -q "file-$DELETED_FID"; then
  pass "orphan_file_collections=$orphan_count (includes file-$DELETED_FID)"
else
  fail "orphan_file_collections=$orphan_count samples='$orphan_samples' (expected file-$DELETED_FID)"
  finish; exit 1
fi
[ "$kb_flagged" = "NO" ] && pass "knowledge-bases NOT flagged (blocker-1 exclusion holds)" \
  || { fail "knowledge-bases was flagged by class 3 (blocker-1 regression)"; finish; exit 1; }

# --- 6. PURGE=1 make kb-check: export + drop the orphan + reclaim segment dir -
section "PURGE=1 make kb-check (purge + export)"
purge_out=$(PURGE=1 make kb-check 2>&1)
# The export dir lives under the clone's DATA_ROOT/openwebui/check-exports/<ts>/
# (DATA_ROOT is exported by load_env from .env; default ./data, relative to the
# clone cwd -- strip a leading ./ so it joins $E2E_CLONE cleanly).
_dr="${DATA_ROOT:-./data}"; _dr="${_dr#./}"
export_root="$E2E_CLONE/$_dr/openwebui/check-exports"
latest_export=$(ls -1d "$export_root"/*/ 2>/dev/null | tail -1)
if [ -n "$latest_export" ] && [ -f "$latest_export/manifest.json" ]; then
  purged_n=$(python3 -c 'import sys,json;print(len(json.load(open(sys.argv[1]))["purged_collections"]))' "$latest_export/manifest.json" 2>/dev/null || echo 0)
  jsonl_ok="no"
  [ -f "$latest_export/file-$DELETED_FID.jsonl" ] && jsonl_ok="yes"
  pass "export written: $latest_export (purged_collections=$purged_n, file-$DELETED_FID.jsonl=$jsonl_ok)"
  [ "${purged_n:-0}" -ge 1 ] || { fail "manifest purged_collections=$purged_n (expected >=1)"; finish; exit 1; }
  [ "$jsonl_ok" = "yes" ] || { fail "export JSONL for file-$DELETED_FID missing"; finish; exit 1; }
else
  fail "no export dir / manifest.json found under $export_root"
  echo "$purge_out" | tail -5
  finish; exit 1
fi

# --- 7. re-audit: the orphan is gone (class 3 back to 0 for our collection) ---
section "make kb-check (re-audit: orphan purged)"
re_json=$(JSON=1 make kb-check 2>/dev/null)
re_orphans=$(printf '%s' "$re_json" | python3 -c 'import sys,json;print(json.load(sys.stdin)["classes"]["orphan_file_collections"]["count"])' 2>/dev/null || true)
case "$re_orphans" in
  *[!0-9]*|"") fail "re-audit JSON parse failed (re_orphans='$re_orphans'); make kb-check output:"; printf '%s\n' "$re_json" | tail -8 >&2; finish; exit 1;;
esac
re_samples=$(printf '%s' "$re_json" | python3 -c 'import sys,json;print(" ".join(json.load(sys.stdin)["classes"]["orphan_file_collections"]["samples"]))' 2>/dev/null || true)
# Strict: a fresh isolated stack has NO orphans after purging ours (the 2
# remaining files each have a linked file-{id} collection). Require the count
# back at 0 -- the old `|| ! grep` clause let OTHER orphans slip past as long as
# ours was gone from the capped sample.
if [ "$re_orphans" -eq 0 ]; then
  pass "re-audit orphan_file_collections=0 (file-$DELETED_FID purged)"
else
  fail "re-audit orphan_file_collections=$re_orphans samples='$re_samples' (expected 0)"
  finish; exit 1
fi

# Sanity: the 2 remaining files are still linked (the purge only touched the
# orphan file-{id} collection, not the live KB). Same /files .total endpoint.
fc2=$(curl -s "$H/api/v1/knowledge/$KB_ID/files" "${ADM[@]}" 2>/dev/null \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("total",0))' 2>/dev/null || echo 0)
[ "${fc2:-0}" -eq 2 ] && pass "fixture KB linked files=2 (purge did not touch the live KB)" \
  || { fail "fixture KB linked files=$fc2 (expected 2; purge affected live data)"; finish; exit 1; }

finish