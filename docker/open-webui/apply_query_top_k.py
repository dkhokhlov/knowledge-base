#!/usr/bin/env python3
"""Apply the top-k preservation patch (patch 8) to OWUI.

Why: the reranker candidate cap `k_reranker` is set to the GLOBAL
TOP_K_RERANKER (default 3) regardless of the request `k`. A request with k=10
gets its hybrid/lexical result truncated to 3. The api-gateway /retrieve route
honors a per-request k up to KB_RETRIEVE_K_MAX, so the cap must never fall
below the request k.

Fix: `k_reranker = max(k, global)` -- the reranker never truncates below the
requested k. The global still raises the cap when it is larger (e.g. 50). With
patch 9 (skip the cosine reranker when no real reranker is configured) the cap
is moot in the no-reranker case, but the max() keeps the contract correct when a
real reranker IS configured.

Two sites:
  * retrieval/utils.py query_collection hybrid call: `k_reranker=config.get(...)`
    (1 occurrence).
  * routers/retrieval.py: `k_reranker=form_data.k_reranker or config.TOP_K_RERANKER`
    (2 occurrences -- the single-doc /query handler and the /query/collection
    handler; both get the same max() fix).

Anchors come from the RUNNING image (kb-openwebui), not the upstream clone.

Fails loud (exit 1) if the utils anchor is not found exactly once, or the
retrieval anchor is not found exactly twice. Override target files for local
testing:
  OWUI_UTILS_PY=/tmp/utils.py OWUI_RETRIEVAL_PY=/tmp/retrieval.py python3 apply_query_top_k.py
"""
import os
import pathlib
import sys

UTILS = pathlib.Path(os.environ.get("OWUI_UTILS_PY", "/app/backend/open_webui/retrieval/utils.py"))
RETRIEVAL = pathlib.Path(os.environ.get("OWUI_RETRIEVAL_PY", "/app/backend/open_webui/routers/retrieval.py"))

# Site 1 (utils.py): the hybrid-search call's k_reranker. `or 0` guards a None
# config value (max() raises TypeError on None). Normal config is an int.
SITE1_OLD = "                k_reranker=config.get('rag.top_k_reranker'),"
SITE1_NEW = "                k_reranker=max(k, config.get('rag.top_k_reranker') or 0),"

# Site 2 (retrieval.py): both the single-doc /query and the /query/collection
# handlers. Identical string, exactly 2 occurrences.
SITE2_OLD = "                k_reranker=form_data.k_reranker or config.TOP_K_RERANKER,"
SITE2_NEW = (
    "                k_reranker=max(form_data.k if form_data.k else config.TOP_K, "
    "form_data.k_reranker or config.TOP_K_RERANKER),"
)
SITE2_EXPECTED = 2


def apply_one(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        print(f"FAIL {label}: expected exactly 1 occurrence of anchor, found {n}", file=sys.stderr)
        print(f"     anchor: {old!r}", file=sys.stderr)
        sys.exit(1)
    return text.replace(old, new)


def apply_n(text: str, old: str, new: str, expected: int, label: str) -> str:
    n = text.count(old)
    if n != expected:
        print(f"FAIL {label}: expected exactly {expected} occurrences of anchor, found {n}", file=sys.stderr)
        print(f"     anchor: {old!r}", file=sys.stderr)
        sys.exit(1)
    return text.replace(old, new)


def main() -> None:
    for p in (UTILS, RETRIEVAL):
        if not p.exists():
            print(f"FAIL target not found: {p}", file=sys.stderr)
            sys.exit(1)
    text = UTILS.read_text()
    text = apply_one(text, SITE1_OLD, SITE1_NEW, "site1 (utils k_reranker)")
    UTILS.write_text(text)

    text = RETRIEVAL.read_text()
    text = apply_n(text, SITE2_OLD, SITE2_NEW, SITE2_EXPECTED, "site2 (retrieval k_reranker x2)")
    RETRIEVAL.write_text(text)

    print(f"OK top-k preservation patch applied ({UTILS.name}: 1 site, {RETRIEVAL.name}: 2 sites)")


if __name__ == "__main__":
    main()