#!/usr/bin/env bash
# Operator tool: create the ParadeDB pg_search extension + the BM25 index on
# document_chunk that the OWUI hybrid/lexical FTS arm (patch 10) queries, and
# drop the now-dead GIN FTS index the old plainto_tsquery arm used.
#
# Why a separate script (not OWUI init): OWUI init (pgvector.py) runs in one
# transaction and re-raises on any failure, which would take the whole vector
# client down if the extension/index creation failed. pg_search also needs
# shared_preload_libraries=pg_search (baked into the kb-postgres image from
# docker/postgres/); a missing preload makes CREATE EXTENSION fail. So this is
# an operator step, run once after the kb-postgres image is up.
#
# Idempotent: CREATE EXTENSION IF NOT EXISTS + CREATE INDEX IF NOT EXISTS +
# DROP INDEX IF EXISTS. Re-run is safe. A def change (a new indexed column) =
# DROP INDEX idx_document_chunk_bm25 + re-run.
#
# Preconditions:
#   - kb-postgres running with shared_preload_libraries=pg_search. On the stock
#     pgvector image, CREATE EXTENSION pg_search fails loud (preload missing).
#   - document_chunk exists (OWUI init creates it at vector-client init, before
#     /health goes green). The guard below fails loud if it is absent.
#   - PGVECTOR_USER / PGVECTOR_DB in .env (scaffolded by make bootstrap).
#   - Run when no gdrive drain is in flight: CREATE INDEX takes a brief
#     ACCESS EXCLUSIVE lock on document_chunk (~seconds for 9703 chunks; one-time).
#
# Container targeting (C3): POSTGRES_CONTAINER (iso runs export it via
# lib-e2e-env.sh) or kb-postgres (live). Mirrors scripts/kb-finalize.sh.
#
# Entry point (see Makefile): make kb-bm25-init
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

# Precondition: document_chunk must exist (OWUI init creates it at vector-client
# init). A missing table makes CREATE INDEX fail with an opaque error; fail loud
# here instead.
rel=$(docker exec -i "$PG_CTN" psql -U "$PGVECTOR_USER" -d "$PGVECTOR_DB" -tAc \
  "SELECT to_regclass('document_chunk')")
if [ "$rel" != "document_chunk" ]; then
  echo "FAIL  document_chunk table not found in $PGVECTOR_DB (container $PG_CTN)." >&2
  echo "       OWUI creates it at vector-client init. Start the stack (make start)" >&2
  echo "       and wait for /health, then re-run. Iso run: check POSTGRES_CONTAINER." >&2
  exit 1
fi

docker exec -i "$PG_CTN" psql -U "$PGVECTOR_USER" -d "$PGVECTOR_DB" -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;      -- pg_search depends on vector
CREATE EXTENSION IF NOT EXISTS pg_search;
CREATE INDEX IF NOT EXISTS idx_document_chunk_bm25
  ON document_chunk USING bm25 (id, (text::pdb.simple), (collection_name::pdb.literal))
  WITH (key_field='id');
DROP INDEX IF EXISTS idx_document_chunk_text_search;  -- dead GIN FTS index (patch 10 Site 2 removed its creation); one-time live cleanup
SQL

echo "OK  pg_search extension + BM25 index ready (container $PG_CTN)."