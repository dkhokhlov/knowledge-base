#!/usr/bin/env python3
"""Apply the orphan-vector-cleanup-on-delete patch to OWUI files.py (build-time).

Why: OWUI inserts a file's KB-collection vectors during `process_file` BEFORE
the KB-link step (`add_file_to_knowledge`). If the link step fails (sqlite
"database is locked" under contention, or a transport error), the file is
marked `failed` but its vectors are already committed in the collection — an
orphan-in-waiting. The file has no `knowledge_file` membership row, so it is
NOT a KB member.

The `DELETE /api/v1/files/{id}` route (`delete_file_by_id`) cleans a file's
KB-collection vectors, but ONLY for KBs the file is a member of
(`Knowledges.get_knowledges_by_file_id`). A failed-link orphan is not a
member -> that loop is empty -> its vectors are never removed. The route then
deletes the FileModel + blob, leaving the vectors behind, keyed by a
now-deleted `file_id`.

On the next sync re-trigger, oikb deletes the failed file (this route) and
re-uploads. The re-upload extracts, recomputes the path-aware dedup hash
(apply_path_hash.py), and finds the orphan's vectors already in the
collection by `hash` -> `DUPLICATE_CONTENT` -> the file fails forever. This is
the root cause of the deterministic "Duplicate content detected" stuck-failures.

Fix: after the member-loop vector cleanup, ALSO clean the collection named by
the upload metadata `file.meta['data']['knowledge_id']` (the KB the file was
uploaded to), deleting by `file_id` (and by `hash` when present). This catches
the failed-link orphan's vectors that the member loop skipped. It is a no-op
when the file was a member of that KB (the member loop already cleaned it;
guarded by `_kid not in {k.id for k in knowledges}`).

`file.meta` is a dict (the FileModel parses it; the path-hash patch already
relies on `(file.meta or {}).get("data")`). `ASYNC_VECTOR_DB_CLIENT`,
`file.hash`, `id`, `knowledges`, and `log` are all in scope in the route. No new
imports, no other file touched. The pure-row model method `Files.delete_file_by_id`
is unchanged (it never cleaned vectors; its only direct caller is the upload-
idempotency reclaim of stale, vector-less orphans).

This script does one targeted insertion, asserting the anchor occurs exactly
once (fail loud on drift). See open-webui/PATCH.md (Patch 4).

Override the target file for local testing:
  OWUI_FILES_PY=/tmp/files.py python3 apply_vector_cleanup_on_delete.py
"""
import os
import pathlib
import sys

PATH = pathlib.Path(os.environ.get("OWUI_FILES_PY", "/app/backend/open_webui/routers/files.py"))

# The member-loop cleanup `except` tail + the blank line + the `result =` line.
# Insert the orphan-cleanup block in the blank line before `result =`.
ANCHOR_OLD = (
    "            except Exception as e:\n"
    "                log.debug(f'KB embedding cleanup for {knowledge.id}: {e}')\n"
    "\n"
    "        result = await Files.delete_file_by_id(id, db=db)\n"
)

ANCHOR_NEW = (
    "            except Exception as e:\n"
    "                log.debug(f'KB embedding cleanup for {knowledge.id}: {e}')\n"
    "\n"
    "        # Clean orphan vectors: a file that failed the KB-link step keeps\n"
    "        # its embeddings in the collection it was uploaded to (embed runs\n"
    "        # before link) but has no knowledge_file membership row, so the\n"
    "        # member loop above skipped it. Derive the target collection from\n"
    "        # the upload metadata (meta.data.knowledge_id) and delete by file_id;\n"
    "        # also by hash when present. No-op if the file was a member of that\n"
    "        # KB (the member loop already cleaned it).\n"
    "        _kid = ((file.meta or {}).get('data') or {}).get('knowledge_id')\n"
    "        if _kid and _kid not in {k.id for k in knowledges}:\n"
    "            try:\n"
    "                await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=_kid, filter={'file_id': id})\n"
    "                if file.hash:\n"
    "                    await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=_kid, filter={'hash': file.hash})\n"
    "            except Exception as e:\n"
    "                log.debug(f'Orphan KB embedding cleanup for {_kid}: {e}')\n"
    "\n"
    "        result = await Files.delete_file_by_id(id, db=db)\n"
)


def main() -> None:
    if not PATH.exists():
        print(f"FAIL target not found: {PATH}", file=sys.stderr)
        sys.exit(1)
    text = PATH.read_text()
    n = text.count(ANCHOR_OLD)
    if n != 1:
        print(f"FAIL: expected exactly 1 occurrence of anchor, found {n}", file=sys.stderr)
        print(f"     anchor first line: {ANCHOR_OLD.splitlines()[0]!r}", file=sys.stderr)
        sys.exit(1)
    text = text.replace(ANCHOR_OLD, ANCHOR_NEW)
    PATH.write_text(text)
    print(f"OK orphan-vector-cleanup-on-delete patch applied to {PATH} (1 site)")


if __name__ == "__main__":
    main()