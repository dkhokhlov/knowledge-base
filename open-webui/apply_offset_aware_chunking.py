#!/usr/bin/env python3
"""Apply the offset-aware chunking patch to OWUI retrieval.py (build-time).

Why: every chunk in Chroma has start_index = 0, so a retrieved chunk cannot be
located inside the full document text served by GET /files/{id}/data/content
(the chunk is not "sliceable by offset"). Goal:
  base_text[start_index : start_index + len(chunk_text)] == chunk_text
where base_text is the text /data/content serves, so base_text[si-W : si+W]
gives surrounding context.

Two prior approaches failed (verified):
  1. In-save_docs rebase (`full_text = " ".join(docs)` + `find` per chunk): the
     KB collection is embedded in Phase 2 (process_file elif collection_name),
     where `docs` are already-split chunks queried from file-{id}, not a fresh
     loader run, so the join != /data/content; and MarkdownHeaderTextSplitter
     mutates ("  \n" join) so header chunks are not substrings of the raw text.
     Live verify: 1/12 slice-correct.
  2. Keep MarkdownHeaderTextSplitter and return its concat as the base: MDS
     .strip()s every line (including inside fenced code blocks) and removes all
     blank lines, so /data/content would serve code/tables with indentation
     gone and no paragraph breaks -- degrading the context window that is the
     feature's whole point.

Fix: a span-preserving chunker `split_docs_with_base(page_docs, config)`. It
returns base_text (the verbatim sanitized extracted text, pages joined with a
single space) plus chunks that are verbatim substrings of it. It does NOT use
MarkdownHeaderTextSplitter. It scans page_text for markdown ATX header lines
while tracking ``` /~~~ fence state (_atx_header_spans), records section spans
[start_i, start_{i+1}) that cover the whole page, and feeds each
page_text[start_i:start_{i+1}] slice to RecursiveCharacterTextSplitter
(add_start_index=True). The splitter's section-relative offset `rel` is rebased
to an absolute offset `page_base + start_i + rel`. Chunk metadata is built from
the page doc's metadata only (never the splitter's) -> no Header 1..6 leak.

process_file computes (base_text, chunks) and writes base_text to
file.data['content'] (one write, one hash, both from base_text) BEFORE the
embedding step, then calls save_docs_to_vector_db with split=False (the chunks
are pre-split; save_docs persists them as supplied). This separates document
provenance (process_file owns offsets + /data/content) from vector persistence
(save_docs persists exactly what it is given).

Config gate: the chunker is used when TEXT_SPLITTER in ('', 'character').
Otherwise the caller falls back to the legacy save_docs(split=True) path
(section-relative offsets, not sliceable) -- degrade, never raise, so a config
flip or token mode cannot trigger an indexing outage.

All five save_docs callers use the chunker when the gate holds: process_file,
process_files_batch, process_text, process_web, process_web_search. With every
caller producing non-mutating header-semantic chunks, the mutating
MarkdownHeaderTextSplitter path in save_docs is redundant and is removed (along
with merge_docs_to_target_size + can_merge_chunks, which were nested in it).

Branch B (KB-add, Phase 2) copies Phase-1 chunks verbatim (split=False, no
re-split) so their absolute start_index carries through; /data/content stays
the Phase-1 base. Its file-{id}-empty fallback rechunks file.data['content']
via the span-preserving chunker (base_text == the input, so no desync).

This is the fifth build-time patch on the custom OWUI overlay (after
apply_path_hash.py, apply_upload_idempotency.py, apply_mtime_to_chunks.py,
apply_vector_cleanup_on_delete.py). It replaces the earlier
apply_start_index_rebase.py: the dead `full_text`/`_cursor` rebase is not
re-injected, so save_docs_to_vector_db reverts to pristine 0.11.0 and its
`if split:` block becomes the legacy fallback path for unsupported configs.

Deployment model: greenfield + forced re-index. No version marker, no
mixed-generation handling. Every chunk gets correct offsets after re-index.

Fails loud (exit 1) if an anchor is not found exactly once, so a base image
bump that drifts an anchor cannot silently pass -- the build breaks and forces
a re-review.

Override the target file for local testing:
  OWUI_RETRIEVAL_PY=/tmp/retrieval.py python3 apply_offset_aware_chunking.py
"""
import pathlib
import os
import sys

PATH = pathlib.Path(
    os.environ.get("OWUI_RETRIEVAL_PY", "/app/backend/open_webui/routers/retrieval.py")
)

# The new functions, inserted once, above save_docs_to_vector_db.
NEW_FUNCS = '''_ATX_HEADER_RE = re.compile(r"^#{1,6}(?:[ \\t].*|$)")


def _is_atx_header(line: str) -> bool:
    """True if line is a CommonMark ATX header: 1-6 `#` then a space/tab or EOL.

    `#NoSpace` (no separator) and `#######` (7 hashes) are not headers.
    """
    return bool(_ATX_HEADER_RE.match(line))


def _atx_header_spans(text: str) -> list[tuple[int, int]]:
    """Consecutive character spans [s_i, e_i) covering all of `text`, split at
    markdown ATX header lines that are NOT inside a fenced code block.

    Fence state toggles on a line whose left-stripped content starts with ``` or
    ~~~ (opening) and closes only on the same marker. Header lines inside an
    open fence are ignored (so `# not a header` inside a code block does not
    start a section). Spans cover the whole text (s_0 == 0, e_last == len), so
    the page text is preserved verbatim and chunks are its substrings.
    """
    n = len(text)
    bounds = [0]
    in_fence = False
    fence_marker = None
    i = 0
    while i < n:
        j = text.find("\\n", i)
        if j < 0:
            line = text[i:]
            next_i = n + 1
        else:
            line = text[i:j]
            next_i = j + 1
        # CommonMark: ATX headers and fenced code blocks allow up to 3 leading
        # spaces. A line indented >=4 spaces is an indented code block: it is not
        # a fence opener or closer and not a header, so it never toggles fence
        # state or starts a section (a ``` inside an indented code block is content).
        indent = len(line) - len(line.lstrip(" "))
        if indent > 3:
            i = next_i
            continue
        stripped = line[indent:]
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = True
                fence_marker = stripped[:3]
        elif stripped.startswith(fence_marker):
            in_fence = False
            fence_marker = None
        if not in_fence and _is_atx_header(stripped) and i != 0:
            bounds.append(i)
        i = next_i
    spans = []
    for k in range(len(bounds)):
        s = bounds[k]
        e = bounds[k + 1] if k + 1 < len(bounds) else n
        spans.append((s, e))
    return spans


def _coalesce_spans(spans, config):
    """Merge adjacent spans forward while a span is smaller than the min-size
    target and the combined span fits in `CHUNK_SIZE`.

    Coalescing SPANS (not chunks) keeps each merged span a contiguous substring
    of the page text, so offsets stay exact and nothing is mutated. This
    restores at the caller the min-size mitigation that lived in `save_docs`'s
    removed MDS branch, but span-level + verbatim: a TOC of 50 `### Item N`
    headers coalesces into ~`CHUNK_SIZE`-sized spans instead of 50 ten-char
    chunks. With `CHUNK_MIN_SIZE_TARGET <= 0` this is a no-op (header-strict,
    matching the legacy `=0` behavior).
    """
    min_size = config.CHUNK_MIN_SIZE_TARGET
    max_size = config.CHUNK_SIZE
    if min_size <= 0 or len(spans) <= 1:
        return spans
    merged = []
    cur_s, cur_e = spans[0]
    for s_i, e_i in spans[1:]:
        if (cur_e - cur_s) < min_size and (e_i - cur_s) <= max_size:
            cur_e = e_i
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s_i, e_i
    merged.append((cur_s, cur_e))
    return merged


def split_docs_with_base(page_docs, config):
    """Chunk `page_docs` into Documents whose metadata `start_index` is a
    character offset into the returned `base_text`, so:

        base_text[start_index : start_index + len(chunk.page_content)] == chunk.page_content

    `base_text` is the verbatim sanitized extracted text, pages joined with a
    single space (same shape as today's /data/content, modulo sanitization).
    Sections come from `_atx_header_spans` (fence-aware ATX headers); each section
    slice is split by RecursiveCharacterTextSplitter(add_start_index=True). The
    splitter's section-relative `rel` is rebased to `page_base + start_i + rel`.

    Chunk metadata is built from the page doc's metadata plus `start_index` only
    (never the splitter's Document metadata) so no `Header 1..6` leaks into
    Chroma. `page`/`source`/`created_by`/`file_id`/`name` are preserved when
    present on the page doc.
    """
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.CHUNK_SIZE,
        chunk_overlap=config.CHUNK_OVERLAP,
        add_start_index=True,
    )
    base_parts = []
    page_base = 0
    chunks = []
    for d in page_docs:
        page_text = sanitize_text_for_db(d.page_content)
        spans = _coalesce_spans(_atx_header_spans(page_text), config)
        for s_i, e_i in spans:
            sect = page_text[s_i:e_i]
            if not sect:
                continue
            for c in text_splitter.split_documents(
                [Document(page_content=sect, metadata={**d.metadata})]
            ):
                rel = c.metadata.get("start_index", -1)
                if rel < 0:
                    # Splitter omitted the offset: recover it exactly by locating
                    # the chunk inside the section before falling back to the
                    # section start (keeps the slice invariant for every chunk).
                    rel = sect.find(c.page_content)
                if rel < 0:
                    abs_si = page_base + s_i
                    log.warning(
                        "split_docs_with_base: chunk offset not found; "
                        "using section start %d",
                        abs_si,
                    )
                else:
                    abs_si = page_base + s_i + rel
                chunks.append(
                    Document(
                        page_content=c.page_content,
                        metadata={**d.metadata, "start_index": abs_si},
                    )
                )
        base_parts.append(page_text)
        page_base += len(page_text) + 1  # +1 for the " " page-joiner
    base_text = " ".join(base_parts)
    return base_text, chunks


'''

# Site 1: insert the new functions above save_docs_to_vector_db.
SITE1_OLD = (
    "def save_docs_to_vector_db(\n"
    "    request: Request,\n"
    "    docs,\n"
    "    collection_name,\n"
    "    config: RetrievalConfig,\n"
)
SITE1_NEW = NEW_FUNCS + SITE1_OLD

# Site 2: define the config gate before the branch dispatch in process_file.
SITE2_OLD = (
    "                await _validate_collection_access([collection_name], user, access_type='write')\n"
    "\n"
    "            if form_data.content:\n"
)
SITE2_NEW = (
    "                await _validate_collection_access([collection_name], user, access_type='write')\n"
    "\n"
    "            # Offset-aware chunking produces a sliceable base_text\n"
    "            # (/data/content) + pre-split chunks with character offsets.\n"
    "            # Used with a character text splitter; otherwise save_docs's\n"
    "            # legacy split path runs (split=True, section-relative offsets,\n"
    "            # not sliceable) -- degrade, no outage.\n"
    "            _offset_aware = config.TEXT_SPLITTER in ('', 'character')\n"
    "            _copy_phase1 = False\n"
    "\n"
    "            if form_data.content:\n"
)

# Site 3 was here: a rechunk inside `if form_data.content:` (Branch A). Removed:
# it double-applied split_docs_with_base for Branch A (SITE5's outer rechunk also
# fires when _copy_phase1 is False, which Branch A never sets). SITE5 alone
# applies the transform exactly once for Branch A; the slice invariant held
# against the corrupted base, so the double-apply was silent /data/content
# corruption (duplicated overlap regions + joiners).

# Site 4: Branch B copy sub-branch -- mark that docs are Phase-1 chunks to copy
# verbatim (no rechunk). The fallback sub-branch rechunks via the unified
# transform at site 5.
SITE4_OLD = (
    "                        for idx, id in enumerate(result.ids[0])\n"
    "                    ]\n"
    "                else:\n"
)
SITE4_NEW = (
    "                        for idx, id in enumerate(result.ids[0])\n"
    "                    ]\n"
    "                    _copy_phase1 = True\n"
    "                else:\n"
)

# Site 5: unified transform after the branch chain -- rechunk docs into a
# sliceable base + pre-split chunks unless this is the Phase-1 copy path.
SITE5_OLD = (
    "                text_content = ' '.join([doc.page_content for doc in docs])\n"
    "\n"
    "            log.debug('text_content: %s', text_content)\n"
)
SITE5_NEW = (
    "                text_content = ' '.join([doc.page_content for doc in docs])\n"
    "\n"
    "            if _offset_aware and not _copy_phase1:\n"
    "                text_content, docs = split_docs_with_base(docs, config)\n"
    "\n"
    "            log.debug('text_content: %s', text_content)\n"
)

# Site 6: call site -- pass split=False for the offset path (pre-chunked) and
# the Phase-1 copy path; split=True only for the legacy fallback.
SITE6_OLD = (
    "                        add=(True if form_data.collection_name else False),\n"
    "                        user=user,\n"
)
SITE6_NEW = (
    "                        split=(not _offset_aware) and (not _copy_phase1),\n"
    "                        add=(True if form_data.collection_name else False),\n"
    "                        user=user,\n"
)

# Site 7: process_files_batch -- config gate, per-file rechunk, split flag.
SITE7_OLD = (
    "    config = await get_retrieval_config()\n"
    "    collection_name = form_data.collection_name\n"
)
SITE7_NEW = (
    "    config = await get_retrieval_config()\n"
    "    _offset_aware = config.TEXT_SPLITTER in ('', 'character')\n"
    "    collection_name = form_data.collection_name\n"
)

# Site 8: process_files_batch loop -- build the per-file doc, rechunk if offset-aware.
SITE8_OLD = (
    "            text_content = file.data.get('content', '')\n"
    "            docs: list[Document] = [\n"
    "                Document(\n"
    "                    page_content=text_content.replace('<br/>', '\\n'),\n"
    "                    metadata={\n"
    "                        **file.meta,\n"
    "                        'name': file.filename,\n"
    "                        'created_by': file.user_id,\n"
    "                        'file_id': file.id,\n"
    "                        'source': file.filename,\n"
    "                    },\n"
    "                )\n"
    "            ]\n"
    "\n"
    "            all_docs.extend(docs)\n"
)
SITE8_NEW = (
    "            docs: list[Document] = [\n"
    "                Document(\n"
    "                    page_content=file.data.get('content', '').replace('<br/>', '\\n'),\n"
    "                    metadata={\n"
    "                        **file.meta,\n"
    "                        'name': file.filename,\n"
    "                        'created_by': file.user_id,\n"
    "                        'file_id': file.id,\n"
    "                        'source': file.filename,\n"
    "                    },\n"
    "                )\n"
    "            ]\n"
    "            if _offset_aware:\n"
    "                text_content, docs = split_docs_with_base(docs, config)\n"
    "            else:\n"
    "                text_content = file.data.get('content', '')\n"
    "\n"
    "            all_docs.extend(docs)\n"
)

# Site 9: process_files_batch save_docs call -- pass split flag.
SITE9_OLD = (
    "                config,\n"
    "                add=True,\n"
    "                user=user,\n"
)
SITE9_NEW = (
    "                config,\n"
    "                split=not _offset_aware,\n"
    "                add=True,\n"
    "                user=user,\n"
)


def apply(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        print(
            f"FAIL {label}: expected exactly 1 occurrence of anchor, found {n}",
            file=sys.stderr,
        )
        print(f"     anchor: {old!r}", file=sys.stderr)
        sys.exit(1)
    return text.replace(old, new)


def delete_between(text: str, start: str, end: str, label: str, max_span: int) -> str:
    """Remove the span [start, end): everything from the start anchor up to (not
    including) the end anchor. Both anchors must be present, the start anchor
    must be unique, end must occur after start, and the deleted span must not
    exceed `max_span` chars — so a base bump that reorders code (putting the end
    anchor far past the intended region) fails loud instead of deleting too much
    while the symbol-count asserts still pass."""
    if text.count(start) != 1:
        print(
            f"FAIL {label}: start anchor not unique ({text.count(start)} found)",
            file=sys.stderr,
        )
        sys.exit(1)
    i = text.find(start)
    j = text.find(end, i + len(start))
    if j < 0:
        print(f"FAIL {label}: end anchor not found after start", file=sys.stderr)
        sys.exit(1)
    if j - i > max_span:
        print(
            f"FAIL {label}: deleted span {j - i} chars exceeds cap {max_span} "
            "(end anchor moved past intended region?)",
            file=sys.stderr,
        )
        sys.exit(1)
    return text[:i] + text[j:]


# Site 10: process_text -- rechunk raw text when offset-aware; split flag.
SITE10_OLD = (
    "    config = await get_retrieval_config()\n"
    "    result = await run_in_threadpool(save_docs_to_vector_db, request, docs, collection_name, config, user=user)\n"
)
SITE10_NEW = (
    "    config = await get_retrieval_config()\n"
    "    _offset_aware = config.TEXT_SPLITTER in ('', 'character')\n"
    "    if _offset_aware:\n"
    "        text_content, docs = split_docs_with_base(docs, config)\n"
    "    result = await run_in_threadpool(\n"
    "        save_docs_to_vector_db, request, docs, collection_name, config,\n"
    "        split=not _offset_aware, user=user,\n"
    "    )\n"
)

# Site 11: process_web -- rechunk fetched web content when offset-aware.
SITE11_OLD = (
    "            if not config.BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL:\n"
    "                await run_in_threadpool(\n"
    "                    save_docs_to_vector_db,\n"
    "                    request,\n"
    "                    docs,\n"
    "                    collection_name,\n"
    "                    config,\n"
    "                    overwrite=overwrite,\n"
    "                    add=(not overwrite),\n"
    "                    user=user,\n"
    "                )\n"
)
SITE11_NEW = (
    "            if not config.BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL:\n"
    "                _offset_aware = config.TEXT_SPLITTER in ('', 'character')\n"
    "                if _offset_aware:\n"
    "                    content, docs = split_docs_with_base(docs, config)\n"
    "                await run_in_threadpool(\n"
    "                    save_docs_to_vector_db,\n"
    "                    request,\n"
    "                    docs,\n"
    "                    collection_name,\n"
    "                    config,\n"
    "                    overwrite=overwrite,\n"
    "                    add=(not overwrite),\n"
    "                    split=not _offset_aware,\n"
    "                    user=user,\n"
    "                )\n"
)

# Site 12: process_web_search -- rechunk loaded search results when offset-aware.
SITE12_OLD = (
    "            try:\n"
    "                await run_in_threadpool(\n"
    "                    save_docs_to_vector_db,\n"
    "                    request,\n"
    "                    docs,\n"
    "                    collection_name,\n"
    "                    config,\n"
    "                    overwrite=True,\n"
    "                    user=user,\n"
    "                )\n"
)
SITE12_NEW = (
    "            try:\n"
    "                _offset_aware = config.TEXT_SPLITTER in ('', 'character')\n"
    "                _loaded = len(docs)\n"
    "                if _offset_aware:\n"
    "                    _, docs = split_docs_with_base(docs, config)\n"
    "                await run_in_threadpool(\n"
    "                    save_docs_to_vector_db,\n"
    "                    request,\n"
    "                    docs,\n"
    "                    collection_name,\n"
    "                    config,\n"
    "                    overwrite=True,\n"
    "                    split=not _offset_aware,\n"
    "                    user=user,\n"
    "                )\n"
)

# Site 13: process_web_search response -- report loaded page count, not the
# chunk count (SITE12 rebinds `docs` to chunks). The plural `collection_names`
# key disambiguates this from the BYPASS branch's `collection_name: None` response.
SITE13_OLD = (
    "                'collection_names': [collection_name],\n"
    "                'items': result_items,\n"
    "                'filenames': urls,\n"
    "                'loaded_count': len(docs),\n"
)
SITE13_NEW = (
    "                'collection_names': [collection_name],\n"
    "                'items': result_items,\n"
    "                'filenames': urls,\n"
    "                'loaded_count': _loaded,\n"
)

# Deletion 1: the mutating MarkdownHeaderTextSplitter branch inside `if split:`.
# With all five callers producing non-mutating header-semantic chunks, this
# branch is redundant. Removing [branch start, RCTS-branch start) drops it and
# its inner merge_docs_to_target_size call; the RCTS/token branches stay as the
# legacy fallback for token mode.
DEL1_START = "        if config.ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER:\n"
DEL1_END = "        if config.TEXT_SPLITTER in ['', 'character']:\n"

# Deletion 4: the now-unused MarkdownHeaderTextSplitter import.
DEL4_OLD = (
    "from langchain_text_splitters import (\n"
    "    MarkdownHeaderTextSplitter,\n"
    "    RecursiveCharacterTextSplitter,\n"
)
DEL4_NEW = (
    "from langchain_text_splitters import (\n"
    "    RecursiveCharacterTextSplitter,\n"
)

# Deletion 3 + 2: the orphaned merge helpers. can_merge_chunks was used only
# inside merge_docs_to_target_size, which was called only inside the removed MDS
# branch. Delete can_merge_chunks first (its end anchor is the merge def), then
# the merge def (its end anchor is the next function, get_transformers_tokenizer).
DEL3_START = "def can_merge_chunks(a: Document, b: Document) -> bool:\n"
DEL3_END = "def merge_docs_to_target_size(\n"
DEL2_START = "def merge_docs_to_target_size(\n"
DEL2_END = "def get_transformers_tokenizer(request: Request, config: RetrievalConfig):"


def main() -> None:
    if not PATH.exists():
        print(f"FAIL target not found: {PATH}", file=sys.stderr)
        sys.exit(1)
    text = PATH.read_text()
    text = apply(text, SITE1_OLD, SITE1_NEW, "site 1 (insert split_docs_with_base)")
    text = apply(text, SITE2_OLD, SITE2_NEW, "site 2 (config gate in process_file)")
    text = apply(text, SITE4_OLD, SITE4_NEW, "site 4 (Branch B copy flag)")
    text = apply(text, SITE5_OLD, SITE5_NEW, "site 5 (unified transform)")
    text = apply(text, SITE6_OLD, SITE6_NEW, "site 6 (call site split flag)")
    text = apply(text, SITE7_OLD, SITE7_NEW, "site 7 (batch config gate)")
    text = apply(text, SITE8_OLD, SITE8_NEW, "site 8 (batch loop rechunk)")
    text = apply(text, SITE9_OLD, SITE9_NEW, "site 9 (batch save_docs split flag)")
    text = apply(text, SITE10_OLD, SITE10_NEW, "site 10 (process_text rechunk + split)")
    text = apply(text, SITE11_OLD, SITE11_NEW, "site 11 (process_web rechunk + split)")
    text = apply(text, SITE12_OLD, SITE12_NEW, "site 12 (process_web_search rechunk + split)")
    text = apply(text, SITE13_OLD, SITE13_NEW, "site 13 (process_web_search loaded_count = pages)")
    # MDS-path removal: delete the redundant mutating splitter + its orphans.
    text = apply(text, DEL4_OLD, DEL4_NEW, "del 4 (drop MarkdownHeaderTextSplitter import)")
    text = delete_between(text, DEL1_START, DEL1_END, "del 1 (remove MDS branch)", max_span=2000)
    text = delete_between(text, DEL3_START, DEL3_END, "del 3 (remove can_merge_chunks)", max_span=1000)
    text = delete_between(text, DEL2_START, DEL2_END, "del 2 (remove merge_docs_to_target_size)", max_span=3500)
    # Structural asserts: fail loud if the MDS path is not fully removed or if
    # split_docs_with_base is wired into the wrong number of callers (guards
    # against a double-apply reintroduction). ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER
    # does not match "MarkdownHeaderTextSplitter" (case + underscores differ).
    assert text.count("MarkdownHeaderTextSplitter") == 0, "MDS splitter not fully removed"
    assert text.count("merge_docs_to_target_size") == 0, "merge_docs_to_target_size not removed"
    assert text.count("can_merge_chunks") == 0, "can_merge_chunks not removed"
    assert text.count("split_docs_with_base(docs, config)") == 5, (
        "split_docs_with_base must be called exactly 5 times "
        "(process_file, process_files_batch, process_text, process_web, process_web_search)"
    )
    PATH.write_text(text)
    print(f"OK offset-aware chunking patch applied to {PATH} (12 sites, 4 deletions)")


if __name__ == "__main__":
    main()