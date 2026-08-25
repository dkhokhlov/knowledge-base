#!/usr/bin/env python3
"""Apply the source-mtime chunk-metadata patch to OWUI retrieval.py (build-time).

Why: the kb-gateway indexes gdrive files (rclone-preserved mtime) and stores each
file's mtime in File.meta.data.mtime at upload. OWUI does NOT propagate custom
File.meta.data into Chroma chunk metadata by default — the per-branch doc-metadata
dicts spread `**file.meta` (whose `data` is a nested dict that Chroma drops) or
`**filter_metadata(doc.metadata)` (the per-unit {page}), neither of which carries
mtime. So a retrieve hit could not tell when the source file was last modified.

Fix: inject `mtime` (read from file.meta['data']['mtime']) into the `metadata=`
dict that process_file passes to save_docs_to_vector_db. That function merges the
caller's `metadata` into every chunk (`{**doc.metadata, **metadata,
'embedding_config': ...}`), so this single site covers ALL process_file paths:
  * Phase 1 (no collection_name): extract via the external loader -> embed into
    `file-{file.id}`.
  * Phase 2 (collection_name=knowledge_id): copy `file-{file.id}` vectors into the
    KB collection (normal path), OR rebuild from `file.data['content']` (the
    no-results fallback, which otherwise loses page and mtime).
All three funnel through this one save_docs_to_vector_db call, so mtime lands in
every chunk the KB collection holds — including the fallback path that the
Phase-1-only patch would miss. `file` is in scope here (process_file), and
file.meta is not mutated before this point (the dedup-hash patch reads
file.meta['data']['directory_id'] at the hash line just above; file.data is
overwritten, not file.meta).

The access pattern `((file.meta or {}).get('data') or {}).get('mtime')` mirrors
the existing dedup patch's directory_id read, and is None-safe for non-KB uploads
(no mtime in meta.data) — Chroma's process_metadata drops None, so non-KB chunks
are unaffected.

This is the third build-time patch on the custom OWUI overlay (after
apply_path_hash.py and apply_upload_idempotency.py). See open-webui/PATCH.md.

Fails loud (exit 1) if the anchor is not found exactly once, so a base image bump
that drifts the anchor cannot silently pass — the build breaks and forces a
re-review.

Override the target file for local testing:
  OWUI_RETRIEVAL_PY=/tmp/retrieval.py python3 apply_mtime_to_chunks.py
"""
import os
import pathlib
import sys

PATH = pathlib.Path(os.environ.get("OWUI_RETRIEVAL_PY", "/app/backend/open_webui/routers/retrieval.py"))

# The `metadata=` dict passed to save_docs_to_vector_db in process_file (single-file
# path). Unique anchor: `'hash': hash,` occurs only here (the per-branch doc-metadata
# dicts use `'source': file.filename,`, not `'hash': hash,`). 24-space indent on the
# `metadata={`/`},` lines, 28-space on the keys (kwargs of run_in_threadpool).
SITE_OLD = (
    "                        metadata={\n"
    "                            'file_id': file.id,\n"
    "                            'name': file.filename,\n"
    "                            'hash': hash,\n"
    "                        },"
)
SITE_NEW = (
    "                        metadata={\n"
    "                            'file_id': file.id,\n"
    "                            'name': file.filename,\n"
    "                            'hash': hash,\n"
    "                            'mtime': ((file.meta or {}).get('data') or {}).get('mtime'),\n"
    "                        },"
)


def apply(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        print(f"FAIL {label}: expected exactly 1 occurrence of anchor, found {n}", file=sys.stderr)
        print(f"     anchor: {old!r}", file=sys.stderr)
        sys.exit(1)
    return text.replace(old, new)


def main() -> None:
    if not PATH.exists():
        print(f"FAIL target not found: {PATH}", file=sys.stderr)
        sys.exit(1)
    text = PATH.read_text()
    text = apply(text, SITE_OLD, SITE_NEW, "site (save_docs_to_vector_db metadata dict)")
    PATH.write_text(text)
    print(f"OK source-mtime chunk-metadata patch applied to {PATH} (1 site)")


if __name__ == "__main__":
    main()