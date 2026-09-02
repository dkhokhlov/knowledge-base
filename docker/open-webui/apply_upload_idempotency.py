#!/usr/bin/env python3
"""Apply the upload-idempotency patch to OWUI files.py (build-time).

Why: OWUI's upload handler mints a new uuid + new on-disk blob + new FileModel row
on every POST /files/ (files.py: `id = str(uuid.uuid4())` then
`Storage.upload_file(...)` then `Files.insert_new_file(...)`), with no lookup for an
existing file at the same (knowledge_id, directory_id, filename). The oikb
gdrive-indexer re-uploads failed-to-index files every sync cycle (because sync/diff
builds its known-set from KB members only, so a failed file -- never a member -- is
always reported `added`), so each re-upload creates a NEW storage item. The failed
orphans are never cleaned (sync/cleanup runs only for modified/deleted members), so
disk grows without bound.

Fix: before the normal upload path runs, look for an existing FileModel with the same
logical identity (meta.data.knowledge_id, meta.data.directory_id, filename). If one
exists with the same file_hash (unchanged failed file): reuse it -- return the
existing FileModel without storing a new blob or re-running process_file -- and
reclaim any extra stale copies. If a match has a different file_hash (a failed file
later edited to have content): reclaim the stale orphan and fall through to the
normal new-upload path so the new content extracts + links (self-heal). Genuinely new
files fall through unchanged.

Guarded to oikb KB uploads only: the block runs only when metadata carries BOTH
knowledge_id AND file_hash (oikb always sends both; client.py). Non-KB uploads
(/file/add, STT, etc.) and manual KB uploads without a pre-computed hash skip the
block entirely -> zero behavior change for every other caller.

Identity is (knowledge_id, directory_id, filename), NOT byte hash, so
same-content-different-path files (the path-aware dedup-hash patch's case) have
different identity and still upload as separate members. This patch is orthogonal to
the dedup-hash patch (apply_path_hash.py); both ship in the same overlay image.

Uses only symbols already imported/used in files.py: Files.get_files,
Files.delete_file_by_id, Storage.delete_file, asyncio.to_thread, log. No new model
methods, no other file touched. Reuses Files.get_files (a Python filter) rather than a
JSON query on File.meta -- there is no existing JSON-query on meta in models/files.py
and File.meta is a JSON column on SQLite; an in-memory filter is trivial for this
single-KB stack (caveat: does not scale to a large multi-tenant instance).

Fails loud (exit 1) if the anchor is not found exactly once, so a base image bump that
drifts the anchor cannot silently pass -- the build breaks and forces a re-review
(see open-webui/PATCH.md).

Override the target file for local testing:
  OWUI_FILES_PY=/tmp/files.py python3 apply_upload_idempotency.py
"""
import os
import pathlib
import sys

PATH = pathlib.Path(os.environ.get("OWUI_FILES_PY", "/app/backend/open_webui/routers/files.py"))

# The anchor: the uuid mint + name capture + on-disk filename reassignment, in
# upload_file_handler. Insert the idempotency block between `name = filename` and
# `filename = f'{id}_{filename}'` (after `name` is defined; a reuse early-return then
# discards the already-minted uuid -- harmless). 8-space indent (inside try:).
ANCHOR_OLD = (
    "        id = str(uuid.uuid4())\n"
    "        name = filename\n"
    "        filename = f'{id}_{filename}'\n"
)

ANCHOR_NEW = (
    "        id = str(uuid.uuid4())\n"
    "        name = filename\n"
    "        # --- upload idempotency for KB sync: stop failed-to-index files from\n"
    "        # --- making a new storage item every re-upload. oikb KB uploads only\n"
    "        # --- (metadata carries knowledge_id + file_hash). Reuses the existing\n"
    "        # --- FileModel + blob for the same (knowledge_id, directory_id,\n"
    "        # --- filename); reclaims stale orphan copies. See open-webui/PATCH.md.\n"
    "        _kb_id = file_metadata.get(\"knowledge_id\")\n"
    "        _fhash = file_metadata.get(\"file_hash\")\n"
    "        if _kb_id and _fhash:\n"
    "            _dir_id = file_metadata.get(\"directory_id\")\n"
    "            _existing = None\n"
    "            for _f in await Files.get_files(db=db):\n"
    "                _md = (_f.meta or {}).get(\"data\") or {}\n"
    "                if (\n"
    "                    _f.filename == name\n"
    "                    and _md.get(\"knowledge_id\") == _kb_id\n"
    "                    and _md.get(\"directory_id\") == _dir_id\n"
    "                ):\n"
    "                    if _md.get(\"file_hash\") == _fhash and _existing is None:\n"
    "                        _existing = _f\n"
    "                    else:\n"
    "                        try:\n"
    "                            await Files.delete_file_by_id(_f.id, db=db)\n"
    "                            if _f.path:\n"
    "                                await asyncio.to_thread(Storage.delete_file, _f.path)\n"
    "                        except Exception as _e:\n"
    "                            log.warning(f\"upload-idempotency reclaim failed for {_f.id}: {_e}\")\n"
    "            if _existing is not None:\n"
    "                log.info(f\"upload-idempotency reuse {name} (kb={_kb_id}) -> file {_existing.id}\")\n"
    "                return {\"status\": True, **_existing.model_dump()}\n"
    "        filename = f'{id}_{filename}'\n"
)


def main() -> None:
    if not PATH.exists():
        print(f"FAIL target not found: {PATH}", file=sys.stderr)
        sys.exit(1)
    text = PATH.read_text()
    n = text.count(ANCHOR_OLD)
    if n != 1:
        print(f"FAIL: expected exactly 1 occurrence of anchor, found {n}", file=sys.stderr)
        print(f"     anchor (first line): {ANCHOR_OLD.splitlines()[0]!r}", file=sys.stderr)
        sys.exit(1)
    text = text.replace(ANCHOR_OLD, ANCHOR_NEW)
    PATH.write_text(text)
    print(f"OK upload-idempotency patch applied to {PATH} (1 site)")


if __name__ == "__main__":
    main()