#!/usr/bin/env bash
# Body-only e2e for make kb-check on pgvector: drive the tool against REAL
# isolated DBs (OWUI SQLite + Postgres document_chunk) on the NAMED throwaway
# stack provisioned by the e2e_env_named("kbcheck") fixture.
#
# Target classes: 1 (ghost_rows, TIER_ADVISORY) + 3 (orphan_file_collections,
# TIER_SAFE). A ghost is a file row that is completed + knowledge_id-tagged but
# NO longer in the knowledge_file junction. It is the backend-INDEPENDENT leak
# (reads webui.db only; no vector-store concept), so it reproduces on pgvector
# (unlike the old class-3 orphan file-{id} Chroma collection, which was
# Chroma-only and patch 4 cleaned on pgvector). Class 1 is ADVISORY (de-tiered:
# /knowledge/{id}/file/remove?delete_file=false leaves a legitimate ghost; a
# purge would delete a live file). The SAFE purge is class 3 only: orphan
# file-{id} collections (no file DB row), direct Postgres delete, OWUI live.
#
# Reproduction is deterministic: upload 3 files (alpha, beta, gamma), wait for
# completed+linked, then DELETE alpha's junction row directly (sqlite on the
# running OWUI). Its file row stays (completed + KB-tagged) with no junction ->
# ghost (class 1, advisory). PURGE=1 must NOT purge it (assert it survives). A
# genuine class-3 orphan is then made by deleting gamma's FILE row (its file-{id}
# collection, which /knowledge/{id}/file/add reads into the KB collection and
# never deletes, is now an orphan WITH chunks); PURGE=1 exports + deletes it.
# NOTE: deleting gamma's file row also leaves a class-5b leak (gamma's KB-level
# vectors) + a class-7 orphan junction (gamma) that persist through sections 7-9
# (neither is touched by the safe PURGE or PRUNE_KB path); no assertion breaks.
# This is the e2e equivalent of the unit test's FakeStores injection
# (tests/test_kb_check.py): real DBs + a direct SQL nudge create the exact
# inconsistency the tool detects + repairs.
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

# --- 2. upload 3 synthetic .txt files (OWUI background task extracts+embeds+links) ---
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
f3=$(upload_one "gamma.txt"  "$MARKER gamma document three. The cat sat on the mat.") || { fail "upload gamma.txt failed"; finish; exit 1; }
pass "uploaded: $f1 $f2 $f3"

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
  for fid in $f1 $f2 $f3; do
    st="$(file_status "$fid")"
    case "$st" in completed|failed) ;; *) all_done=0;; esac
  done
  [ "$all_done" = "1" ] && break
  sleep 3
done
declare -A FINAL
for fid in $f1 $f2 $f3; do FINAL[$fid]="$(file_status "$fid")"; done
completed_n=0
for fid in $f1 $f2 $f3; do [ "${FINAL[$fid]}" = "completed" ] && completed_n=$((completed_n+1)); done
if [ "$completed_n" -eq 3 ]; then
  pass "drain: 3/3 completed (f1=${FINAL[$f1]} f2=${FINAL[$f2]} f3=${FINAL[$f3]})"
else
  fail "drain did not complete 3/3: f1=${FINAL[$f1]} f2=${FINAL[$f2]} f3=${FINAL[$f3]} (waited ${wait_s}s)"
  finish; exit 1
fi

# Sanity: the fixture KB now has 3 linked files (knowledge_file junction rows).
# Use GET /knowledge/{id}/files .total (the junction count); GET /knowledge/{id}
# returns a bare KnowledgeModel with NO file_count (always 0 -> false-fail).
fc=$(curl -s "$H/api/v1/knowledge/$KB_ID/files" "${ADM[@]}" 2>/dev/null \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("total",0))' 2>/dev/null || echo 0)
[ "${fc:-0}" -eq 3 ] && pass "fixture KB linked files=3 (junction)" \
  || { fail "fixture KB linked files=$fc (expected 3; link step may have failed)"; finish; exit 1; }

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
pass "file row stays (completed + KB-tagged) -> $ghost_fid is a ghost (class 1); beta ($f2) + gamma ($f3) linked controls"

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

# --- 5. PURGE=1 on advisory-only fixture: the class-1 ghost is NOT purged -----
section "PURGE=1 make kb-check (advisory ghost NOT purged)"
# Class 1 is TIER_ADVISORY (de-tiered): the safe purge (class 3) has nothing to
# do, so the manifest is written with an EMPTY purged_collections list, and the
# ghost SURVIVES. This is the assertion a future regression that re-purges
# class 1 would catch.
# The export dir lives under the clone's DATA_ROOT/openwebui/check-exports/<ts>/
# (DATA_ROOT from .env; default ./data, relative to cwd=$E2E_CLONE -- strip a
# leading ./ so it joins $E2E_CLONE cleanly).
_dr="${DATA_ROOT:-./data}"; _dr="${_dr#./}"
export_root="$E2E_CLONE/$_dr/openwebui/check-exports"
_prev_latest=$(ls -1d "$export_root"/*/ 2>/dev/null | tail -1)
purge_out=$(PURGE=1 make kb-check 2>&1)
latest_export=$(ls -1d "$export_root"/*/ 2>/dev/null | tail -1)
if [ -n "$latest_export" ] && [ "$latest_export" != "$_prev_latest" ] && [ -f "$latest_export/manifest.json" ]; then
  purged_n=$(python3 -c 'import sys,json;print(len(json.load(open(sys.argv[1]))["purged_collections"]))' "$latest_export/manifest.json" 2>/dev/null || echo 0)
  if [ "${purged_n:-0}" -eq 0 ]; then
    pass "advisory-only purge: purged_collections=0 (class-1 ghost NOT purged; safe tier is class 3)"
  else
    fail "advisory-only purge purged_collections=$purged_n (expected 0; class 1 is advisory)"; finish; exit 1
  fi
else
  fail "no NEW export dir / manifest.json after PURGE=1 under $export_root"
  echo "$purge_out" | tail -5
  finish; exit 1
fi
# Re-audit: the ghost SURVIVES (advisory -> untouched).
re_json=$(JSON=1 make kb-check 2>/dev/null)
re_ghost=$(printf '%s' "$re_json" | python3 -c 'import sys,json;print(json.load(sys.stdin)["classes"]["ghost_rows"]["count"])' 2>/dev/null || true)
case "$re_ghost" in
  *[!0-9]*|"") fail "re-audit JSON parse failed (re_ghost='$re_ghost'); make kb-check output:"; printf '%s\n' "$re_json" | tail -8 >&2; finish; exit 1;;
esac
if [ "$re_ghost" -eq 1 ]; then
  pass "re-audit ghost_rows=1 ($ghost_fid survives; advisory untouched)"
else
  fail "re-audit ghost_rows=$re_ghost (expected 1; class 1 must NOT be purged)"
  finish; exit 1
fi

# --- 5b. make a genuine class-3 orphan: delete gamma's FILE row --------------
section "delete gamma file row (-> class-3 orphan file-{id} collection)"
# gamma's file row gone -> its file-{id} collection now has no file row (class 3).
# /knowledge/{id}/file/add reads file-{id} into the KB collection and never
# deletes it, so the file-{id} collection HAS chunks (confirmed live: 452 file-*
# for 452 file rows). NOTE: this also leaves a class-5b leak (gamma's KB-level
# vectors) + a class-7 orphan junction (gamma) that persist through sections 7-9
# (neither is touched by the safe PURGE or PRUNE_KB path); no assertion breaks.
g3="$f3"
changed=$(docker exec -i "$OWUI_CONTAINER" python3 - "$g3" <<'PY'
import sqlite3, sys
fid = sys.argv[1]
c = sqlite3.connect("/app/backend/data/webui.db", timeout=30)
c.execute("DELETE FROM file WHERE id=?", (fid,))
c.commit()
print(c.total_changes)
PY
)
[ "${changed:-0}" -ge 1 ] && pass "gamma file row deleted ($changed row) -> file-$g3 is a class-3 orphan" \
  || { fail "gamma file delete changed $changed rows (expected 1)"; finish; exit 1; }

# --- 6. PURGE=1: export + DELETE the class-3 orphan; ghost still alive --------
section "PURGE=1 make kb-check (purge class-3 orphan + export)"
_prev_latest2=$(ls -1d "$export_root"/*/ 2>/dev/null | tail -1)
purge_out2=$(PURGE=1 make kb-check 2>&1)
latest_export2=$(ls -1d "$export_root"/*/ 2>/dev/null | tail -1)
if [ -n "$latest_export2" ] && [ "$latest_export2" != "$_prev_latest2" ] && [ -f "$latest_export2/manifest.json" ]; then
  purged_n2=$(python3 -c 'import sys,json;print(len(json.load(open(sys.argv[1]))["purged_collections"]))' "$latest_export2/manifest.json" 2>/dev/null || echo 0)
  [ "${purged_n2:-0}" -ge 1 ] && pass "class-3 purge: purged_collections=$purged_n2 (file-$g3 exported + deleted)" \
    || { fail "class-3 purge purged_collections=$purged_n2 (expected >=1)"; finish; exit 1; }
else
  fail "no NEW export dir / manifest.json after class-3 PURGE=1 under $export_root"
  echo "$purge_out2" | tail -5
  finish; exit 1
fi
# Re-audit: the class-3 orphan is gone; the class-1 ghost is STILL alive.
re2=$(JSON=1 make kb-check 2>/dev/null)
re_orphan=$(printf '%s' "$re2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["classes"]["orphan_file_collections"]["count"])' 2>/dev/null || true)
case "$re_orphan" in
  *[!0-9]*|"") fail "re-audit JSON parse failed (re_orphan='$re_orphan'); make kb-check output:"; printf '%s\n' "$re2" | tail -8 >&2; finish; exit 1;;
esac
[ "$re_orphan" -eq 0 ] && pass "re-audit orphan_file_collections=0 (class-3 purged)" \
  || { fail "re-audit orphan_file_collections=$re_orphan (expected 0)"; finish; exit 1; }
re_ghost2=$(printf '%s' "$re2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["classes"]["ghost_rows"]["count"])' 2>/dev/null || true)
[ "${re_ghost2:-0}" -eq 1 ] && pass "re-audit ghost_rows=1 ($ghost_fid still advisory; untouched by class-3 purge)" \
  || { fail "re-audit ghost_rows=$re_ghost2 (expected 1; class 1 must survive the class-3 purge)"; finish; exit 1; }

# Sanity: the class-3 purge deleted ONLY gamma's file-{id} collection (a direct
# Postgres delete); no file ROWS were touched. alpha (ghost) + beta (linked)
# file rows survive; gamma's file row is gone (we deleted it in 5b).
file_exists() {
  curl -s "$H/api/v1/files/$1" "${ADM[@]}" 2>/dev/null \
  | python3 -c 'import sys,json
try:
    print("yes" if json.load(sys.stdin).get("id") else "no")
except Exception:
    print("no")' 2>/dev/null || echo no
}
[ "$(file_exists "$f1")" = "yes" ] && pass "alpha file row survives (ghost, not purged)" \
  || { fail "alpha file row vanished (class-3 purge must not touch file rows)"; finish; exit 1; }
[ "$(file_exists "$f2")" = "yes" ] && pass "beta file row survives (linked control)" \
  || { fail "beta file row vanished (class-3 purge must not touch file rows)"; finish; exit 1; }
[ "$(file_exists "$g3")" = "no" ] && pass "gamma file row gone (deleted in 5b for the class-3 orphan)" \
  || { fail "gamma file row still present (expected deleted in 5b)"; finish; exit 1; }

# --- 7. class 11: stale root KB (source=root, no backing dir) ---------------
# Create a source=root KB whose ./root/<name>/ dir does NOT exist -> stale. The
# class-11 detector parses the source= kv from the description (created by hand
# here to match what kb-bootstrap.sh writes; the gateway is NOT needed -- the
# e2e child env has no live KB_HOST). Synthetic name + description (no PII).
# NOTE: in the e2e CLONE, ./root/* is gitignored (only .tests/ is tracked), so
# ROOT_DIRS=[] -> every source=root KB is stale (legitimate per the design: an
# empty scan means no backing dirs). This named throwaway stack is the only
# place prune runs, so pruning root KBs here is safe.
section "make kb-check (class 11: stale root KB)"
STALE_NAME="stale-iso-kbcheck"
STALE_DESC="Indexed from local root/$STALE_NAME/ via api-gateway | source=root | host=testhost | path=$STALE_NAME"
STALE_ID=$(curl -s -X POST "$H/api/v1/knowledge/create" "${ADM[@]}" -H 'Content-Type: application/json' \
  -d "{\"name\":\"$STALE_NAME\",\"description\":\"$STALE_DESC\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
[ -n "$STALE_ID" ] && pass "stale root KB id: $STALE_ID (name=$STALE_NAME, no backing dir)" \
  || { fail "stale root KB create failed"; finish; exit 1; }

# Helper: space-joined KB ids whose parsed description source == $1.
kbs_by_source() {
  curl -s "$H/api/v1/knowledge/?page=1" "${ADM[@]}" 2>/dev/null \
  | python3 -c 'import sys,json
src=sys.argv[1]
def parse(d):
    kv={}
    for t in (d or "").split("|"):
        t=t.strip()
        if "=" in t:
            k,_,v=t.partition("="); kv[k.strip()]=v.strip()
    if "source" in kv: return kv["source"]
    if (d or "").startswith("Indexed from local root/"): return "root"
    if (d or "").startswith("Indexed from local "): return "root"
    if (d or "").startswith("Claude projects memory"): return "projects-memory"
    return "unknown"
data=json.load(sys.stdin)
items=data.get("items",[]) if isinstance(data,dict) else (data or [])
print(" ".join(i.get("id","") for i in items if parse(i.get("description",""))==src))' "$1" 2>/dev/null || true
}
# Snapshot the projects-memory KBs BEFORE any prune (the prune must NEVER
# touch source=projects-memory KBs; their backing is ~/.claude/projects/).
PROJ_BEFORE="$(kbs_by_source projects-memory)"

audit2=$(JSON=1 make kb-check 2>/dev/null)
stale_samples=$(printf '%s' "$audit2" | python3 -c 'import sys,json;print(" ".join(json.load(sys.stdin)["classes"]["stale_root_kb"]["samples"]))' 2>/dev/null || true)
if printf '%s' "$stale_samples" | grep -q "$STALE_ID"; then
  pass "stale_root_kb flags $STALE_ID"
else
  fail "stale_root_kb samples='$stale_samples' (expected to include $STALE_ID)"
  printf '%s\n' "$audit2" | tail -8 >&2
  finish; exit 1
fi
# The audit advises PRUNE_KB=1 for class 11.
printf '%s' "$audit2" | python3 -c 'import sys,json
ad=json.load(sys.stdin).get("advised_commands",[])
sys.exit(0 if any("PRUNE_KB=1" in c for c in ad) else 1)' \
  || { fail "advised_commands missing PRUNE_KB=1 for class 11"; finish; exit 1; }
pass "advised: PRUNE_KB=1 make kb-check"

# --- 8. negative gates -----------------------------------------------------
section "PRUNE_KB negative gates"
# PRUNE_KB=0 must NOT prune: the stale KB survives an audit.
if PRUNE_KB=0 make kb-check >/dev/null 2>&1; then :; fi
stale_exists=$(curl -s "$H/api/v1/knowledge/$STALE_ID" "${ADM[@]}" 2>/dev/null \
  | python3 -c 'import sys,json
try:
    print("yes" if json.load(sys.stdin).get("id") else "no")
except Exception:
    print("no")' 2>/dev/null || echo no)
[ "$stale_exists" = "yes" ] && pass "PRUNE_KB=0 did NOT prune (stale KB survives)" \
  || { fail "PRUNE_KB=0 pruned the stale KB (must not)"; finish; exit 1; }

# PRUNE_KB=1 BACKUP=0 -> error (backup is mandatory for prune).
if PRUNE_KB=1 BACKUP=0 make kb-check >/tmp/kbc_nobackup.out 2>&1; then rc=0; else rc=$?; fi
if [ "${rc:-0}" -ne 0 ] && grep -q "requires a backup" /tmp/kbc_nobackup.out; then
  pass "PRUNE_KB=1 BACKUP=0 rejected (rc=$rc)"
else
  fail "PRUNE_KB=1 BACKUP=0 not rejected (rc=$rc)"; tail -5 /tmp/kbc_nobackup.out >&2; finish; exit 1
fi

# PRUNE_KB=1 MAINT=1 -> Makefile incompat error (prune needs OWUI running).
if PRUNE_KB=1 MAINT=1 make kb-check >/tmp/kbc_maint.out 2>&1; then rc=0; else rc=$?; fi
if [ "${rc:-0}" -ne 0 ] && grep -q "incompatible with MAINT=1" /tmp/kbc_maint.out; then
  pass "PRUNE_KB=1 MAINT=1 rejected (rc=$rc)"
else
  fail "PRUNE_KB=1 MAINT=1 not rejected (rc=$rc)"; tail -5 /tmp/kbc_maint.out >&2; finish; exit 1
fi

# PRUNE_KB=1 REPAIR=1 -> same incompat error.
if PRUNE_KB=1 REPAIR=1 make kb-check >/tmp/kbc_repair.out 2>&1; then rc=0; else rc=$?; fi
if [ "${rc:-0}" -ne 0 ] && grep -q "incompatible with MAINT=1" /tmp/kbc_repair.out; then
  pass "PRUNE_KB=1 REPAIR=1 rejected (rc=$rc)"
else
  fail "PRUNE_KB=1 REPAIR=1 not rejected (rc=$rc)"; tail -5 /tmp/kbc_repair.out >&2; finish; exit 1
fi

# Stale KB still present (all negative gates are non-mutating).
stale_exists2=$(curl -s "$H/api/v1/knowledge/$STALE_ID" "${ADM[@]}" 2>/dev/null \
  | python3 -c 'import sys,json
try:
    print("yes" if json.load(sys.stdin).get("id") else "no")
except Exception:
    print("no")' 2>/dev/null || echo no)
[ "$stale_exists2" = "yes" ] && pass "stale KB survives all negative gates" \
  || { fail "stale KB vanished after a negative gate (should be non-mutating)"; finish; exit 1; }

# --- 9. PRUNE_KB=1 make kb-check: backup + DELETE the stale root KB ----------
section "PRUNE_KB=1 make kb-check (prune stale root KB + backup)"
_prune_export_root="$E2E_CLONE/${_dr#./}/openwebui/check-exports"
# Mark the current latest export so we can identify the prune's NEW dir after.
_prev_latest=$(ls -1d "$_prune_export_root"/*/ 2>/dev/null | tail -1)
if PRUNE_KB=1 make kb-check >/tmp/kbc_prune.out 2>&1; then rc=0; else rc=$?; fi
[ "${rc:-0}" -eq 0 ] || { fail "PRUNE_KB=1 make kb-check failed (rc=$rc)"; tail -10 /tmp/kbc_prune.out >&2; finish; exit 1; }
pass "PRUNE_KB=1 make kb-check rc=0"
# A NEW export dir was created (the prune's strict backup, with a fresh ts).
_new_latest=$(ls -1d "$_prune_export_root"/*/ 2>/dev/null | tail -1)
if [ -n "$_new_latest" ] && [ "$_new_latest" != "$_prev_latest" ]; then
  pass "new export dir: $_new_latest"
else
  fail "no new export dir after prune (prev=$_prev_latest new=$_new_latest)"; finish; exit 1
fi
# The strict backup <kb_id>.jsonl was written before the DELETE.
_backup="$_new_latest/$STALE_ID.jsonl"
[ -f "$_backup" ] && pass "backup written: $_backup" \
  || { fail "no backup file $_backup"; ls -la "$_new_latest" >&2; finish; exit 1; }
# The prune manifest records the pruned KB.
if [ -f "$_new_latest/prune-manifest.json" ]; then
  _pn=$(python3 -c 'import sys,json;print(len(json.load(open(sys.argv[1]))["pruned_kbs"]))' "$_new_latest/prune-manifest.json" 2>/dev/null || echo 0)
  [ "${_pn:-0}" -ge 1 ] && pass "prune-manifest: pruned_kbs=$_pn" \
    || { fail "prune-manifest pruned_kbs=$_pn (expected >=1)"; finish; exit 1; }
else
  fail "no prune-manifest.json in $_new_latest"; finish; exit 1
fi

# The stale KB is gone from OWUI (DELETE /knowledge/{id}/delete).
stale_gone=$(curl -s "$H/api/v1/knowledge/$STALE_ID" "${ADM[@]}" 2>/dev/null \
  | python3 -c 'import sys,json
try:
    print("no" if json.load(sys.stdin).get("id") else "no")
except Exception:
    print("no")' 2>/dev/null || echo no)
[ "$stale_gone" = "no" ] && pass "stale root KB $STALE_ID deleted from OWUI" \
  || { fail "stale root KB still exists after prune"; finish; exit 1; }

# Guard: NO projects-memory KB was pruned (the prune touches source=root only).
PROJ_AFTER="$(kbs_by_source projects-memory)"
[ "$PROJ_BEFORE" = "$PROJ_AFTER" ] && pass "projects-memory KBs untouched by prune" \
  || { fail "prune touched projects-memory KBs: before=[$PROJ_BEFORE] after=[$PROJ_AFTER]"; finish; exit 1; }

# Re-audit: class 11 no longer flags STALE_ID.
re2=$(JSON=1 make kb-check 2>/dev/null)
re_samples=$(printf '%s' "$re2" | python3 -c 'import sys,json;print(" ".join(json.load(sys.stdin)["classes"]["stale_root_kb"]["samples"]))' 2>/dev/null || true)
if printf '%s' "$re_samples" | grep -q "$STALE_ID"; then
  fail "re-audit still flags $STALE_ID after prune (samples='$re_samples')"; finish; exit 1
fi
pass "re-audit: $STALE_ID no longer stale"

finish