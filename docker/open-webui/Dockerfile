# syntax=docker/dockerfile:1.9
#
# Thin overlay on the pinned official Open WebUI image that applies eleven build-time
# patches to the backend: the path-aware dedup-hash patch (retrieval.py), the
# upload-idempotency patch (files.py), the source-mtime chunk-metadata patch
# (retrieval.py), the orphan-vector-cleanup-on-delete patch (files.py), the
# offset-aware span-preserving chunking + MDS-path removal patch (retrieval.py),
# the resilient terminal-status patch (retrieval.py + models/files.py), the
# per-request hybrid-mode override patch (retrieval/utils.py + routers/retrieval.py),
# the top-k preservation patch (retrieval/utils.py + routers/retrieval.py), the
# skip-cosine-reranker patch (retrieval/utils.py), the BM25 lexical-arm patch
# (retrieval/vector/dbs/pgvector.py -- ParadeDB pg_search replaces
# plainto_tsquery + ts_rank_cd; requires the kb-postgres image + make kb-bm25-init),
# and the lexical-dsl Tantivy-DSL patch (retrieval/vector/dbs/pgvector.py +
# retrieval/utils.py -- a sentinel-prefixed query branch runs
# paradedb.parse_with_field for phrase/+AND/composite-NOT; re-raises parse errors
# as HTTP 400 instead of swallowing them into a full-collection fallback).
# See docker/open-webui/PATCH.md for the why, the anchors, and the rebase steps.
#
# No frontend rebuild: the patches touch three backend files in place.
# Build seconds, not minutes. Each apply script fails loud (exit 1) if its anchor
# has drifted on a base-digest bump, so a silent mismatch cannot ship.
#
# Build:  docker compose build openwebui
# Rebuild after a base bump: change OPENWEBUI_BASE_DIGEST below (or via
#         --build-arg), rebuild. If an anchor drifted, the build fails and
#         PATCH.md's rebase steps apply.

# Pinned by digest (immutable); the :main tag is provenance only — the digest
# binds the build, not the tag. = Open WebUI 0.11.0 (frontend package.json at pin
# time). The :0.11.0 release tag is a separate build artifact (manifest digest
# sha256:72c0ba64…), same 0.11.0 content; this pin keeps the :main build the
# eleven patches were validated against. See PATCH.md "Base image".
ARG OPENWEBUI_BASE_DIGEST=sha256:6a773e5c3a246b65cbe74ce942b294292c0e5f81c138f703d111bc162f7d7c3d
FROM ghcr.io/open-webui/open-webui:main@${OPENWEBUI_BASE_DIGEST}

# Ops debug tool: the sqlite3 CLI lets an operator inspect webui.db (the OWUI
# SQLite app DB: file / knowledge / knowledge_file tables) without a host-side
# Python one-liner:
#   docker exec kb-openwebui sqlite3 /app/backend/data/webui.db "SELECT ..."
# Installed for webui.db only (Chroma is removed; vector_db/ is gone). Not an
# OWUI behavioral patch, so the patch-suffix image tag below does not track it.
# Placed before the patch COPY/RUN so this layer caches independently of patch
# changes.
RUN apt-get update && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY apply_path_hash.py /tmp/apply_path_hash.py
COPY apply_upload_idempotency.py /tmp/apply_upload_idempotency.py
COPY apply_mtime_to_chunks.py /tmp/apply_mtime_to_chunks.py
COPY apply_vector_cleanup_on_delete.py /tmp/apply_vector_cleanup_on_delete.py
COPY apply_offset_aware_chunking.py /tmp/apply_offset_aware_chunking.py
COPY apply_terminal_status.py /tmp/apply_terminal_status.py
COPY apply_query_mode.py /tmp/apply_query_mode.py
COPY apply_query_top_k.py /tmp/apply_query_top_k.py
COPY apply_skip_cosine_reranker.py /tmp/apply_skip_cosine_reranker.py
COPY apply_bm25_search.py /tmp/apply_bm25_search.py
COPY apply_lexical_dsl.py /tmp/apply_lexical_dsl.py
RUN python3 /tmp/apply_path_hash.py && python3 /tmp/apply_upload_idempotency.py && python3 /tmp/apply_mtime_to_chunks.py && python3 /tmp/apply_vector_cleanup_on_delete.py && python3 /tmp/apply_offset_aware_chunking.py && python3 /tmp/apply_terminal_status.py && python3 /tmp/apply_query_mode.py && python3 /tmp/apply_query_top_k.py && python3 /tmp/apply_skip_cosine_reranker.py && python3 /tmp/apply_bm25_search.py && python3 /tmp/apply_lexical_dsl.py && rm /tmp/apply_path_hash.py /tmp/apply_upload_idempotency.py /tmp/apply_mtime_to_chunks.py /tmp/apply_vector_cleanup_on_delete.py /tmp/apply_offset_aware_chunking.py /tmp/apply_terminal_status.py /tmp/apply_query_mode.py /tmp/apply_query_top_k.py /tmp/apply_skip_cosine_reranker.py /tmp/apply_bm25_search.py /tmp/apply_lexical_dsl.py