#!/usr/bin/env python3
"""Apply the path-aware dedup-hash patch to OWUI retrieval.py (build-time).

Why: OWUI dedups a KB by the SHA-256 of the EXTRACTED TEXT only
(retrieval.py: `hash = calculate_sha256_string(text_content)`). That hash
ignores path and filename, so two files with the same content at different
source paths collide; one is rejected as DUPLICATE_CONTENT and re-uploaded
every sync cycle. The oikb gdrive-indexer then churns those files forever and
grows disk without bound.

Fix: include the file's KB directory UUID + filename in the dedup hash. Two
files with the same content at different paths now get different hashes, so
both index. Same path + name + content stays idempotent (same hash).

directory_id is read from `file.meta['data']['directory_id']` (set by the
upload handler from oikb's metadata). `file.data` is overwritten with
`{'content': text_content}` before the hash line, but `file.meta` is not, so
the directory_id survives to hash time. Default (no directory_id, e.g. non-KB
uploads) -> hash becomes sha256("/" + filename + "\\n" + text) — filename-aware,
a safe improvement over pure-text; no caller breaks.

This script patches the two dedup-hash sites:
  * process_file single-file hash (retrieval.py:~1970)
  * process_files_batch hash        (retrieval.py:~3080)
Both have `file` in scope (FileModel with .meta and .filename). String
concatenation (not an f-string) is used in the replacement to avoid nested
quote escaping; the formula is identical at both sites.

Fails loud (exit 1) if either anchor is not found exactly once, so a base
image bump that drifts an anchor cannot silently pass — the build breaks and
forces a re-review (see open-webui/PATCH.md).

Override the target file for local testing:
  OWUI_RETRIEVAL_PY=/tmp/retrieval.py python3 apply_path_hash.py
"""
import os
import pathlib
import sys

PATH = pathlib.Path(os.environ.get("OWUI_RETRIEVAL_PY", "/app/backend/open_webui/routers/retrieval.py"))

# Site 1: process_file single-file hash. 12-space indent, `hash = ` form.
# Introduces a _dir_id local, then a path-aware hash.
SITE1_OLD = '            hash = calculate_sha256_string(text_content)'
SITE1_NEW = (
    '            _dir_id = ((file.meta or {}).get("data") or {}).get("directory_id") or ""\n'
    '            hash = calculate_sha256_string(_dir_id + "/" + (file.filename or "") + "\\n" + text_content)'
)

# Site 2: process_files_batch hash. 20-space indent, `hash=` kwarg form, trailing comma.
# Inlines the directory_id lookup (no local var in the batch loop body).
SITE2_OLD = '                    hash=calculate_sha256_string(text_content),'
SITE2_NEW = (
    '                    hash=calculate_sha256_string((((file.meta or {}).get("data") or {}).get("directory_id") or "") + "/" + (file.filename or "") + "\\n" + text_content),'
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
    text = apply(text, SITE1_OLD, SITE1_NEW, "site1 (process_file hash)")
    text = apply(text, SITE2_OLD, SITE2_NEW, "site2 (process_files_batch hash)")
    PATH.write_text(text)
    print(f"OK path-aware dedup hash patch applied to {PATH} (2 sites)")


if __name__ == "__main__":
    main()