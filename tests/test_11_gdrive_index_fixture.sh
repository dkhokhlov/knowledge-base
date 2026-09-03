#!/usr/bin/env bash
# System integration test: api-gateway /index `path` parameter on a small,
# deterministic, committed fixture set (fast `make test` replacement for the
# full real-gdrive drain in test_09).
#
# Indexes root/.tests/ (a dot-dir the generic walk skips, so it never
# contaminates any real KB) into a throwaway temp KB via
# POST /index?dir=.tests&kb_id=<temp>. The gateway uploads via
# POST /files/ (process_in_background=True) and does NOT link files itself;
# OWUI's per-upload background task is the sole linker (extract -> embed ->
# link). That drain is async, so this test polls GET /status?dir=.tests for the
# REAL drain terminal state, audits failures, and runs a deterministic semantic
# search by a fixed marker token.
#
# Self-contained: the temp KB is created with the admin key, granted '*' read so
# the agent (user) key can search it, and deleted on EXIT (its files too). The
# committed fixture files under root/.tests/ are NOT deleted (they are tracked
# in the repo). The fixture set is small text files (.txt/.md/.json) plus minimal
# binary files (.pdf/.docx/.pptx) so the binary extraction path is exercised too.
# The text fixtures extract without markitdown-ocr; the binary fixtures exercise
# the markitdown-ocr path. When OCR is not provisioned the binary fixtures fail
# extraction and surface as a genuine-failure notice (not a hard fail) — the text
# fixtures still complete and the marker search still carries the test.
#
# Tolerant: SKIPs (passes with a notice) when root/.tests has no allowlisted
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
# Patch 11 lexical-dsl sentinel. MUST equal docker/gateway/app.py
# LEXICAL_DSL_PREFIX + apply_lexical_dsl.py SENTINEL. The direct-OWUI contract
# below prefixes the query with this manually (simulating the gateway's
# mode=lexical-dsl dispatch) to verify OWUI recognizes + strips it.
SENTINEL="KB_LEXICAL_DSL_V1::"
# Coined single-word DSL probe tokens (pdb.simple splits on _/-, so these are
# single tokens). Unique across root/.tests/ (verified: no collisions).
DSL_PHRASE='zenith rotating zephyr'        # exact phrase in dsl-phrase.md
DSL_AND_A='dslwordalpha'                   # in dsl-and-a.md AND dsl-and-b.md
DSL_AND_B='dslwordbeta'                    # in dsl-and-a.md only

# --- skip condition ----------------------------------------------------------
# Exclude .meta/.meta.json sidecars: the gateway's _entry_for skips them by
# name (app.py), so they are never indexed. src_count must match what the drain
# can account, not what the allowlist regex alone matches (.meta.json ends in
# .json, so the regex alone would over-count sidecars the gateway drops).
src_count=$(find root/.tests -type f -regextype posix-extended -iregex ".*${ALLOW_RE}" \
  ! -name '*.meta' ! -name '*.meta.json' 2>/dev/null | wc -l)
if [ "${src_count:-0}" -eq 0 ]; then
  section "gdrive index (fixture)"
  pass "SKIP: root/.tests has no allowlisted fixture files (committed fixtures missing)"
  finish
  exit 0
fi

require_env OPENWEBUI_ADMIN_API_KEY KB_API_KEY || { finish; exit 1; }
AK="$OPENWEBUI_ADMIN_API_KEY"
UK="$KB_API_KEY"
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

# --- POST /index (admin): reconcile root/.tests into the temp KB (dir=.tests) -
section "POST /index (api-gateway, dir=.tests)"
idx_resp=$(curl -sS --max-time 1200 -X POST \
  "$O/index?dir=.tests&kb_id=${KB_ID}" \
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
section "poll GET /status (real drain, dir=.tests)"
wait_s="${GDRIVE_FIXTURE_WAIT:-180}"
deadline=$(( $(date +%s) + wait_s ))
completed=0; pending=0; processing=0; failed=0; status_json=""
while :; do
  status_json=$(curl -sS "$O/status?dir=.tests&kb_id=${KB_ID}&json=1" "${ADM[@]}" 2>/dev/null || true)
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
  fail "check: docker logs ${OWUI_CONTAINER:-kb-openwebui}"
  finish
  exit 1
fi

# --- payload invariants: each list length == its count; lists sum == counts sum
section "status payload invariants (lists vs counts)"
inv_out=$(printf '%s' "$status_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
idx = d.get("indexed_files") or []
pend = d.get("pending_files") or []
fl = d.get("failed_files") or []
ic = d.get("indexed_count", 0); p = d.get("pending", 0)
pc = d.get("processing", 0); fc = d.get("failed", 0)
errs = []
if len(idx) != ic: errs.append("len(indexed_files)=%d != indexed_count=%d" % (len(idx), ic))
if len(pend) != p: errs.append("len(pending_files)=%d != pending=%d" % (len(pend), p))
if len(fl) != fc: errs.append("len(failed_files)=%d != failed=%d" % (len(fl), fc))
if len(idx) + len(pend) + len(fl) + pc != ic + p + fc + pc:
    errs.append("list sum + processing=%d != count sum=%d" % (len(idx)+len(pend)+len(fl)+pc, ic+p+fc+pc))
print("OK" if not errs else "FAIL")
print("%d %d %d" % (len(idx), len(pend), len(fl)))
print("\n".join(errs))
' 2>/dev/null || echo "FAIL"$'\n''0 0 0'$'\n''parse error')
inv_verdict=$(printf '%s' "$inv_out" | sed -n 1p)
read -r inv_idx inv_pend inv_fail < <(printf '%s' "$inv_out" | sed -n 2p)
inv_errs=$(printf '%s' "$inv_out" | sed -n '3,$p')
if [ "$inv_verdict" = "OK" ]; then
  pass "indexed_files=${inv_idx} pending_files=${inv_pend} failed_files=${inv_fail} == indexed_count=${completed} pending=${pending} failed=${failed}"
else
  fail "payload invariants violated:"
  printf '%s\n' "$inv_errs"
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

# --- patch 10 BM25 arm (ParadeDB pg_search ||| + pdb.score) ------------------
# Direct psql probes of the patch-10 FTS arm, scoped to the temp fixture KB
# collection (collection_name = KB_ID). The ||| operator tokenizes its RHS
# (multi-term OR, colon/dash-safe); pdb.score is real BM25. These prove the
# arm works on indexed content (the kb-bm25-check gate covers the same probes
# live; this is the deterministic iso verification).
section "BM25 arm (pg_search ||| + pdb.score)"
PG_CTN="${POSTGRES_CONTAINER:?POSTGRES_CONTAINER not set (iso fixture must provide it)}"
bm25_count() {  # print "<count|ERR> <rc>" for a ||| count scoped to KB_ID
  local cnt rc
  # NOTE: the SQL goes via stdin heredoc, not -c. psql does NOT interpolate
  # :'var' in a -c argument (documented: no variable substitution in -c); it
  # DOES on stdin. A bare -c "SELECT ... :'q'" sends the literal : to the
  # server -> syntax error. -i keeps stdin open for the heredoc.
  cnt=$(docker exec -i "$PG_CTN" psql -U "${PGVECTOR_USER:?}" -d "${PGVECTOR_DB:?}" \
    -v ON_ERROR_STOP=1 -tA -v kb_id="$KB_ID" -v q="$1" 2>/dev/null <<'SQL'
SELECT count(*) FROM document_chunk WHERE collection_name = :'kb_id' AND text ||| :'q'
SQL
)
  rc=$?
  printf '%s %s' "${cnt:-ERR}" "$rc"
}
# Patch 11: DSL count via paradedb.parse_with_field (lenient => false). Same
# heredoc + :'q' binding as bm25_count. ON_ERROR_STOP=1 makes psql exit non-zero
# (rc=3) on a parse error -> the malformed-raises assertion checks rc!=0. A
# fresh psql connection per call (no persistent txn), so a raising query does
# not poison later probes (unlike kb_check's shared stores._pg connection).
bm25_dsl_count() {  # print "<count|ERR> <rc>" for a parse_with_field count scoped to KB_ID
  local cnt rc
  cnt=$(docker exec -i "$PG_CTN" psql -U "${PGVECTOR_USER:?}" -d "${PGVECTOR_DB:?}" \
    -v ON_ERROR_STOP=1 -tA -v kb_id="$KB_ID" -v q="$1" 2>/dev/null <<'SQL'
SELECT count(*) FROM document_chunk WHERE collection_name = :'kb_id' AND id @@@ paradedb.parse_with_field('text', :'q', lenient => false)
SQL
)
  rc=$?
  printf '%s %s' "${cnt:-ERR}" "$rc"
}
bm25_dsl_count_text() {  # args: dsl_query, token, rel(1=ILIKE contains/0=NOT ILIKE lacks)
  # -> "<count|ERR> <rc>". Operator semantics discriminator: counts DSL-matched
  # chunks whose raw text contains (rel=1) or lacks (rel=0) a token. Lets the
  # AND/composite-NOT assertions catch a parser that ignores a +/- operand: a
  # correct AND (+a +b) never matches a beta-less chunk; a correct composite-NOT
  # (+a -b) never matches a beta chunk. The unquoted heredoc expands ${op} only
  # (no $ in the rest of the SQL); :'tok' / :'q' are psql vars.
  local cnt rc rel="$3" op
  if [ "$rel" = "0" ]; then op="NOT ILIKE"; else op="ILIKE"; fi
  cnt=$(docker exec -i "$PG_CTN" psql -U "${PGVECTOR_USER:?}" -d "${PGVECTOR_DB:?}" \
    -v ON_ERROR_STOP=1 -tA -v kb_id="$KB_ID" -v q="$1" -v tok="$2" 2>/dev/null <<SQL
SELECT count(*) FROM document_chunk WHERE collection_name = :'kb_id' AND id @@@ paradedb.parse_with_field('text', :'q', lenient => false) AND text ${op} '%' || :'tok' || '%'
SQL
)
  rc=$?
  printf '%s %s' "${cnt:-ERR}" "$rc"
}
# extension + index exist
ext=$(docker exec "$PG_CTN" psql -U "${PGVECTOR_USER:?}" -d "${PGVECTOR_DB:?}" -tA -c \
  "SELECT count(*) FROM pg_extension WHERE extname='pg_search'" 2>/dev/null)
if [ "${ext:-0}" = "1" ]; then pass "pg_search extension installed"; else
  fail "pg_search extension missing (count=${ext:-0}) — run make kb-bm25-init"; finish; exit 1; fi
idx=$(docker exec "$PG_CTN" psql -U "${PGVECTOR_USER:?}" -d "${PGVECTOR_DB:?}" -tA -c \
  "SELECT count(*) FROM pg_indexes WHERE schemaname='public' AND indexname='idx_document_chunk_bm25'" 2>/dev/null)
if [ "${idx:-0}" = "1" ]; then pass "idx_document_chunk_bm25 present"; else
  fail "idx_document_chunk_bm25 missing — run make kb-bm25-init"; finish; exit 1; fi
# multi-term ||| returns >0 (the marker is in every fixture file -> tokenized OR matches)
read -r mt_cnt mt_rc < <(bm25_count "$MARKER")
if [ "${mt_rc:-1}" = "0" ] && [ "${mt_cnt:-0}" -gt 0 ] 2>/dev/null; then
  pass "||| multi-term '${MARKER}' -> ${mt_cnt} hit(s)"
else
  fail "||| multi-term '${MARKER}' -> ${mt_cnt} (rc=${mt_rc}) — expected >0 on a completed fixture KB"
  finish; exit 1
fi
# pdb.score on the ranking path (the production ORDER BY); max score must be > 0
score=$(docker exec -i "$PG_CTN" psql -U "${PGVECTOR_USER:?}" -d "${PGVECTOR_DB:?}" -tA \
  -v kb_id="$KB_ID" -v q="$MARKER" 2>/dev/null <<'SQL'
SELECT max(s) FROM (SELECT pdb.score(id) AS s FROM document_chunk WHERE collection_name = :'kb_id' AND text ||| :'q' ORDER BY pdb.score(id) DESC LIMIT 5) t
SQL
)
if [ -n "${score:-}" ] && [ "${score:-0}" != "0" ] 2>/dev/null; then
  pass "pdb.score ranking path -> max score ${score}"
else
  fail "pdb.score ranking path returned 0/empty ('${score:-}') — the BM25 index is not scoring"
  finish; exit 1
fi
# colon/dash-safe: a query with a colon must NOT error (the C1 silent-zero class for |||)
read -r col_cnt col_rc < <(bm25_count "error: ${MARKER}")
if [ "${col_rc:-1}" = "0" ]; then
  pass "||| colon-safe 'error: ${MARKER}' -> ${col_cnt} hit(s) (no error)"
else
  fail "||| colon-safe query errored (rc=${col_rc}) — the C1 silent-zero class"
  finish; exit 1
fi
# zero-token: ??? -> 0 rows, no error
read -r z_cnt z_rc < <(bm25_count '???')
if [ "${z_rc:-1}" = "0" ] && [ "${z_cnt:-0}" = "0" ] 2>/dev/null; then
  pass "||| zero-token '???' -> 0 hit(s) (no error)"
else
  fail "||| zero-token '???' -> ${z_cnt} (rc=${z_rc}) — expected 0 rows, no error"
  finish; exit 1
fi

# --- patch 11 lexical-dsl SQL probes (paradedb.parse_with_field, lenient => false)
# Direct psql probes of the patch-11 DSL predicate on the temp fixture KB. The
# 4 dsl/*.md fixtures (single-word coined tokens; pdb.simple splits on _/-) give
# ground truth the kb_check smoke gate cannot (kb_check has no semantic ground
# truth). ON_ERROR_STOP=1 + a fresh psql per call -> a malformed query exits
# rc!=0 (the lenient => false raise), no txn cascade.
section "lexical-dsl SQL probes (parse_with_field, lenient => false)"
# phrase: "zenith rotating zephyr" -> >0 (only dsl-phrase.md has the phrase)
read -r ph_cnt ph_rc < <(bm25_dsl_count "\"$DSL_PHRASE\"")
if [ "${ph_rc:-1}" = "0" ] && [ "${ph_cnt:-0}" -gt 0 ] 2>/dev/null; then
  pass "dsl phrase \"${DSL_PHRASE}\" -> ${ph_cnt} hit(s)"
else
  fail "dsl phrase \"${DSL_PHRASE}\" -> ${ph_cnt} (rc=${ph_rc}) -- expected >0 (dsl-phrase.md)"
  finish; exit 1
fi
# phrase negative: "zenith rotating nowhere" -> 0, rc=0 (valid syntax, no match)
read -r pn_cnt pn_rc < <(bm25_dsl_count "\"zenith rotating nowhere\"")
if [ "${pn_rc:-1}" = "0" ] && [ "${pn_cnt:-0}" = "0" ] 2>/dev/null; then
  pass "dsl phrase-negative \"zenith rotating nowhere\" -> 0 hit(s) (valid, no match)"
else
  fail "dsl phrase-negative -> ${pn_cnt} (rc=${pn_rc}) -- expected 0 rows, rc=0"
  finish; exit 1
fi
# AND: +dslwordalpha +dslwordbeta -> >0 (only dsl-and-a.md has both)
read -r and_cnt and_rc < <(bm25_dsl_count "+${DSL_AND_A} +${DSL_AND_B}")
if [ "${and_rc:-1}" = "0" ] && [ "${and_cnt:-0}" -gt 0 ] 2>/dev/null; then
  pass "dsl AND +${DSL_AND_A} +${DSL_AND_B} -> ${and_cnt} hit(s)"
else
  fail "dsl AND +${DSL_AND_A} +${DSL_AND_B} -> ${and_cnt} (rc=${and_rc}) -- expected >0 (dsl-and-a.md)"
  finish; exit 1
fi
# AND discriminator: no matched chunk may LACK beta. A parser that ignores
# +dslwordbeta (ORs instead of ANDs) matches dsl-and-b.md too (alpha, lacks beta)
# -> violation count >0. Fixture ground truth: only dsl-and-a.md has both.
read -r andv_cnt andv_rc < <(bm25_dsl_count_text "+${DSL_AND_A} +${DSL_AND_B}" "$DSL_AND_B" 0)
if [ "${andv_rc:-1}" = "0" ] && [ "${andv_cnt:-0}" = "0" ] 2>/dev/null; then
  pass "dsl AND excludes beta-less chunks (${andv_cnt} violation(s))"
else
  fail "dsl AND +${DSL_AND_A} +${DSL_AND_B} -> ${andv_cnt} beta-less chunk(s) (rc=${andv_rc}) -- +beta ignored (OR not AND)"
  finish; exit 1
fi
# composite-NOT: +dslwordalpha -dslwordbeta -> >0 (only dsl-and-b.md: has alpha, lacks beta)
read -r not_cnt not_rc < <(bm25_dsl_count "+${DSL_AND_A} -${DSL_AND_B}")
if [ "${not_rc:-1}" = "0" ] && [ "${not_cnt:-0}" -gt 0 ] 2>/dev/null; then
  pass "dsl composite-NOT +${DSL_AND_A} -${DSL_AND_B} -> ${not_cnt} hit(s)"
else
  fail "dsl composite-NOT +${DSL_AND_A} -${DSL_AND_B} -> ${not_cnt} (rc=${not_rc}) -- expected >0 (dsl-and-b.md)"
  finish; exit 1
fi
# composite-NOT discriminator: no matched chunk may CONTAIN beta. A parser that
# ignores -dslwordbeta matches dsl-and-a.md too (has both) -> violation count >0.
# Fixture ground truth: only dsl-and-b.md has alpha without beta.
read -r notv_cnt notv_rc < <(bm25_dsl_count_text "+${DSL_AND_A} -${DSL_AND_B}" "$DSL_AND_B" 1)
if [ "${notv_rc:-1}" = "0" ] && [ "${notv_cnt:-0}" = "0" ] 2>/dev/null; then
  pass "dsl composite-NOT excludes beta chunks (${notv_cnt} violation(s))"
else
  fail "dsl composite-NOT +${DSL_AND_A} -${DSL_AND_B} -> ${notv_cnt} beta chunk(s) (rc=${notv_rc}) -- -beta ignored (NOT broken)"
  finish; exit 1
fi
# malformed: "unmatched phrase -> rc!=0 (lenient => false RAISES -- the C1 property)
read -r bad_cnt bad_rc < <(bm25_dsl_count '"unmatched phrase')
if [ "${bad_rc:-1}" != "0" ]; then
  pass "dsl malformed (unmatched quote) -> rc=${bad_rc} (lenient => false raised)"
else
  fail "dsl malformed (unmatched quote) -> rc=0 (lenient => false did NOT raise -- the C1 silent-zero regression)"
  finish; exit 1
fi

# --- patch 11 lexical-dsl direct-OWUI contract (sentinel + C1 re-raise) ------
# Send a sentinel-prefixed query through the gateway passthrough to OWUI's
# /api/v1/retrieval/query/collection (hybrid_bm25_weight=1.0 skips the vector
# arm; the FTS arm recognizes the sentinel + runs parse_with_field). Verifies
# OWUI recognizes + strips the sentinel (the cross-image contract) and that a
# malformed DSL re-raises through the 3 except gates -> HTTPException 400 (the
# C1 fix), not a silent 200 + 0 / full-collection fallback.
section "lexical-dsl direct-OWUI contract (sentinel + C1 re-raise)"
# valid phrase: sentinel + "zenith rotating zephyr" -> >=1 hit
dsl_q=$(python3 -c 'import sys,json;print(json.dumps(sys.argv[1]))' "${SENTINEL}\"${DSL_PHRASE}\"")
dsl_hits=$(curl -s -X POST "$O/api/v1/retrieval/query/collection" "${RD[@]}" -H 'Content-Type: application/json' \
  -d "{\"collection_names\":[\"${KB_ID}\"],\"query\":${dsl_q},\"k\":4,\"hybrid\":true,\"hybrid_bm25_weight\":1.0}" \
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
if [ "${dsl_hits:-0}" -gt 0 ]; then
  pass "lexical-dsl valid phrase -> ${dsl_hits} hit(s) (sentinel recognized + stripped)"
else
  fail "lexical-dsl valid phrase -> 0 hits (sentinel not recognized, or DSL predicate broken)"
  finish; exit 1
fi
# malformed: sentinel + "unmatched -> HTTP 400 (the C1 re-raise, not silent 200)
bad_q=$(python3 -c 'import sys,json;print(json.dumps(sys.argv[1]))' "${SENTINEL}\"unmatched")
http_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$O/api/v1/retrieval/query/collection" "${RD[@]}" -H 'Content-Type: application/json' \
  -d "{\"collection_names\":[\"${KB_ID}\"],\"query\":${bad_q},\"k\":4,\"hybrid\":true,\"hybrid_bm25_weight\":1.0}")
if [ "${http_code}" = "400" ]; then
  pass "lexical-dsl malformed -> HTTP 400 (C1 re-raise propagates, not silent 200)"
else
  fail "lexical-dsl malformed -> HTTP ${http_code} (expected 400; C1 re-raise broken -- error swallowed)"
  finish; exit 1
fi

finish