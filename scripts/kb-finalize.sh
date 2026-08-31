#!/usr/bin/env bash
# Operator tool: finalize a gdrive drain — rebuild the pgvector ivfflat vector
# index + the GIN FTS index so the freshly embedded vectors become queryable.
#
# Named "finalize" (not "reindex") for two reasons:
#   1. On a fresh drain the vectors were NEVER queryable yet. ivfflat builds its
#      inverted lists at CREATE INDEX time and folds rows inserted after that
#      into the lists only on REINDEX, so the drained vectors sit outside the
#      lists until this step runs. This is their first publish, not a re-do.
#   2. It must not collide with the gateway's own "reindex_all"
#      (POST /index?reindex_all=1 / INDEX_ALL=1 make gdrive-sync), which
#      RE-PROCESSES files — drains the KB and re-extracts + re-embeds every
#      file (~40min cold OCR). That is a different operation at a different
#      layer. A "make kb-reindex" would read as "re-run the document indexing".
#
# What it does:
#   - REINDEX INDEX idx_document_chunk_vector (ivfflat): the load-bearing step.
#     OWUI hybrid = vector-fetch -> BM25 rerank (no independent FTS fallback),
#     so vector=0 -> hybrid=0; the un-refreshed index returns 0 hits.
#   - REINDEX INDEX idx_document_chunk_text_search (GIN on to_tsvector('simple',
#     text)): GIN is incremental (queryable without REINDEX) but is REINDEXed to
#     compact it after the bulk load + remove FTS-index state as a variable.
#   - Each REINDEX duration is logged (capacity-planning data).
#
# pgvector-only: ivfflat is the index that needs this. HNSW (pgvector) and
# Chroma (hnswlib) are incremental and do not. Guarded on VECTOR_DB=pgvector;
# on anything else it exits 0 with a message (a no-op, not an error).
#
# Two entry points (see Makefile):
#   make kb-finalize            REINDEX only (assumes the drain is terminal).
#   make gdrive-sync-finalize   gdrive-sync prereq (dispatches the async drain),
#                              then --wait: poll GET /status until the drain is
#                              terminal (pending+processing=0 AND
#                              completed+failed>=source_count), then REINDEX.
#                              One command = dispatch + wait + finalize.
#                              Times out after GDRIVE_TEST_WAIT (default 2400s) if
#                              the drain never terminates; fails loud (do not
#                              REINDEX while inserts are in flight — that races
#                              the live index).
#
# Preconditions:
#   - Stack running + the gdrive drain dispatched (make gdrive-sync) for --wait.
#   - PGVECTOR_USER / PGVECTOR_DB in .env (scaffolded by make bootstrap).
#   - OPENWEBUI_ADMIN_API_KEY + GDRIVE_KB_ID in .env.local (for --wait only).
# Container name: POSTGRES_CONTAINER (iso-fixture-injected) or kb-postgres (live).
#
# test_09_gdrive_index.sh keeps its OWN inlined REINDEX (with POSTGRES_CONTAINER
# :? — fail-loud, no live-contaminating fallback) to stay integrated with the
# test's pass/fail/finish harness; this script uses :- kb-postgres (live is the
# intended operational target). The duplication is deliberate.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck source=/dev/null
. ./.env
# shellcheck source=/dev/null
. ./.env.local 2>/dev/null || true
set +a

WAIT=0
for a in "$@"; do
  case "$a" in
    --wait) WAIT=1 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'FAIL  unknown arg: %s (use --wait)\n' "$a" >&2; exit 2 ;;
  esac
done

# pgvector guard: ivfflat REINDEX is pgvector-specific. Chroma (hnswlib) and HNSW
# are incremental -> a no-op here is correct, not an error. Distinguish unset (a
# misconfiguration -> FAIL loud: a silent SKIP on a pgvector stack leaves the
# drained vectors unqueryable, the exact 0-hits bug this tool fixes) from an
# explicit non-pgvector value (a deliberate chroma/HNSW stack -> SKIP). VECTOR_DB
# may be shell-sourced (not in .env on some stacks) -> source the env you use for
# `make start`, or check the running stack: docker exec kb-openwebui printenv VECTOR_DB.
if [ -z "${VECTOR_DB:-}" ]; then
  echo "FAIL  VECTOR_DB not set (in .env or shell). kb-finalize needs VECTOR_DB=pgvector." >&2
  echo "       source the env you use for 'make start', or check the running stack: docker exec ${OWUI_CONTAINER:-kb-openwebui} printenv VECTOR_DB" >&2
  exit 1
fi
if [ "$VECTOR_DB" != "pgvector" ]; then
  printf 'SKIP  VECTOR_DB=%s: index is incremental (no REINDEX needed); kb-finalize is a pgvector-ivfflat step.\n' "$VECTOR_DB"
  exit 0
fi

: "${PGVECTOR_USER:?FAIL  PGVECTOR_USER not set in .env (run: make bootstrap)}"
: "${PGVECTOR_DB:?FAIL  PGVECTOR_DB not set in .env (run: make bootstrap)}"
PG_CTN="${POSTGRES_CONTAINER:-kb-postgres}"

# --- --wait: poll GET /status until the drain is terminal ---------------------
# Terminal = pending+processing == 0 AND completed+failed >= source_count (every
# uploaded file reached a terminal state). The source_count guard is load-bearing:
# a /status error (curl fail / 401 / bad JSON) falls back to all-zero counts, for
# which pending+processing == 0 BUT accounted(0) < source_count(N>0) -> does NOT
# break -> keeps polling -> times out failing loud. Without it a transient /status
# error would break immediately and REINDEX while the drain is still in flight (the
# exact race this tool exists to prevent). Mirrors tests/test_09_gdrive_index.sh;
# src_count is read from /status (the gateway's authoritative source walk) instead
# of a local `find`, so this stays allowlist-agnostic.
if [ "$WAIT" = "1" ]; then
  : "${KB_HOST:?FAIL  KB_HOST not set -- export KB_HOST=http://host:port (see .env.template)}"
  : "${GDRIVE_KB_ID:?FAIL  GDRIVE_KB_ID not set in .env.local (run: make gdrive-index-bootstrap)}"
  : "${OPENWEBUI_ADMIN_API_KEY:?FAIL  OPENWEBUI_ADMIN_API_KEY not set in .env.local (run: make api-keys)}"
  wait_s="${GDRIVE_TEST_WAIT:-2400}"
  status_url="${KB_HOST}/status?source=gdrive&kb_id=${GDRIVE_KB_ID}&json=1"
  adm=(-H "Authorization: Bearer ${OPENWEBUI_ADMIN_API_KEY}")

  # source_count: the drain's reference set. Read ONCE before polling (the source
  # dir is stable after `make gdrive-sync`, so this is a constant for the drain).
  # Fail loud if unreadable -- do NOT finalize without a verified reference: an
  # unknown source_count would let the all-zero error fallback break the loop and
  # REINDEX mid-drain. -1 = unreadable (curl fail / bad JSON / missing key).
  src_count=$(curl -sS "$status_url" "${adm[@]}" 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get("source_count", -1))
except Exception:
    print("-1")
' 2>/dev/null || echo "-1")
  if [ "${src_count}" -lt 0 ] 2>/dev/null; then
    echo "FAIL  could not read source_count from /status (got '${src_count}'); refusing to finalize." >&2
    echo "       do not REINDEX without a verified terminal state; check KB_HOST / admin key / gateway health." >&2
    exit 1
  fi

  deadline=$(( $(date +%s) + wait_s ))
  completed=0; pending=0; processing=0; failed=0
  while :; do
    read -r completed pending processing failed < <(curl -sS "$status_url" "${adm[@]}" 2>/dev/null \
      | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get("indexed_count",0), d.get("pending",0), d.get("processing",0), d.get("failed",0))
except Exception:
    print("0 0 0 0")
' 2>/dev/null || echo "0 0 0 0") || true
    in_flight=$(( pending + processing ))
    accounted=$(( completed + failed ))
    if [ "$in_flight" = "0" ] && [ "$accounted" -ge "$src_count" ]; then break; fi
    [ "$(date +%s)" -ge "$deadline" ] && break
    sleep 10
  done
  in_flight=$(( pending + processing ))
  accounted=$(( completed + failed ))
  if [ "$in_flight" != "0" ] || [ "$accounted" -lt "$src_count" ]; then
    printf 'FAIL  drain did not terminate after %ss: completed=%s failed=%s pending=%s processing=%s source=%s (accounted=%s)\n' \
      "$wait_s" "$completed" "$failed" "$pending" "$processing" "$src_count" "$accounted" >&2
    echo '       do not REINDEX while inserts are in flight or /status is unreadable; check: docker logs kb-openwebui / kb-markitdown-ocr' >&2
    exit 1
  fi
  printf '==> drain terminal: completed=%s failed=%s pending=%s processing=%s source=%s\n' \
    "$completed" "$failed" "$pending" "$processing" "$src_count"
  [ "$src_count" -gt 0 ] \
    || echo 'WARN  source_count=0 (empty corpus) — REINDEXing anyway (idempotent, harmless)'
fi

# --- REINDEX ivfflat vector + GIN FTS (the finalize step) ---------------------
# Fixed index names (the schema owns them). A future HNSW migration renames
# idx_document_chunk_vector -> psql errors loud here, which is the correct
# signal to update this script (do not build speculative HNSW detection now).
reindex_one() {  # echo "<seconds> <rc>" for REINDEX INDEX $1
  local idx="$1" t0 t1 rc
  t0=$(date +%s)
  if docker exec "$PG_CTN" psql -U "$PGVECTOR_USER" -d "$PGVECTOR_DB" \
      -v ON_ERROR_STOP=1 -c "REINDEX INDEX $idx" >/dev/null 2>&1; then
    rc=0
  else
    rc=$?
  fi
  t1=$(date +%s)
  echo "$(( t1 - t0 )) $rc"
}

echo "==> REINDEX idx_document_chunk_vector (ivfflat) + idx_document_chunk_text_search (GIN FTS) on ${PG_CTN}"
read -r vec_s vec_rc < <(reindex_one idx_document_chunk_vector) || true
if [ "${vec_rc:-1}" -ne 0 ]; then
  echo "FAIL  REINDEX idx_document_chunk_vector failed (rc=${vec_rc:-<none>}, postgres=${PG_CTN})" >&2
  echo '       (index missing? a future HNSW migration renames it -> update this script)' >&2
  exit 1
fi
read -r fts_s fts_rc < <(reindex_one idx_document_chunk_text_search) || true
if [ "${fts_rc:-1}" -ne 0 ]; then
  echo "FAIL  REINDEX idx_document_chunk_text_search failed (rc=${fts_rc:-<none>}, postgres=${PG_CTN})" >&2
  exit 1
fi
echo "DONE  kb-finalize: ivfflat vector ${vec_s}s + GIN FTS ${fts_s}s (drained vectors now queryable)"