#!/usr/bin/env python3
"""Apply the lexical-dsl Tantivy-DSL patch (patch 11) to OWUI's pgvector backend.

Why: patch 10 shipped the BM25 lexical arm with `text ||| :query` (match-any OR,
no DSL). That fixes recall but cannot express phrase or boolean constraints. An
agent that needs "chunks containing the exact phrase X" or "termA AND termB" or
"termA but NOT termB" has no way to ask. This patch adds an opt-in `lexical-dsl`
retrieval mode that runs the user's query through ParadeDB's Tantivy
`QueryParser` via `paradedb.parse_with_field('text', :query, lenient => false)`.

Plumbing = query-string prefix (NOT a threaded `syntax` param). The gateway
(docker/gateway/app.py, RETRIEVE_MODES["lexical-dsl"]) prefixes the query with
the sentinel `LEXICAL_DSL_PREFIX = "KB_LEXICAL_DSL_V1::"` before forwarding it
to OWUI. OWUI recognizes the sentinel, strips it, and runs the DSL predicate on
the remainder. This keeps the change to ~3 sites in 2 OWUI files instead of
threading a new param through ~13 anchors across 4 files (the hybrid path
bypasses query_collection; AsyncVectorDBClient deliberately removed **kwargs).
The sentinel is a contract between two separately-versioned images (gateway +
kb-openwebui); a kb_check sentinel-agreement probe catches drift at gate time.

DSL scope = 4 operators (phrase `"..."`, phrase-slop `"..."~N`, `+AND`,
`+x -y` composite-NOT). A codex deep-dive against the pg_search 0.25.6 source
confirmed there is NO higher-level parser: `paradedb.parse()` uses the SAME
Tantivy QueryParser as `parse_with_field()` (only field scoping differs).
Fuzzy (`term~N`), regex (`/re/`), wildcard (`pre*`), and pure-NOT (`-x` alone)
fail on parser/grammar grounds (NOT escaping) -- they error or silently return
0 through `parse_with_field`. Full-DSL support needs a gateway-side compiler to
`paradedb.boolean(...)` builders (a larger follow-on); this patch ships the 4
operators the parser actually supports. See
docker/open-webui/PATCH.md patch 11 and the memory note
`pgsearch-no-higher-level-dsl-parser`.

C1 (CRITICAL) error-swallow fix -- mandatory. A DSL parse error is swallowed by
a 3-layer except/None chain and would fall through to a full-collection in-memory
BM25Retriever load with the raw DSL as query -> the agent gets confident WRONG
results, not an error. The chain has THREE swallow sites, each gated on the
sentinel so hybrid/lexical keep their fault-tolerant fallback:

  1. pgvector.py hybrid_search broad except -> return None (site 2 below).
  2. utils.py query_doc_with_native_hybrid_search except -> return None
     (site 3 below). The re-raise from site 2 propagates through
     asyncio.gather to here; re-raise again.
  3. utils.py query_collection hybrid-fallback except -> falls through to
     vector search (site 4 below). This is the THIRD swallow site: it catches
     the re-raised error from site 3 and would fall back to embedding the
     sentinel-prefixed query. Gated on `queries` (the param).

A FOURTH guard (site 7) is not a swallow site but a bypass: when an admin
enables rag.enable_hybrid_search_enriched_texts, query_collection_with_hybrid
_search (the /retrieve entry) skips the native path entirely and runs the
in-memory BM25Retriever on the raw sentinel query -- so NONE of the 3 re-raise
gates fires and a malformed DSL returns 200 with wrong results. Site 7 forces
the native path for any sentinel-prefixed query regardless of that setting.

The re-raise propagates to query_collection_handler (retrieval.py) ->
HTTPException(400) -> the gateway maps 4xx verbatim -> the agent sees a clear
parse error. `lenient => false` is the only safe choice for an explicit opt-in:
`true` silent-zeros on `:` / leading `-` (re-creating the bug on the exact
technical queries the /kb skill steers agents toward).

Runs AFTER apply_bm25_search.py in the Dockerfile chain. Fails loud (exit 1) if
an anchor is not found exactly once. Override the targets for local testing:
  OWUI_PGVECTOR_PY=/tmp/pgvector.py OWUI_UTILS_PY=/tmp/utils.py python3 apply_lexical_dsl.py
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
UTILS = pathlib.Path(
    os.environ.get(
        "OWUI_UTILS_PY",
        "/app/backend/open_webui/retrieval/utils.py",
    )
)

# The sentinel. MUST equal docker/gateway/app.py LEXICAL_DSL_PREFIX. Injected as
# a module-level constant into both patched files.
SENTINEL = "KB_LEXICAL_DSL_V1::"

# Shared header injected above the constant in each file.
_CONST_COMMENT = (
    "\n"
    "# Patch 11 (lexical-dsl): sentinel prefix. The gateway"
    " (docker/gateway/app.py\n"
    "# LEXICAL_DSL_PREFIX) prefixes lexical-dsl queries with this string; OWUI"
    " strips it\n"
    "# and runs the Tantivy DSL predicate (paradedb.parse_with_field). Drift"
    " between the two\n"
    "# images is caught by the kb_check sentinel-agreement probe. MUST equal"
    " the gateway\n"
    "# constant exactly.\n"
    f"LEXICAL_DSL_PREFIX = {SENTINEL!r}\n"
)

# --- pgvector.py -----------------------------------------------------------

# Site 1: inject the sentinel constant after the module logger.
PGV_CONST_OLD = "log = logging.getLogger(__name__)\n"
PGV_CONST_NEW = PGV_CONST_OLD + _CONST_COMMENT

# Site 2: the FTS arm. OLD = the patch-10 ||| block (apply_bm25_search.py
# SITE1_NEW). NEW = branch on the sentinel: DSL -> parse_with_field, else the
# patch-10 ||| block verbatim.
PGV_FTS_OLD = (
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

PGV_FTS_NEW = (
    '            if bm25_weight > 0 and query and query.strip():\n'
    '                # Patch 11 (lexical-dsl): the gateway prefixes lexical-dsl\n'
    '                # queries with LEXICAL_DSL_PREFIX. Branch on it: the DSL arm\n'
    '                # runs paradedb.parse_with_field (Tantivy DSL: phrase, +AND,\n'
    '                # +x -y composite-NOT; lenient => false so bad syntax RAISES);\n'
    '                # the else arm is the patch-10 ||| match-any OR (hybrid +\n'
    '                # lexical). pdb.score = real BM25 (IDF + length norm); the\n'
    '                # value is discarded by RRF fusion (ordinal rank only).\n'
    '                is_dsl = query.startswith(LEXICAL_DSL_PREFIX)\n'
    '                if is_dsl:\n'
    '                    fts_rows = self.session.execute(\n'
    '                        text("""\n'
    '                            SELECT\n'
    '                                document_chunk.id AS id,\n'
    '                                document_chunk.text AS text,\n'
    '                                document_chunk.vmetadata AS vmetadata,\n'
    '                                pdb.score(document_chunk.id) AS rank\n'
    '                            FROM document_chunk\n'
    '                            WHERE document_chunk.collection_name = :collection_name\n'
    '                              AND document_chunk.id @@@ paradedb.parse_with_field(\n'
    "                                     'text', :query, lenient => false)\n"
    '                            ORDER BY pdb.score(document_chunk.id) DESC\n'
    '                            LIMIT :limit\n'
    '                        """),\n'
    '                        {\n'
    "                            'collection_name': collection_name,\n"
    "                            'query': query[len(LEXICAL_DSL_PREFIX):],\n"
    "                            'limit': limit,\n"
    '                        },\n'
    '                    )\n'
    '                else:\n'
    '                    fts_rows = self.session.execute(\n'
    '                        text("""\n'
    '                            SELECT\n'
    '                                document_chunk.id AS id,\n'
    '                                document_chunk.text AS text,\n'
    '                                document_chunk.vmetadata AS vmetadata,\n'
    '                                pdb.score(document_chunk.id) AS rank\n'
    '                            FROM document_chunk\n'
    '                            WHERE document_chunk.collection_name = :collection_name\n'
    '                              AND document_chunk.text ||| :query\n'
    '                            ORDER BY pdb.score(document_chunk.id) DESC\n'
    '                            LIMIT :limit\n'
    '                        """),\n'
    '                        {\n'
    "                            'collection_name': collection_name,\n"
    "                            'query': query,\n"
    "                            'limit': limit,\n"
    '                        },\n'
    '                    )\n'
    '                fts_results = [dict(row) for row in fts_rows.mappings().all()]\n'
    '                self.session.rollback()\n'
)

# Site 3: C1 re-raise at the pgvector.py hybrid_search broad except. Re-raise
# DSL errors so a parse failure propagates instead of returning None (which
# falls through to a full-collection BM25Retriever load -> wrong results).
PGV_EXCEPT_OLD = (
    '        except Exception as e:\n'
    '            self.session.rollback()\n'
    "            log.exception(f'Error during hybrid search: {e}')\n"
    '            return None\n'
)

PGV_EXCEPT_NEW = (
    '        except Exception as e:\n'
    '            self.session.rollback()\n'
    '            # Patch 11: re-raise DSL errors so a parse failure propagates to\n'
    '            # the route handler (HTTPException 400) instead of returning None\n'
    '            # and falling through to a full-collection BM25Retriever load\n'
    '            # (confident wrong results). Gated on the sentinel so\n'
    '            # hybrid/lexical keep their fault-tolerant fallback.\n'
    '            if query.startswith(LEXICAL_DSL_PREFIX):\n'
    '                raise\n'
    "            log.exception(f'Error during hybrid search: {e}')\n"
    '            return None\n'
)

# --- utils.py --------------------------------------------------------------

# Site 4: inject the sentinel constant after the module logger.
UTL_CONST_OLD = "log = logging.getLogger(__name__)\n"
UTL_CONST_NEW = UTL_CONST_OLD + _CONST_COMMENT

# Site 5: C1 re-raise at query_doc_with_native_hybrid_search except. `query` is
# a param of the function (in scope at the except). The re-raise from
# pgvector.py propagates through asyncio.gather to here; re-raise again so it
# escapes to query_collection_handler (HTTPException 400).
UTL_NATIVE_EXCEPT_OLD = (
    '    except Exception as e:\n'
    "        log.debug(f'Native hybrid search failed for {collection_name}, falling back to legacy hybrid search: {e}')\n"
    '        return None\n'
)

UTL_NATIVE_EXCEPT_NEW = (
    '    except Exception as e:\n'
    '        # Patch 11: re-raise DSL errors so they escape to\n'
    '        # query_collection_handler (HTTPException 400) instead of returning\n'
    '        # None and falling back to a full-collection BM25Retriever load.\n'
    '        # Gated on the sentinel; hybrid/lexical keep their fallback.\n'
    '        if query.startswith(LEXICAL_DSL_PREFIX):\n'
    '            raise\n'
    "        log.debug(f'Native hybrid search failed for {collection_name}, falling back to legacy hybrid search: {e}')\n"
    '        return None\n'
)

# Site 6: C1 re-raise at query_collection hybrid-fallback except. `queries` is
# the param (list[str], in scope). This is the THIRD swallow site: it catches
# the re-raise from site 5 and would fall through to vector search (embedding
# the sentinel-prefixed query). Gated on `queries` so the raise escapes to the
# route handler (HTTPException 400).
UTL_COLLECT_EXCEPT_OLD = (
    '        except Exception as e:\n'
    "            log.debug(f'Hybrid search failed, falling back to vector search: {e}')\n"
)

UTL_COLLECT_EXCEPT_NEW = (
    '        except Exception as e:\n'
    '            # Patch 11: re-raise DSL errors so they escape to the route\n'
    '            # handler (HTTPException 400) instead of falling back to vector\n'
    '            # search (which would embed the sentinel-prefixed query). Gated\n'
    '            # on the sentinel; hybrid/lexical keep their vector fallback.\n'
    '            if any(q.startswith(LEXICAL_DSL_PREFIX) for q in queries):\n'
    '                raise\n'
    "            log.debug(f'Hybrid search failed, falling back to vector search: {e}')\n"
)

# Site 7: enriched-texts bypass in query_collection_with_hybrid_search (the
# /retrieve entry point). When an admin sets rag.enable_hybrid_search_enriched
# _texts=true, this function skips the native hybrid path (where the C1
# re-raise gates live) and runs the legacy in-memory BM25Retriever on the raw
# sentinel-prefixed query -> a malformed DSL returns 200 with wrong results
# (no lenient => false raise fires). Force the native path for any
# sentinel-prefixed query regardless of the enriched setting, so the DSL
# predicate (and its C1 raise) runs. `queries` is the param (list[str], in
# scope). The native path's asyncio.gather propagates the parse raise to the
# route handler (HTTPException 400). Found by a codex blocker review.
UTL_ENRICHED_BYPASS_OLD = (
    '    if not enable_enriched_texts:\n'
    '\n'
    '        async def process_native_query(collection_name, query):\n'
)

UTL_ENRICHED_BYPASS_NEW = (
    '    # Patch 11: a sentinel-prefixed (lexical-dsl) query MUST take the native\n'
    '    # path even when an admin enabled enriched texts -- otherwise the\n'
    '    # in-memory BM25Retriever runs on the raw sentinel query and a malformed\n'
    '    # DSL returns 200 with wrong results (no lenient => false raise). The\n'
    '    # native path is where the C1 re-raise gates live.\n'
    '    if not enable_enriched_texts or any(\n'
    '        q.startswith(LEXICAL_DSL_PREFIX) for q in queries\n'
    '    ):\n'
    '\n'
    '        async def process_native_query(collection_name, query):\n'
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
    if not UTILS.exists():
        print(f"FAIL target not found: {UTILS}", file=sys.stderr)
        sys.exit(1)

    pgv = PGVECTOR.read_text()
    pgv = apply(pgv, PGV_CONST_OLD, PGV_CONST_NEW, "pgvector site1 (inject LEXICAL_DSL_PREFIX constant)")
    pgv = apply(pgv, PGV_FTS_OLD, PGV_FTS_NEW, "pgvector site2 (FTS arm: ||| -> is_dsl branch, parse_with_field)")
    pgv = apply(pgv, PGV_EXCEPT_OLD, PGV_EXCEPT_NEW, "pgvector site3 (C1 re-raise at hybrid_search except)")
    PGVECTOR.write_text(pgv)

    utl = UTILS.read_text()
    utl = apply(utl, UTL_CONST_OLD, UTL_CONST_NEW, "utils site4 (inject LEXICAL_DSL_PREFIX constant)")
    utl = apply(utl, UTL_NATIVE_EXCEPT_OLD, UTL_NATIVE_EXCEPT_NEW, "utils site5 (C1 re-raise at native-hybrid except)")
    utl = apply(utl, UTL_COLLECT_EXCEPT_OLD, UTL_COLLECT_EXCEPT_NEW, "utils site6 (C1 re-raise at query_collection fallback except)")
    utl = apply(utl, UTL_ENRICHED_BYPASS_OLD, UTL_ENRICHED_BYPASS_NEW, "utils site7 (force native path for sentinel queries when enriched texts enabled)")
    UTILS.write_text(utl)

    print(
        f"OK lexical-dsl patch applied (pgvector.py: 3 sites, utils.py: 4 sites, "
        f"sentinel={SENTINEL!r})"
    )


if __name__ == "__main__":
    main()