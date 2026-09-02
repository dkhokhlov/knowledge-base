#!/usr/bin/env python3
"""Apply the BM25 lexical-arm patch (patch 10) to OWUI's pgvector backend.

Why: the hybrid retrieval FTS arm (retrieval/vector/dbs/pgvector.py
hybrid_search) builds its query with `plainto_tsquery('simple', :query)`,
which ANDs every token. No single chunk holds all terms of a multi-term query,
so the lexical arm returns 0 and hybrid silently collapses to vector (RRF
0.5/(rank+60), pure-vector order). The arm also scores with `ts_rank_cd`
(cover-density, no IDF, no length normalization), so rare-identifier queries
rank poorly even when recall works.

Fix: replace the `plainto_tsquery` + `ts_rank_cd` + `@@` SQL with ParadeDB
`pg_search` real BM25. ONE static SQL serves both modes (no bm25_weight branch):

- `text ||| :query` = the match-any OR operator. It tokenizes its RHS (no
  Tantivy-DSL parsing), so a bare multi-term query ORs every token (subsumes the
  recall fix: any token matches) and a natural-language RAG query cannot
  misparse as query syntax. It is colon/dash/quote-safe: `error: DMA_WRR_VEC`
  ORs the tokens `error`/`dma`/`wrr`/`vec` and returns rows, not silent-zero.
  This same block serves hybrid (`bm25_weight=0.5`, the global default) and
  lexical (`bm25_weight=1.0`, vector arm skipped) -- the only difference between
  the modes is the final ORDER (lexical = pure BM25; hybrid = RRF fusion), which
  is decided downstream, not in this SQL.
- `pdb.score(document_chunk.id)` = real BM25 (IDF + length normalization). The
  `rank` value is discarded by RRF fusion (merge_hybrid_search_results reads
  only the arm's ordinal rank) -- it exists for the ORDER BY and for psql probes.

Why no Lucene DSL in patch 10: `paradedb.parse_with_field(..., lenient => true)`
silent-zeros on `:` / leading `-` (lenient = "drop what doesn't parse", not
"keep terms") -- re-creating the bug on the exact technical queries the /kb
skill steers agents toward. The DSL (phrase / +term AND / -term NOT / field:) is
deferred to patch 11 as an explicit opt-in `lexical-dsl` mode where the agent is
responsible for quoting/escaping. See docker/open-webui/PATCH.md patch 10.

Site 1: the FTS arm inside hybrid_search. Replace the plainto_tsquery +
ts_rank_cd + @@ query (and its execute/params wrapper) with the ||| + pdb.score
query above.

Site 2: drop the dead GIN FTS index creation. The old plainto_tsquery @@
to_tsvector query (Site 1) was the ONLY user of the GIN index
idx_document_chunk_text_search (_ensure_text_search_index, pgvector.py, called
at init). With Site 1 gone it is dead, but OWUI init re-runs CREATE INDEX IF NOT
EXISTS on every start so it stays created + maintained (GIN is slow to update --
real cost during a 9703-chunk drain). Remove the _ensure_text_search_index()
call; the method itself is left as unused dead code (do not delete unless asked).
Rollback self-heals: the reverted stock image re-creates + uses the GIN index.

The extension + BM25 index are created by `make kb-bm25-init`
(scripts/kb-bm25-init.sh), NOT at OWUI init -- OWUI init (pgvector.py) re-raises
on any failure and would take the whole vector client down if the
extension/index were ensured there.

Fails loud (exit 1) if an anchor is not found exactly once. Override the
target for local testing:
  OWUI_PGVECTOR_PY=/tmp/pgvector.py python3 apply_bm25_search.py
"""
import os
import pathlib
import sys

PGVECTOR = pathlib.Path(
    os.environ.get(
        "OWUI_PGVECTOR_PY",
        "/app/backend/open_webui/retrieval/vector/dbs/pgvector.py",
    )
)

# Site 1: the FTS arm inside hybrid_search. Replace the plainto_tsquery +
# ts_rank_cd + @@ query (and its execute/params wrapper) with the tokenized
# ||| OR operator + pdb.score (real BM25). One static SQL serves both hybrid
# (bm25_weight=0.5) and lexical (bm25_weight=1.0) -- no branch.
SITE1_OLD = (
    '            if bm25_weight > 0 and query and query.strip():\n'
    '                fts_rows = self.session.execute(\n'
    '                    text("""\n'
    '                        WITH fts_query AS (\n'
    "                            SELECT plainto_tsquery('simple', :query) AS query\n"
    '                        )\n'
    '                        SELECT\n'
    '                            document_chunk.id AS id,\n'
    '                            document_chunk.text AS text,\n'
    '                            document_chunk.vmetadata AS vmetadata,\n'
    '                            ts_rank_cd(\n'
    "                                to_tsvector('simple', coalesce(document_chunk.text, '')),\n"
    '                                fts_query.query\n'
    '                            ) AS rank\n'
    '                        FROM document_chunk, fts_query\n'
    '                        WHERE document_chunk.collection_name = :collection_name\n'
    "                          AND to_tsvector('simple', coalesce(document_chunk.text, '')) @@ fts_query.query\n"
    '                        ORDER BY rank DESC\n'
    '                        LIMIT :limit\n'
    '                    """),\n'
    '                    {\n'
    "                        'collection_name': collection_name,\n"
    "                        'query': query,\n"
    "                        'limit': limit,\n"
    '                    },\n'
    '                )\n'
    '                fts_results = [dict(row) for row in fts_rows.mappings().all()]\n'
    '                self.session.rollback()\n'
)

SITE1_NEW = (
    '            if bm25_weight > 0 and query and query.strip():\n'
    '                # ParadeDB pg_search real BM25. `text ||| :query` is the\n'
    '                # match-any OR operator: it tokenizes its RHS (no Tantivy-DSL\n'
    '                # parsing), so a bare multi-term query ORs every token (the\n'
    '                # recall fix) and a natural-language query cannot misparse as\n'
    '                # query syntax (colon/dash/quote-safe). One static SQL serves\n'
    '                # both hybrid (bm25_weight=0.5) and lexical (==1.0, vector arm\n'
    '                # skipped) -- the mode difference is the downstream ORDER, not\n'
    '                # this SQL. pdb.score = real BM25 (IDF + length norm); the\n'
    '                # value is discarded by RRF fusion (ordinal rank only).\n'
    '                fts_rows = self.session.execute(\n'
    '                    text("""\n'
    '                        SELECT\n'
    '                            document_chunk.id AS id,\n'
    '                            document_chunk.text AS text,\n'
    '                            document_chunk.vmetadata AS vmetadata,\n'
    '                            pdb.score(document_chunk.id) AS rank\n'
    '                        FROM document_chunk\n'
    '                        WHERE document_chunk.collection_name = :collection_name\n'
    '                          AND document_chunk.text ||| :query\n'
    '                        ORDER BY pdb.score(document_chunk.id) DESC\n'
    '                        LIMIT :limit\n'
    '                    """),\n'
    '                    {\n'
    "                        'collection_name': collection_name,\n"
    "                        'query': query,\n"
    "                        'limit': limit,\n"
    '                    },\n'
    '                )\n'
    '                fts_results = [dict(row) for row in fts_rows.mappings().all()]\n'
    '                self.session.rollback()\n'
)

# Site 2: drop the dead GIN FTS index creation at OWUI init. The GIN index
# idx_document_chunk_text_search was used only by the old plainto_tsquery @@
# to_tsvector query (Site 1); with Site 1 gone it is dead. OWUI init re-runs
# CREATE INDEX IF NOT EXISTS on every start, so removing this call stops the
# dead index from being created + maintained. The _ensure_text_search_index
# method itself is left in place as unused dead code.
SITE2_OLD = (
    '            self._ensure_vector_index(index_method, index_options)\n'
    '            self._ensure_text_search_index()\n'
)

SITE2_NEW = (
    '            self._ensure_vector_index(index_method, index_options)\n'
)


def apply(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        print(f"FAIL {label}: expected exactly 1 occurrence of anchor, found {n}", file=sys.stderr)
        print(f"     anchor: {old!r}", file=sys.stderr)
        sys.exit(1)
    return text.replace(old, new)


def main() -> None:
    if not PGVECTOR.exists():
        print(f"FAIL target not found: {PGVECTOR}", file=sys.stderr)
        sys.exit(1)
    text = PGVECTOR.read_text()
    text = apply(
        text,
        SITE1_OLD,
        SITE1_NEW,
        "site1 (FTS arm SQL: plainto_tsquery+ts_rank_cd -> paradedb ||| + pdb.score)",
    )
    text = apply(
        text,
        SITE2_OLD,
        SITE2_NEW,
        "site2 (drop dead GIN FTS index creation: remove _ensure_text_search_index call)",
    )
    PGVECTOR.write_text(text)
    print(f"OK BM25 lexical-arm patch applied ({PGVECTOR.name}: 2 sites, ||| both arms + GIN drop)")


if __name__ == "__main__":
    main()