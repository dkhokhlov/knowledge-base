#!/usr/bin/env bash
# Operator tool: roll back patch 10's BM25 index + the pg_search extension.
# Run BEFORE reverting the kb-postgres image to the stock pgvector image (and
# before reverting the OWUI image to the pre-patch-10 build). DROP INDEX before
# DROP EXTENSION: pg_search refuses to drop while idx_document_chunk_bm25
# depends on its types (pdb.literal / pdb.simple), so the order is load-bearing.
#
# M7: refuses to run if pg_search is already absent (the image was already
# reverted) -- nothing to roll back, fail loud instead of a silent no-op.
#
# Coordination: dropping the BM25 index while the patch-10 OWUI image is still
# running makes its `text ||| :query` FTS arm error (no USING bm25 index) and
# fall into the langchain per-query full-collection fallback. So run this
# together with reverting the OWUI image (rebuild openwebui without patch 10),
# not as a standalone DB change on a live patch-10 stack.
#
# The `vector` extension is NOT dropped: OWUI still uses it. The dead GIN FTS
# index is not recreated here -- the reverted (stock) OWUI image re-creates +
# uses it on its next init (_ensure_text_search_index).
#
# Container targeting: POSTGRES_CONTAINER (iso) or kb-postgres (live).
#
# Entry point (see Makefile): make kb-bm25-rollback
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck source=/dev/null
. ./.env
# shellcheck source=/dev/null
. ./.env.local 2>/dev/null || true
set +a

: "${PGVECTOR_USER:?FAIL  PGVECTOR_USER not set in .env (run: make bootstrap)}"
: "${PGVECTOR_DB:?FAIL  PGVECTOR_DB not set in .env (run: make bootstrap)}"
PG_CTN="${POSTGRES_CONTAINER:-kb-postgres}"

# Precondition (M7): if pg_search is already absent, the image was already
# reverted -- nothing to roll back. Fail loud instead of a silent no-op.
has=$(docker exec -i "$PG_CTN" psql -U "$PGVECTOR_USER" -d "$PGVECTOR_DB" -tAc \
  "SELECT count(*) FROM pg_extension WHERE extname='pg_search'")
if [ "$has" = "0" ]; then
  echo "FAIL  pg_search extension not present in $PGVECTOR_DB (container $PG_CTN)." >&2
  echo "       The image was already reverted -- nothing to roll back." >&2
  exit 1
fi

docker exec -i "$PG_CTN" psql -U "$PGVECTOR_USER" -d "$PGVECTOR_DB" -v ON_ERROR_STOP=1 <<'SQL'
DROP INDEX IF EXISTS idx_document_chunk_bm25;
DROP EXTENSION IF EXISTS pg_search;
SQL

echo "OK  BM25 index + pg_search extension removed (container $PG_CTN)."
echo "    Next: revert the kb-postgres + openwebui images (rebuild from the stock bases)."