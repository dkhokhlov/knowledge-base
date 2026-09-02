#!/usr/bin/env bash
# Comprehensive at-scale e2e body (iso long): run via the iso_env_named("gdrive",
# at_scale=True) fixture, which owns isolate + e2e_provision_at_scale (clean-all
# + image rebuild + preflight + real rclone gdrive corpus + make ci) + teardown.
# This body runs in the clone: first the in-clone suite (unit + live-RO 01/02/03
# against the fresh stack), then the full real-gdrive drain below.
#
# gdrive indexing via api-gateway (stateless, no sidecar). POSTs /index (admin
# key) to reconcile ./gdrive into the OWUI "gdrive" KB.
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
# Deterministic semantic search: prove the drained vectors are queryable. Two
# parts, each fixing a distinct failure mode:
#   1. REINDEX ivfflat after the drain (the step above). ivfflat does not fold
#      post-build inserts into its lists until REINDEX, so a fresh bulk drain's
#      vectors are invisible to vector search. OWUI hybrid = vector-fetch ->
#      BM25 rerank (no FTS fallback), so vector=0 -> hybrid=0. This was the real
#      cause of a prior 0-hits (NOT query formulation): /status showed completed
#      but the index was not refreshed.
#   2. Mine a 5-word content PHRASE (not the filename stem) to query the KB
#      collection for >=1 hit. The stem is a separate, latent fragility: it is
#      non-deterministic (first-completed file) and often ubiquitous corpus
#      terms -> hybrid ~0 IDF -> 0 hits on a HEALTHY, refreshed index. A content
#      phrase is rare across any corpus -> retrieves reliably. Corpus-
#      independent: mined at runtime, no hardcoded terms.
#
# /index is idempotent (re-run reports unmodified + re-triggers any failed).
#
# No SKIP: the at-scale fixture always provisions the gdrive KB + syncs the
# corpus (or provision fails first + this body never runs). A missing gdrive KB
# (cannot resolve by name) or empty corpus is a hard fail (resolve / completed<source),
# not a silent pass.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up

# --- in-clone suite: unit + live-RO (01/02/03) against THIS clone's fresh stack
# Direct pytest -- NOT `make test` (which targets the LIVE stack for RO) -- with
# marker "not iso and not long". Runs the unit tests + the integration tests
# (test_01/02/03, live-RO against the clone stack) and EXCLUDES every iso/long
# test (test_09 itself is iso long -> excluded -> no recursion; test_08/test_12/
# shared excluded too). The .venv was provisioned by `make ci` at the tail of
# e2e_provision_at_scale. Fail on any non-zero exit.
section "in-clone suite (unit + live-RO)"
if ! .venv/bin/python -m pytest -m "not iso and not long" -v; then
  fail "in-clone suite failed (see output above)"
  finish
  exit 1
fi
pass "in-clone suite green"

# --- full real-gdrive drain --------------------------------------------------
O="$(kb_host)"
# Allowlist must match gateway DEFAULT_ALLOW (gateway/app.py). find's default
# Emacs regex treats (a|b) as LITERAL (matches 0 files), so every -iregex call
# below MUST use -regextype posix-extended for the alternation to work.
ALLOW_RE='[.](docx|pdf|pptx|xlsx|txt|md|html|json|log|tex)$'
# Exclude dot-dirs (.tests, .sync-reports) from the source count: gateway.walk_source prunes them from a full walk (gateway/app.py "Prune dot-dirs"), so counting them leaves the drain `accounted < src_count` -> a false timeout. `path` opts into a dot-subtree (test_11); this full walk does not.
src_count=$(find root/gdrive -type f -not -path '*/.*' -regextype posix-extended -iregex ".*${ALLOW_RE}" 2>/dev/null | wc -l)

require_env OPENWEBUI_ADMIN_API_KEY KB_API_KEY || { finish; exit 1; }
# Resolve the "gdrive" KB by name (the gateway is stateless; the e2e provision
# bootstrapped it via `make kb-bootstrap KB=gdrive`). Fail fast if it is missing.
GDRIVE_KB_ID=$(KB=gdrive ./scripts/kb-bootstrap.sh --resolve 2>/dev/null) \
  || { fail "could not resolve the 'gdrive' KB by name (run: make kb-bootstrap KB=gdrive)"; finish; exit 1; }
AK="$OPENWEBUI_ADMIN_API_KEY"
UK="$KB_API_KEY"
ADM=(-H "Authorization: Bearer $AK")
RD=(-H "Authorization: Bearer $UK")

# --- POST /index (admin): reconcile ./root/gdrive into the KB (dir=gdrive) ----
section "POST /index (api-gateway)"
idx_resp=$(curl -sS --max-time 1200 -X POST \
  "$O/index?kb_id=${GDRIVE_KB_ID}&dir=gdrive" \
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
# file.data.status via GET /files/. Poll up to GDRIVE_TEST_WAIT (default 2400s:
# cold first-extraction per-figure OCR budget over the full fresh-synced corpus).
# Admin key: /status uses the gateway's held admin key for the file scan
# internally; any valid key authorizes the call.
section "poll GET /status (real drain)"
wait_s="${GDRIVE_TEST_WAIT:-2400}"
deadline=$(( $(date +%s) + wait_s ))
completed=0; pending=0; processing=0; failed=0; status_json=""
while :; do
  status_json=$(curl -sS "$O/status?kb_id=${GDRIVE_KB_ID}&dir=gdrive&json=1" "${ADM[@]}" 2>/dev/null || true)
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
  fail "check: docker logs ${OWUI_CONTAINER:-kb-openwebui} + docker logs ${MARKITDOWN_CONTAINER:-kb-markitdown-ocr}"
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
  fail "re-run: make gdrive-sync; then docker logs ${OWUI_CONTAINER:-kb-openwebui} / ${MARKITDOWN_CONTAINER:-kb-markitdown-ocr}"
  finish
  exit 1
fi

# --- REINDEX after the bulk drain (refresh ivfflat vector) -------------------
# The gdrive drain bulk-inserted rows AFTER the ivfflat index was built.
#   - ivfflat (idx_document_chunk_vector): builds its inverted lists at CREATE
#     INDEX time and folds post-build rows in only on REINDEX, so the freshly
#     drained vectors are invisible to vector search until now. OWUI hybrid is
#     vector-fetch -> BM25 rerank (no independent FTS fallback), so vector=0 ->
#     hybrid=0. This was the real cause of a prior 0-hits: /status showed
#     completed (vectors written) but the index was not refreshed.
# The GIN FTS index (idx_document_chunk_text_search) was DROPPED by patch 10
# (the old plainto_tsquery arm it served is gone; the BM25 arm uses
# idx_document_chunk_bm25, which is incremental -- no REINDEX needed after a
# drain). ivfflat is the only index that needs this. Mirrors the manual REINDEX
# done on the live stack (Phase-1). Run after /status is terminal (all vectors
# written), before the semantic search. Log the duration (capacity-planning data
# for the at-scale corpus). POSTGRES_CONTAINER is iso-fixture-provided; :? (not
# :- kb-postgres) so a missing var fails loud instead of touching the LIVE stack.
section "REINDEX after bulk drain (ivfflat vector)"
PG_CTN="${POSTGRES_CONTAINER:?POSTGRES_CONTAINER not set (iso fixture must provide it)}"
reindex_one() {  # echo "<seconds> <rc>" for REINDEX INDEX $1
  local idx="$1" t0 t1 rc
  t0=$(date +%s)
  docker exec "$PG_CTN" psql -U "${PGVECTOR_USER:?}" -d "${PGVECTOR_DB:?}" \
    -v ON_ERROR_STOP=1 -c "REINDEX INDEX $idx" >/dev/null 2>&1
  rc=$?
  t1=$(date +%s)
  echo "$((t1 - t0)) $rc"
}
read -r vec_s vec_rc < <(reindex_one idx_document_chunk_vector)
if [ "${vec_rc:-1}" -ne 0 ]; then
  fail "REINDEX idx_document_chunk_vector failed (rc=${vec_rc}, postgres=${PG_CTN})"
  finish
  exit 1
fi
pass "REINDEX done: ivfflat vector ${vec_s}s (drained vectors now queryable)"

# --- patch 10 BM25 arm: extension + index exist (deterministic) --------------
section "BM25 arm (pg_search extension + index)"
ext=$(docker exec "$PG_CTN" psql -U "${PGVECTOR_USER:?}" -d "${PGVECTOR_DB:?}" -tA -c \
  "SELECT count(*) FROM pg_extension WHERE extname='pg_search'" 2>/dev/null)
if [ "${ext:-0}" = "1" ]; then pass "pg_search extension installed"; else
  fail "pg_search extension missing (count=${ext:-0}) — run make kb-bm25-init"; finish; exit 1; fi
idx=$(docker exec "$PG_CTN" psql -U "${PGVECTOR_USER:?}" -d "${PGVECTOR_DB:?}" -tA -c \
  "SELECT count(*) FROM pg_indexes WHERE schemaname='public' AND indexname='idx_document_chunk_bm25'" 2>/dev/null)
if [ "${idx:-0}" = "1" ]; then pass "idx_document_chunk_bm25 present"; else
  fail "idx_document_chunk_bm25 missing — run make kb-bm25-init"; finish; exit 1; fi

# --- deterministic semantic search (vectors written + searchable) ------------
# The REINDEX above made the drained vectors queryable (the real 0-hits fix).
# This step mines a multi-word PHRASE from a COMPLETED file's CONTENT (not its
# filename stem) and requires >=1 hit -- a query-robustness measure, not the
# index fix. The filename stem is a latent fragility: non-deterministic (first-
# completed file) + often ubiquitous corpus terms (e.g. a generic title stem like
# "Introduction Overview") -> hybrid ~0 IDF -> 0 hits on a HEALTHY, refreshed
# index (hybrid-retrieve-query-form-sensitive). A 5-word sub-sentence phrase is
# rare across ANY corpus by combinatorics, + within one chunk (the offset-aware
# chunker respects sentence boundaries, and a phrase from the first sentences
# sits at the content start = chunk 0 -> no boundary straddle). Corpus-
# independent: no domain-specific token shape, no hardcoded terms. Iterate
# completed files until one yields a phrase (bounded; usually file 1).
#
# /status indexed_files[] is gdrive-KB-scoped but carries no file id (only
# filename/status/size/error). /api/v1/files/ items carry the id + data.status
# but are paginated 50/page. Iterate the files list (page 1 holds 50 completed
# files -> enough to find one with a minable phrase), take each COMPLETED file's
# id, fetch its content, and mine a phrase until one yields. In this at-scale
# clone the only files are the gdrive KB's (the in-clone suite excludes every iso
# upload test), so a completed file here is a gdrive-KB file with vectors in the
# gdrive collection; the query targets collection_names=[GDRIVE_KB_ID] directly.
section "semantic search over gdrive KB (deterministic)"
q_phrase=""
src_fid=""
page_json="$(curl -sS "$O/api/v1/files/?content=false&page=1" "${ADM[@]}" 2>/dev/null || true)"
for fid in $(printf '%s' "$page_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
for it in (d.get("items") or []):
    if (it.get("data") or {}).get("status") == "completed" and it.get("id"):
        print(it["id"])
' 2>/dev/null); do
  [ -n "$fid" ] || continue
  tok=$(curl -s "$O/api/v1/files/${fid}/data/content" "${ADM[@]}" 2>/dev/null \
    | python3 -c '
import sys, json, re
try:
    txt = (json.load(sys.stdin) or {}).get("content") or ""
except Exception:
    txt = ""
if not txt:
    print(""); sys.exit(0)
stop = {"the","a","an","in","on","of","and","to","is","are","for","with","or","as",
        "at","by","be","this","that","it","was","were","from","its","their","which",
        "not","but","has","have","had","will","can","may","we","you","they","he","she"}
# A 5-word sub-sentence phrase (>= 3 substantive words: non-stop, len>=4): rare
# across any corpus + within one chunk (sentence-bounded, near the content start).
for sent in re.split(r"(?<=[.!?])\s+", txt):
    ws = re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]*", sent)
    if len(ws) < 6:
        continue
    for start in range(0, len(ws) - 4):
        win = ws[start:start + 5]
        if sum(1 for w in win if w.lower() not in stop and len(w) >= 4) >= 3:
            print(" ".join(win)); sys.exit(0)
print("")
' 2>/dev/null || true)
  if [ -n "$tok" ]; then q_phrase="$tok"; src_fid="$fid"; break; fi
done
if [ -z "$q_phrase" ]; then
  pass "SKIP semantic search: no completed file (page 1) has a usable content phrase"
else
  hits=$(curl -s -X POST "$O/api/v1/retrieval/query/collection" "${RD[@]}" -H 'Content-Type: application/json' \
    -d "{\"collection_names\":[\"${GDRIVE_KB_ID}\"],\"query\":$(python3 -c 'import sys,json;print(json.dumps(sys.argv[1]))' "$q_phrase"),\"k\":4,\"hybrid\":true}" \
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
    pass "search q=\"$q_phrase\" (file ${src_fid}) -> ${hits} hit(s) (vectors searchable)"
  else
    fail "search q=\"$q_phrase\" (file ${src_fid}) -> 0 hits (completed file has vectors but its content phrase retrieved none — check embedding/RAG config)"
    finish
    exit 1
  fi
fi

# --- patch 10 BM25 arm: ||| + pdb.score probes + sort-order-per-mode ---------
# These need a corpus-guaranteed query (the mined q_phrase). SKIPped above when
# no phrase was mined -- gate the whole block on q_phrase so it SKIPs the same
# way (extension+index existence already asserted above; this is the ranking +
# sort-order proof). M9: q_phrase is corpus-mined at runtime, NOT a real Drive
# filename/id -- no PII. The sort-order comparison grounds the Step 4 SKILL
# guidance: lexical = pure BM25/IDF (rare tokens rank high), hybrid = RRF.
if [ -n "$q_phrase" ]; then
  section "BM25 arm (pg_search ||| + pdb.score + sort-order-per-mode)"
  # A single rare token (first substantive word of q_phrase) for the direct-psql
  # probes -- guaranteed in the corpus (mined from it).
  q_token=$(printf '%s' "$q_phrase" | python3 -c '
import sys, re
stop = {"the","a","an","in","on","of","and","to","is","are","for","with","or","as",
        "at","by","be","this","that","it","was","were","from","its","their","which",
        "not","but","has","have","had","will","can","may","we","you","they","he","she"}
ws = re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]*", sys.stdin.read())
for w in ws:
    if w.lower() not in stop and len(w) >= 4:
        print(w); break
' 2>/dev/null)
  bm25_count() {  # print "<count|ERR> <rc>" for a ||| count scoped to GDRIVE_KB_ID
    local cnt rc
    # SQL via stdin heredoc (NOT -c): psql does not interpolate :'var' in a -c
    # argument; it does on stdin. -i keeps stdin open for the heredoc.
    cnt=$(docker exec -i "$PG_CTN" psql -U "${PGVECTOR_USER:?}" -d "${PGVECTOR_DB:?}" \
      -v ON_ERROR_STOP=1 -tA -v kb_id="$GDRIVE_KB_ID" -v q="$1" 2>/dev/null <<'SQL'
SELECT count(*) FROM document_chunk WHERE collection_name = :'kb_id' AND text ||| :'q'
SQL
)
    rc=$?
    printf '%s %s' "${cnt:-ERR}" "$rc"
  }
  # (c) ||| multi-term returns >0 (the mined phrase tokens are in the corpus)
  read -r mt_cnt mt_rc < <(bm25_count "$q_phrase")
  if [ "${mt_rc:-1}" = "0" ] && [ "${mt_cnt:-0}" -gt 0 ] 2>/dev/null; then
    pass "||| multi-term (corpus phrase) -> ${mt_cnt} hit(s)"
  else
    fail "||| multi-term (corpus phrase) -> ${mt_cnt} (rc=${mt_rc}) -- expected >0 (phrase is corpus-mined)"
    finish; exit 1
  fi
  # (c cont) pdb.score on the ranking path (the production ORDER BY); max > 0
  score=$(docker exec -i "$PG_CTN" psql -U "${PGVECTOR_USER:?}" -d "${PGVECTOR_DB:?}" -tA \
    -v kb_id="$GDRIVE_KB_ID" -v q="$q_phrase" 2>/dev/null <<'SQL'
SELECT max(s) FROM (SELECT pdb.score(id) AS s FROM document_chunk WHERE collection_name = :'kb_id' AND text ||| :'q' ORDER BY pdb.score(id) DESC LIMIT 5) t
SQL
)
  if [ -n "${score:-}" ] && [ "${score:-0}" != "0" ] 2>/dev/null; then
    pass "pdb.score ranking path -> max score ${score}"
  else
    fail "pdb.score ranking path returned 0/empty ('${score:-}') -- the BM25 index is not scoring"
    finish; exit 1
  fi
  # (d) colon-safe: a query with a colon must NOT error (the C1 silent-zero class)
  read -r col_cnt col_rc < <(bm25_count "error: ${q_token}")
  if [ "${col_rc:-1}" = "0" ]; then
    pass "||| colon-safe 'error: <token>' -> ${col_cnt} hit(s) (no error)"
  else
    fail "||| colon-safe query errored (rc=${col_rc}) -- the C1 silent-zero class"
    finish; exit 1
  fi
  # (d cont) zero-token: ??? -> 0 rows, no error
  read -r z_cnt z_rc < <(bm25_count '???')
  if [ "${z_rc:-1}" = "0" ] && [ "${z_cnt:-0}" = "0" ] 2>/dev/null; then
    pass "||| zero-token '???' -> 0 hit(s) (no error)"
  else
    fail "||| zero-token '???' -> ${z_cnt} (rc=${z_rc}) -- expected 0 rows, no error"
    finish; exit 1
  fi
  # (e/f/g) sort-order-per-mode via gateway /retrieve (same query, 3 modes) ----
  # lexical (bm25_weight=1.0, vector arm skipped) = pure BM25 (pdb.score DESC,
  # IDF-driven); hybrid (0.5) = RRF fusion (BM25 score value discarded, ordinal
  # rank only); vector = cosine. Compare the ordered hit-text lists. Grounds the
  # SKILL guidance: lexical = BM25/IDF precision, hybrid = RRF conceptual recall.
  retrieve_order() {  # print the ordered hit texts (first 120 chars each), newline-joined
    local mode="$1" q="$2" bf
    bf="$(mktemp)"
    curl -s -o "$bf" -w '' -X POST "$O/retrieve" "${RD[@]}" -H 'Content-Type: application/json' \
      -d "{\"kb_id\":\"${GDRIVE_KB_ID}\",\"query\":$(python3 -c 'import sys,json;print(json.dumps(sys.argv[1]))' "$q"),\"k\":10,\"mode\":\"${mode}\"}" 2>/dev/null
    python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
out = []
for h in (d.get("hits") or []):
    t = ((h or {}).get("text") or "").replace("\n", " ")[:120]
    out.append(t)
print("\n".join(out))
' < "$bf" 2>/dev/null
    rm -f "$bf"
  }
  lex=$(retrieve_order lexical "$q_phrase")
  hyb=$(retrieve_order hybrid  "$q_phrase")
  vec=$(retrieve_order vector  "$q_phrase")
  # (e) lexical rank-0 contains >=2 of the 5 rare phrase words (BM25/IDF drives
  # the rare-token chunk to #1). Robust to truncation + which chunk is the source.
  lex0_ok=$(printf '%s\t%s' "$lex" "$q_phrase" | python3 -c '
import sys
line = sys.stdin.read()
lex_text, phrase = line.split("\t", 1)
rank0 = (lex_text.split("\n") or [""])[0].lower()
ws = [w.lower() for w in phrase.split() if len(w) >= 4]
n = sum(1 for w in ws if w and w in rank0)
print("OK" if n >= 2 else "FAIL")
' 2>/dev/null || echo FAIL)
  if [ "$lex0_ok" = "OK" ]; then
    pass "lexical rank-0 contains >=2 rare phrase words (BM25/IDF precision)"
  else
    fail "lexical rank-0 missing the rare phrase words (BM25 not driving the order)"
    printf '  lexical rank-0: %.120s\n' "$lex"
    finish; exit 1
  fi
  # (f) lexical order != vector order (BM25 vs cosine -- different algorithms)
  if [ -n "$lex" ] && [ "$lex" != "$vec" ]; then
    pass "lexical order != vector order (pure BM25 vs cosine)"
  else
    fail "lexical order == vector order (expected divergence: BM25 vs cosine)"
    finish; exit 1
  fi
  # (g) lexical order != hybrid order (pure BM25 vs RRF -- the score value is
  # discarded by RRF, only ordinal rank feeds fusion)
  if [ -n "$lex" ] && [ "$lex" != "$hyb" ]; then
    pass "lexical order != hybrid order (pure BM25 vs RRF)"
  else
    fail "lexical order == hybrid order (expected divergence: BM25 vs RRF)"
    finish; exit 1
  fi
fi

finish