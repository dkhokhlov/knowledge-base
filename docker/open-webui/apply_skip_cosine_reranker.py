#!/usr/bin/env python3
"""Apply the skip-cosine-reranker patch (patch 9) to OWUI retrieval/utils.py.

Why: when no real reranker is configured (RAG_RERANKING_ENGINE=""),
RerankCompressor.acompress_documents falls back to embedding-cosine: it re-embeds
the query + every candidate and re-sorts by pure-semantic similarity. That is an
EXTRA embedding pass (a cost, not a perf hack), and on the hybrid path it re-sorts
the BM25/RRF-fused results and buries the exact keyword/register chunks BM25
surfaced (measured: CAP_ENGAGE exact chunk dropped to r10, 0x1c05 absent). The
fallback exists to keep the langchain ContextualCompressionRetriever pipeline
uniform, not because cosine rerank helps.

Fix: when reranking_function is None, do NOT cosine-rerank. Preserve the input
(RRF-fused) order: keep any existing per-doc 'score' (the native path and BM25
both attach one), else assign a decreasing rank score so the downstream
sort-by-distance preserves this order. Cap at top_n (>= k via patch 8). This is a
SINGLE-site patch to the compressor's else-branch, so it covers BOTH the native
pgvector path and the legacy ensemble path uniformly (gating the two call sites
instead would drop the legacy path's score and yield None distances downstream).

Anchors come from the RUNNING image (kb-openwebui), not the upstream clone.

Fails loud (exit 1) if the anchor is not found exactly once. Override the target
for local testing:
  OWUI_UTILS_PY=/tmp/utils.py python3 apply_skip_cosine_reranker.py
"""
import os
import pathlib
import sys

UTILS = pathlib.Path(os.environ.get("OWUI_UTILS_PY", "/app/backend/open_webui/retrieval/utils.py"))

# The cosine fallback inside acompress_documents (the else-branch body). 12-space
# indent. Replaced with a pass-through that preserves the fused order + a score.
SITE_OLD = (
    "            from sentence_transformers import util as st_util\n"
    "\n"
    "            query_embedding = await self.embedding_function(query, RAG_EMBEDDING_QUERY_PREFIX)\n"
    "            doc_texts = [doc.page_content for doc in documents]\n"
    "            document_embedding = await self.embedding_function(doc_texts, RAG_EMBEDDING_CONTENT_PREFIX)\n"
    "            scores = st_util.cos_sim(query_embedding, document_embedding)[0]"
)
SITE_NEW = (
    "            # No real reranker configured: do NOT cosine-rerank. The cosine\n"
    "            # fallback re-sorts by pure-semantic similarity and buries the exact\n"
    "            # keyword/FTS matches the hybrid search surfaced. Preserve the input\n"
    "            # (RRF-fused) order; keep any existing per-doc 'score' (the native path\n"
    "            # and BM25 both attach one), else assign a decreasing rank score so the\n"
    "            # downstream sort-by-distance preserves this order. Cap at top_n\n"
    "            # (>= k via patch 8's k_reranker=max(k,...)).\n"
    "            final_results = []\n"
    "            for idx, doc in enumerate(documents[: self.top_n]):\n"
    "                metadata = doc.metadata\n"
    "                if 'score' not in metadata:\n"
    "                    metadata['score'] = float(len(documents) - idx)\n"
    "                final_results.append(\n"
    "                    Document(page_content=doc.page_content, metadata=metadata)\n"
    "                )\n"
    "            return final_results"
)


def apply(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        print(f"FAIL {label}: expected exactly 1 occurrence of anchor, found {n}", file=sys.stderr)
        print(f"     anchor: {old!r}", file=sys.stderr)
        sys.exit(1)
    return text.replace(old, new)


def main() -> None:
    if not UTILS.exists():
        print(f"FAIL target not found: {UTILS}", file=sys.stderr)
        sys.exit(1)
    text = UTILS.read_text()
    text = apply(text, SITE_OLD, SITE_NEW, "compressor else-branch (cosine fallback)")
    UTILS.write_text(text)
    print(f"OK skip-cosine-reranker patch applied to {UTILS} (1 site)")


if __name__ == "__main__":
    main()