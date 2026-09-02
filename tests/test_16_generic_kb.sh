#!/usr/bin/env bash
# System integration test: GENERIC (non-gdrive) KB under ./root/<name>/.
#
# Proves the parts of the ./root/ multi-KB design that the gdrive + .tests iso
# tests do NOT cover:
#   1. The additive .kb-ignore ancestor-chain deny-list on a NON-gdrive KB:
#      root/.kb-ignore (globals) denies a type for EVERY KB, and a per-KB
#      root/<name>/.kb-ignore ADDS a further deny for that one KB. A `!` negation
#      in the globals RE-INCLUDES a file a shallower pattern denied (gentest.md is
#      denied by global *.md then re-included by !gentest.md -> still indexed).
#      DEFAULT_ALLOW (gateway/app.py) = docx pdf pptx xlsx txt md html json log
#      tex, so .json + .log + .md ARE allowlisted -> it is the .kb-ignore (not the
#      allowlist) that must drop .json + .log, and the `!` that must keep .md.
#   2. The generic shell pipeline BY NAME (no gdrive, no GDRIVE_KB_ID):
#        make kb-bootstrap KB=<name>  (find-or-create + grant user:* read)
#        make kb-sync KB=<name>       (POST /index?dir=<name>&kb_id=<id>)
#        poll GET /status?dir=<name>  (drain terminal)
#        make kb-finalize KB=<name>   (global-terminal guard + flock + REINDEX)
#
# Drops a synthetic ./root/gentest/ tree + root/.kb-ignore + root/gentest/.kb-ignore
# in the throwaway iso clone, then runs the pipeline. Asserts:
#   - source_count == 2 (post-ignore): .md (re-included by !) + .txt counted;
#     .json + .log dropped at walk time (the ignore is upstream of the manifest).
#   - indexed_files == {gentest.md, gentest.txt}; .json + .log absent.
#   - semantic search for a fixed marker returns hits (vectors written + the
#     REINDEX published them; ivfflat folds post-build rows in only on REINDEX).
#
# Self-contained: deletes the gentest KB + its files on EXIT. The synthetic
# ./root/gentest/ + .kb-ignore files live only in the throwaway clone (destroyed
# on teardown). No gdrive, no real corpus, no PII. Uses a unique marker so a
# re-run in a kept (failed) clone does not collide with stale vectors.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up

O="$(kb_host)"
NAME="gentest"
MARKER="gentest-marker-3c5e1"
ROOT_DIR="root/${NAME}"
KBIGNORE="root/.kb-ignore"

require_env OPENWEBUI_ADMIN_API_KEY KB_API_KEY || { finish; exit 1; }
AK="$OPENWEBUI_ADMIN_API_KEY"
UK="$KB_API_KEY"
ADM=(-H "Authorization: Bearer $AK")
RD=(-H "Authorization: Bearer $UK")
KB_ID=""

# --- cleanup: delete the gentest KB + its files (NOT ./root fixtures; clone-only) -
cleanup() {
  local fid
  if [ -n "$KB_ID" ]; then
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
  rm -rf "$ROOT_DIR" "$KBIGNORE" 2>/dev/null || true
}
trap cleanup EXIT

# --- drop a synthetic ./root/gentest/ tree + .kb-ignore files ------------------
section "synthetic ./root/gentest/ + .kb-ignore"
mkdir -p "$ROOT_DIR"
printf '# gentest doc\n\n%s alpha content for semantic search over the generic KB.\n' "$MARKER" > "$ROOT_DIR/gentest.md"
printf '%s beta plain-text content for semantic search.\n' "$MARKER" > "$ROOT_DIR/gentest.txt"
# .json: allowlisted, denied by the GLOBAL root/.kb-ignore -> must NOT index.
printf '{"marker": "%s", "note": "denied by global *.json"}\n' "$MARKER" > "$ROOT_DIR/gentest.json"
# .log: allowlisted, denied by the PER-KB root/gentest/.kb-ignore -> must NOT index.
printf '%s log line denied by per-KB *.log (additive)\n' "$MARKER" > "$ROOT_DIR/gentest.log"
# root/.kb-ignore (globals): deny *.json + *.md, then !gentest.md re-includes the
# .md (proves `!` negation: gentest.md survives -> indexed; without the ! it would
# be dropped by *.md -> source_count would be 1, failing the assert below).
cat > "$KBIGNORE" <<EOF
# Global deny-list (applies to EVERY KB; rules relative to ./root).
*.json
*.md
!gentest.md
EOF
# root/gentest/.kb-ignore (per-KB additive): further deny *.log for this KB only.
cat > "$ROOT_DIR/.kb-ignore" <<EOF
# Per-KB additive deny (further denies for the gentest KB only; relative to gentest/).
*.log
EOF
ls -1 "$ROOT_DIR"
pass "dropped 4 fixtures (md, txt, json, log) + .kb-ignore (global *.json *.md !gentest.md; per-KB *.log)"

# --- make kb-bootstrap KB=gentest (find-or-create + grant user:* read) ---------
section "make kb-bootstrap KB=${NAME}"
KB_ID=$(make kb-bootstrap KB="$NAME" 2>/dev/null | tail -1)
if [ -n "$KB_ID" ]; then
  pass "bootstrapped + granted read: KB ${NAME} -> ${KB_ID}"
else
  fail "kb-bootstrap KB=${NAME} produced no kb_id"; finish; exit 1
fi

# --- make kb-sync KB=gentest (POST /index?dir=gentest&kb_id=<id>) --------------
section "make kb-sync KB=${NAME} (reconcile)"
sync_out=$(make kb-sync KB="$NAME" 2>&1) || true
printf '%s\n' "$sync_out" | grep -E 'added=|FAIL|OK|DONE' | tail -5
added=$(printf '%s\n' "$sync_out" | sed -n 's/.*added=\([0-9][0-9]*\).*/\1/p' | head -1)
if [ "${added:-0}" -gt 0 ] 2>/dev/null; then
  pass "kb-sync: added=${added} (the .md + .txt; .json + .log excluded upstream)"
else
  fail "kb-sync added=0 (expected 2: the .kb-ignore may have over-denied, or /index failed)"; finish; exit 1
fi

# --- poll GET /status until the drain is terminal ------------------------------
section "poll GET /status (dir=${NAME})"
wait_s="${GENERIC_KB_WAIT:-240}"
deadline=$(( $(date +%s) + wait_s ))
completed=0; pending=0; processing=0; failed=0; src_count=0; status_json=""
while :; do
  status_json=$(curl -sS "$O/status?dir=${NAME}&kb_id=${KB_ID}&json=1" "${ADM[@]}" 2>/dev/null || true)
  read -r completed pending processing failed src_count < <(printf '%s' "$status_json" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get("indexed_count",0), d.get("pending",0), d.get("processing",0),
          d.get("failed",0), d.get("source_count",0))
except Exception:
    print("0 0 0 0 0")
')
  in_flight=$(( pending + processing ))
  accounted=$(( completed + failed ))
  if [ "$in_flight" = "0" ] && [ "$accounted" -ge "$src_count" ] 2>/dev/null; then break; fi
  if [ "$(date +%s)" -ge "$deadline" ]; then break; fi
  sleep 5
done
in_flight=$(( pending + processing ))
accounted=$(( completed + failed ))
if [ "$in_flight" = "0" ] && [ "$accounted" -ge "$src_count" ] 2>/dev/null; then
  pass "drain terminal: completed=${completed} failed=${failed} pending=${pending} processing=${processing} source=${src_count}"
else
  fail "drain did not terminate after ${wait_s}s: completed=${completed} failed=${failed} pending=${pending} processing=${processing} source=${src_count}"
  fail "check: docker logs ${OWUI_CONTAINER:-kb-openwebui}"
  finish; exit 1
fi

# --- assert: source_count == 2 (post-exclude); .json + .log dropped at walk time -
section "exclude semantics (source_count + indexed_files)"
if [ "$src_count" = "2" ]; then
  pass "source_count=${src_count} (post-exclude: .md + .txt counted; .json + .log dropped at walk time)"
else
  fail "source_count=${src_count} != 2 (global *.json or per-KB *.log not applied, or !gentest.md re-include broken -- .kb-ignore broken)"
  finish; exit 1
fi
# Parse indexed_files filenames; assert the exact allowlisted set + deny absent.
excl_out=$(printf '%s' "$status_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
idx = d.get("indexed_files") or []
names = sorted((f.get("filename") or "") for f in idx)
print(" ".join(names))
' 2>/dev/null || echo "")
have_md=0; have_txt=0; have_json=0; have_log=0
for n in $excl_out; do
  case "$n" in
    gentest.md) have_md=1 ;;
    gentest.txt) have_txt=1 ;;
    gentest.json) have_json=1 ;;
    gentest.log) have_log=1 ;;
  esac
done
if [ "$have_md" = "1" ] && [ "$have_txt" = "1" ] && [ "$have_json" = "0" ] && [ "$have_log" = "0" ]; then
  pass "indexed_files={gentest.md, gentest.txt}; .json (global) + .log (per-KB) excluded; gentest.md kept via !gentest.md re-include"
else
  fail "indexed_files mismatch: md=${have_md} txt=${have_txt} json=${have_json} log=${have_log} (expected md=1 txt=1 json=0 log=0)"
  fail "got: ${excl_out}"
  finish; exit 1
fi

# --- make kb-finalize KB=gentest (global-terminal guard + flock + REINDEX) ------
# Proves the B11 global-terminal guard (every ./root KB terminal) + the host lock
# + the ivfflat/GIN REINDEX on a generic KB. pgvector-only; the iso stack runs
# pgvector (rag-config). POSTGRES_CONTAINER is iso-injected; VECTOR_DB + PGVECTOR_*
# come from the clone .env (kb-finalize.sh sources it).
section "make kb-finalize KB=${NAME} (REINDEX ivfflat + GIN FTS)"
fin_out=$(make kb-finalize KB="$NAME" 2>&1) || true
printf '%s\n' "$fin_out" | grep -E '==>|DONE|FAIL|SKIP|WARN' | tail -6
if printf '%s\n' "$fin_out" | grep -q 'DONE  kb-finalize'; then
  pass "kb-finalize: REINDEX complete (ivfflat + GIN FTS; global-terminal guard + lock passed)"
else
  fail "kb-finalize did not complete:"; printf '%s\n' "$fin_out" | tail -8 >&2
  finish; exit 1
fi

# --- semantic search (vectors written + REINDEX-published) ---------------------
# Query the fixed marker (present in the two indexed files) and require >=1 hit.
# Bounded retry: a freshly REINDEXed collection may need a moment before it is
# queryable (cold collection / ivfflat lists just rebuilt).
section "semantic search over generic KB (marker)"
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
  pass "search q=\"${MARKER}\" -> ${hits} hit(s) (generic KB vectors searchable post-REINDEX)"
else
  fail "search q=\"${MARKER}\" -> 0 hits (REINDEX ran but vectors not searchable -- check pgvector / RAG config)"
  finish; exit 1
fi

finish