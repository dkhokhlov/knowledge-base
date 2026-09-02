#!/usr/bin/env python3
"""Apply the per-request hybrid-mode override patch (patch 7) to OWUI.

Why: query_collection() (retrieval/utils.py) re-reads the GLOBAL
rag.enable_hybrid_search and ignores the per-request `hybrid` flag, so
`hybrid=False` (pure vector) is a no-op when the global is on, and the api-gateway
/retrieve `mode=vector` cannot override the global. The handler's else-branch
(non-hybrid) also never passes `hybrid`/`hybrid_bm25_weight` into query_collection,
so the per-request value is dropped before it can be honored.

Fix:
  1. Add `hybrid` + `hybrid_bm25_weight` params to query_collection.
  2. Replace the global-only gate with: honor the per-request `hybrid` over the
     global (False -> pure vector; True+weight=1.0 -> pure FTS/lexical; omitted ->
     the global); honor the per-request `hybrid_bm25_weight` over the global.
  3. Thread the resolved `hybrid_bm25_weight` into query_collection_with_hybrid_search.
  4. Pass `hybrid`/`hybrid_bm25_weight` from the handler's else-branch so the
     per-request flag reaches query_collection (the hybrid branch already passes
     hybrid_bm25_weight into query_collection_with_hybrid_search).

Targets retrieval/utils.py (query_collection) + routers/retrieval.py (the
/query/collection handler else-branch). Anchors come from the RUNNING image
(kb-openwebui), NOT the upstream clone -- the image uses ThreadPoolExecutor +
f-string logging; the clone uses asyncio.gather + %s and the anchors differ.

Fails loud (exit 1) if any anchor is not found exactly once. Override the target
files for local testing:
  OWUI_UTILS_PY=/tmp/utils.py OWUI_RETRIEVAL_PY=/tmp/retrieval.py python3 apply_query_mode.py
"""
import os
import pathlib
import sys

UTILS = pathlib.Path(os.environ.get("OWUI_UTILS_PY", "/app/backend/open_webui/retrieval/utils.py"))
RETRIEVAL = pathlib.Path(os.environ.get("OWUI_RETRIEVAL_PY", "/app/backend/open_webui/routers/retrieval.py"))

# Site 1: query_collection signature -- add the two per-request params.
SITE1_OLD = (
    "async def query_collection(\n"
    "    request,\n"
    "    collection_names: list[str],\n"
    "    queries: list[str],\n"
    "    embedding_function,\n"
    "    k: int,\n"
    ") -> dict:"
)
SITE1_NEW = (
    "async def query_collection(\n"
    "    request,\n"
    "    collection_names: list[str],\n"
    "    queries: list[str],\n"
    "    embedding_function,\n"
    "    k: int,\n"
    "    hybrid: bool | None = None,\n"
    "    hybrid_bm25_weight: float | None = None,\n"
    ") -> dict:"
)

# Site 2: the global-only gate. Resolve an effective hybrid flag + bm25 weight
# (per-request overrides global) and gate on the effective flag.
SITE2_OLD = (
    "    # When request is provided, try hybrid search + reranking if enabled\n"
    "    if request and config.get('rag.enable_hybrid_search'):"
)
SITE2_NEW = (
    "    # When request is provided, try hybrid search + reranking if enabled.\n"
    "    # A per-request `hybrid` flag overrides the global rag.enable_hybrid_search:\n"
    "    # hybrid=False -> pure vector; hybrid=True + hybrid_bm25_weight=1.0 -> pure\n"
    "    # FTS (lexical); omitted -> the global. hybrid_bm25_weight overrides the\n"
    "    # global rag.hybrid_bm25_weight when supplied (else the global applies).\n"
    "    _hybrid_enabled = (\n"
    "        hybrid if hybrid is not None else config.get('rag.enable_hybrid_search')\n"
    "    )\n"
    "    _effective_bm25_weight = (\n"
    "        hybrid_bm25_weight if hybrid_bm25_weight is not None\n"
    "        else config.get('rag.hybrid_bm25_weight')\n"
    "    )\n"
    "    if request and _hybrid_enabled:"
)

# Site 3: thread the resolved bm25 weight into the hybrid-search call.
SITE3_OLD = "                hybrid_bm25_weight=config.get('rag.hybrid_bm25_weight'),"
SITE3_NEW = "                hybrid_bm25_weight=_effective_bm25_weight,"

# Site 4 (routers/retrieval.py): the /query/collection handler else-branch -- pass
# the per-request flag + weight into query_collection (vector mode lands here).
SITE4_OLD = (
    "        else:\n"
    "            return await query_collection(\n"
    "                request,\n"
    "                collection_names=form_data.collection_names,\n"
    "                queries=[form_data.query],\n"
    "                embedding_function=lambda query, prefix: request.app.state.EMBEDDING_FUNCTION(\n"
    "                    query, prefix=prefix, user=user\n"
    "                ),\n"
    "                k=form_data.k if form_data.k else config.TOP_K,\n"
    "            )"
)
SITE4_NEW = (
    "        else:\n"
    "            return await query_collection(\n"
    "                request,\n"
    "                collection_names=form_data.collection_names,\n"
    "                queries=[form_data.query],\n"
    "                embedding_function=lambda query, prefix: request.app.state.EMBEDDING_FUNCTION(\n"
    "                    query, prefix=prefix, user=user\n"
    "                ),\n"
    "                k=form_data.k if form_data.k else config.TOP_K,\n"
    "                hybrid=form_data.hybrid,\n"
    "                hybrid_bm25_weight=form_data.hybrid_bm25_weight,\n"
    "            )"
)


def apply(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        print(f"FAIL {label}: expected exactly 1 occurrence of anchor, found {n}", file=sys.stderr)
        print(f"     anchor: {old!r}", file=sys.stderr)
        sys.exit(1)
    return text.replace(old, new)


def main() -> None:
    for p in (UTILS, RETRIEVAL):
        if not p.exists():
            print(f"FAIL target not found: {p}", file=sys.stderr)
            sys.exit(1)
    text = UTILS.read_text()
    text = apply(text, SITE1_OLD, SITE1_NEW, "site1 (query_collection signature)")
    text = apply(text, SITE2_OLD, SITE2_NEW, "site2 (hybrid gate)")
    text = apply(text, SITE3_OLD, SITE3_NEW, "site3 (hybrid_bm25_weight arg)")
    UTILS.write_text(text)

    text = RETRIEVAL.read_text()
    text = apply(text, SITE4_OLD, SITE4_NEW, "site4 (handler else-branch)")
    RETRIEVAL.write_text(text)

    print(f"OK per-request hybrid-mode patch applied ({UTILS.name}: 3 sites, {RETRIEVAL.name}: 1 site)")


if __name__ == "__main__":
    main()