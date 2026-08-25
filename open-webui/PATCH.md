# Open WebUI custom image — path-aware dedup hash + upload idempotency + source mtime

This directory builds a thin-overlay custom Open WebUI image that applies three
build-time patches to the backend. Patches 1–2 target the **2b churn**: the
gdrive-indexer (oikb) re-uploading files every sync cycle. Patch 3 propagates
the source file mtime into chunk metadata so a retrieve hit can report it.

- **Patch 1 — path-aware dedup hash** (`retrieval.py`): OWUI rejected same-content
  files at different paths as `DUPLICATE_CONTENT`; oikb re-uploaded them every cycle.
- **Patch 2 — upload idempotency** (`files.py`): OWUI minted a new uuid + on-disk blob
  + `FileModel` row on every upload; failed-to-index files (never members) were
  re-uploaded every cycle and never cleaned up, so disk grew without bound.

## Problem

OWUI dedups a knowledge base by the SHA-256 of the **extracted text only**
(`retrieval.py`):

```python
hash = calculate_sha256_string(text_content)
```

That hash ignores path and filename. Two source files with the same content
at different paths collide; OWUI rejects the second as `DUPLICATE_CONTENT`
and never links it as a KB member. oikb has no per-file skip memory, so it
re-uploads the rejected file every 30 s sync cycle. Each re-upload creates a
new file record + on-disk blob (the link failure is swallowed and the upload
always returns HTTP 200), so disk grows without bound.

## Fix

Include the file's KB directory UUID + filename in the dedup hash. Two files
with the same content at different paths now get different hashes, so both
index. Same path + name + content stays idempotent (same hash → no re-process).

The directory UUID comes from `file.meta['data']['directory_id']`, which the
upload handler stores from oikb's metadata. (`file.data` is overwritten with
`{'content': text_content}` before the hash line; `file.meta` is not, so the
`directory_id` survives to hash time.) When there is no `directory_id` (non-KB
uploads, STT, `/file/add`), the hash becomes
`sha256("/" + filename + "\n" + text)` — filename-aware, a safe improvement
over pure-text; no caller breaks.

This is a **dedup-policy** fix only. The separate upload-status/lifecycle
defect (HTTP 200 returned before processing/linking succeeds; failed-link
orphans not cleaned for local storage) is **deferred** — see CHANGELOG. The
dedup fix alone stops the duplicate-content churn (the disk-growth driver).

## What the patch changes (2 sites in `retrieval.py`)

`apply_path_hash.py` does two targeted replacements, each asserting the anchor
occurs exactly once (fail loud on drift).

**Site 1 — `process_file` single-file hash (~line 1970):**

```python
# before
            hash = calculate_sha256_string(text_content)
# after
            _dir_id = ((file.meta or {}).get("data") or {}).get("directory_id") or ""
            hash = calculate_sha256_string(_dir_id + "/" + (file.filename or "") + "\n" + text_content)
```

**Site 2 — `process_files_batch` hash (~line 3080), kwarg form:**

```python
# before
                    hash=calculate_sha256_string(text_content),
# after
                    hash=calculate_sha256_string((((file.meta or {}).get("data") or {}).get("directory_id") or "") + "/" + (file.filename or "") + "\n" + text_content),
```

Both sites have `file` (the `FileModel`, with `.meta` and `.filename`) in
scope. The formula is identical at both sites (string concatenation, not an
f-string, to avoid nested-quote escaping).

## What is NOT changed

- `file.meta['file_hash']` — the oikb-supplied SHA-256 of raw bytes used by
  `sync/diff` to decide added/modified/unmodified. Untouched, so sync/diff
  still works and existing unmodified members are never re-uploaded.
- `files.py`, `knowledge.py`, the upload handler, HTTP status codes,
  `_cleanup_local_cache`. No other file is patched.

## Why no KB reset is needed on cutover

`sync/diff` compares oikb's manifest checksum against `file.meta.file_hash`
(the byte hash), not against `file.hash` (the dedup hash this patch changes).
So existing members stay "unmodified" and are never re-uploaded — their old
text-only `file.hash` + vectors remain, no double-up. The previously-rejected
churn files get new path-aware hashes that do not collide with the old
members' text hashes, so they finally link as members. Deploy + observe; no
destructive reset. (Mixed old/new hash scheme is cosmetic; functionally fine.)

## Base image

Pinned by digest (not the rolling `:main` tag) so the patch stays valid
against the exact source it was written for:

```
ghcr.io/open-webui/open-webui:main@sha256:6a773e5c3a246b65cbe74ce942b294292c0e5f81c138f703d111bc162f7d7c3d
```

= Open WebUI 0.11.0 (frontend `package.json` version at pin time). The digest
is the `OPENWEBUI_BASE_DIGEST` build-arg default in `Dockerfile`. The `:main`
tag is provenance only — the `@sha256:` digest binds the build, not the tag.

The `:0.11.0` release tag is a **separate build artifact** (manifest-list
digest `sha256:72c0ba64…`), same 0.11.0 content. This pin keeps the `:main`
build the three patches were validated against; switching to the `:0.11.0`
release digest would change the base artifact and require re-validating all
three apply scripts against its source (see Rebase procedure).

## Rebase procedure (when bumping the base image)

1. Pick the new base digest:
   `docker image inspect ghcr.io/open-webui/open-webui:<tag> --format '{{index .RepoDigests 0}}'`
2. Extract both patched router files:
   `docker create --name x <new-ref> && docker cp x:/app/backend/open_webui/routers/retrieval.py /tmp/r.py && docker cp x:/app/backend/open_webui/routers/files.py /tmp/f.py && docker rm x`
3. Test all three apply scripts against the extracted files:
   `OWUI_RETRIEVAL_PY=/tmp/r.py python3 open-webui/apply_path_hash.py`
   `OWUI_FILES_PY=/tmp/f.py python3 open-webui/apply_upload_idempotency.py`
   `OWUI_RETRIEVAL_PY=/tmp/r.py python3 open-webui/apply_mtime_to_chunks.py`
   - If all print `OK ...` and `python3 -m py_compile /tmp/r.py /tmp/f.py`
     passes → the anchors still match; bump `OPENWEBUI_BASE_DIGEST` in
     `Dockerfile`, rebuild.
   - If any fails (`expected exactly 1 occurrence ...`) → that anchor
     drifted. Re-derive the new anchor text from the extracted file, update the
     `*_OLD`/`*_NEW` (or `ANCHOR_OLD`/`ANCHOR_NEW`) strings in the failed script
     (and the `NEW` strings if the surrounding code changed), re-test until
     `py_compile` passes, then bump the digest and rebuild. Commit the updated
     script + this file.

## Scope note

The hash-formula change is global across all processing paths and all KBs, but
dedup is per-KB (`collection_name = knowledge_id`) and only actively-synced
KBs re-process files. Static KBs never re-upload, so their old-hash vectors
are never queried against a new hash — practical impact on other KBs is nil.

---

# Patch 2 — upload idempotency (`files.py`)

## Problem (patch 2)

Patch 1 fixed `DUPLICATE_CONTENT` (same-content-different-path). A **second,
independent** churn remained: files that fail extraction (`EMPTY_CONTENT` —
genuinely text-empty: image-only PDFs, image-only slides, empty office docs)
are re-uploaded every sync cycle and **never become KB members**. OWUI's
upload handler (`files.py`) mints a **new uuid + new on-disk blob + new
`FileModel` row on every POST**, with no lookup for an existing file at the
same `(knowledge_id, directory_id, filename)`:

```python
id = str(uuid.uuid4())          # new id every upload
...
contents, file_path = await asyncio.to_thread(Storage.upload_file, ...)  # new blob
...
file_item = await Files.insert_new_file(...)  # new row
```

Because failed files are never members, `sync/diff` (which builds its known-set
from members only, `knowledge.py:sync_knowledge_diff`) always reports them
`added`, so oikb re-uploads them every 30 s. The orphan blobs are never
cleaned (`sync/cleanup` runs only for `modified`/`deleted` members), so disk
grew without bound. The dedup-hash patch cannot help: `EMPTY_CONTENT` is
raised at `retrieval.py` **before** the dedup hash is computed.

oikb does **not** generate the file ids — OWUI does. oikb's upload
(`client.py:upload_file`) POSTs `/files/` with `metadata={knowledge_id,
file_hash, directory_id}` only (no id); OWUI mints the id server-side.

## Fix (patch 2)

In `upload_file_handler`, **before** the blob upload, look for an existing
`FileModel` matching the same logical identity
`(meta.data.knowledge_id, meta.data.directory_id, filename)` and act:

- **Same identity + same `file_hash`** (unchanged failed file, re-uploaded
  every cycle): reuse it — return the existing `FileModel` without storing a
  new blob or re-running `process_file` — and reclaim any extra stale copies.
  → One storage item per failed file; no growth; reclaims prior churn orphans.
- **Same identity, different `file_hash`** (a failed file later edited to have
  content): reclaim the stale orphan and fall through to the normal new-upload
  path → new content extracts + links. Self-heal.
- **No match** (genuinely new file): fall through, unchanged.
- **Guard:** the block runs only when metadata carries **both** `knowledge_id`
  and `file_hash` (oikb always sends both). Non-KB uploads (`/file/add`, STT)
  and manual KB uploads without a pre-computed hash skip it entirely → zero
  behavior change for every other caller.

Identity is `(knowledge_id, directory_id, filename)` — **not** byte hash — so
same-content-different-path files (patch 1's case) have **different** identity
and still upload as separate members. Patch 1 and patch 2 are orthogonal.

## What patch 2 changes (1 site in `files.py`)

`apply_upload_idempotency.py` inserts a block in `upload_file_handler` after
`name = filename` and before `filename = f'{id}_{filename}'` (after `name` is
defined; a reuse early-return then discards the already-minted uuid —
harmless). The anchor (the uuid mint + name capture + on-disk filename
reassignment) is asserted exactly once (fail loud on drift).

```python
# before
        id = str(uuid.uuid4())
        name = filename
        filename = f'{id}_{filename}'
# after
        id = str(uuid.uuid4())
        name = filename
        # --- upload idempotency for KB sync ... (see header above) ---
        _kb_id = file_metadata.get("knowledge_id")
        _fhash = file_metadata.get("file_hash")
        if _kb_id and _fhash:
            _dir_id = file_metadata.get("directory_id")
            _existing = None
            for _f in await Files.get_files(db=db):
                _md = (_f.meta or {}).get("data") or {}
                if (_f.filename == name
                    and _md.get("knowledge_id") == _kb_id
                    and _md.get("directory_id") == _dir_id):
                    if _md.get("file_hash") == _fhash and _existing is None:
                        _existing = _f           # same bytes -> reuse
                    else:                         # stale orphan -> reclaim
                        try:
                            await Files.delete_file_by_id(_f.id, db=db)
                            if _f.path:
                                await asyncio.to_thread(Storage.delete_file, _f.path)
                        except Exception as _e:
                            log.warning(...)
            if _existing is not None:
                log.info(...)
                return {"status": True, **_existing.model_dump()}
        filename = f'{id}_{filename}'
```

Reuses only symbols already imported/used in `files.py`: `Files.get_files`,
`Files.delete_file_by_id`, `Storage.delete_file`, `asyncio.to_thread`, `log`.
No new model methods, no other file touched. Orphans have no KB link / no
vectors, so row + blob deletion is sufficient (no vector cleanup needed).

## Why a Python filter, not a JSON query (patch 2)

`File.meta` is a JSON column on SQLite; there is no existing JSON-query on
`meta` anywhere in `models/files.py`. Adding the first nested-key JSON query
is the main risk, so the patch reuses the existing `Files.get_files(db=db)`
and filters in Python by `meta.data.knowledge_id` / `directory_id` /
`filename`. **Caveat:** this loads all `FileModel`s into memory per upload —
trivial for this single-KB stack (hundreds of files, oikb concurrency 4), but
does **not** scale to a large multi-tenant instance. Acceptable for this
custom overlay.

## Compatibility with oikb's flow (patch 2)

- **`added`** (the failed files): oikb uploads with no prior cleanup; the
  idempotency check finds the existing orphan → reuses → no new blob. No race.
- **`modified`:** oikb deletes `stale_file_id` via `sync/cleanup` **first**,
  then uploads → no existing match → normal new upload. No race.
- **`unchanged`:** oikb does not upload → the check never runs → no impact.
- A reused orphan is not re-processed → stays unlinked → `sync/diff` still
  reports it `added` → oikb re-uploads → idempotent return again. Stable loop,
  no new storage item, no waste beyond one cheap HTTP call/cycle.

## What patch 2 does NOT do

- It does **not** stop the re-upload HTTP cycle (oikb still POSTs every cycle);
  it only stops each re-upload from creating a **new** storage item. The
  re-upload becomes a cheap no-op (reuse, no re-extract, no I/O).
- It does **not** re-attempt extraction on reused files (the 9 are genuinely
  text-empty; they will not extract without OCR). If an extraction engine /
  OCR is enabled later, delete the 9 orphans once → the next cycle re-uploads
  + extracts them; do not rely on per-cycle re-attempts.
- Optional follow-up (not applied): patch `sync_knowledge_diff` to index
  orphan `FileModel`s by `meta.data.knowledge_id` so failed files are reported
  `unchanged` and oikb stops re-uploading them entirely. Same JSON-on-SQLite
  concern; deferred — patch 2 already meets the "does not grow" bar.

## No KB reset on cutover (patch 2)

Like patch 1, no destructive step. Existing members are never re-uploaded
(`sync/diff` uses the byte hash). The first sync cycle reclaims the prior
churn orphans (each failed file's extra copies are deleted as oikb re-uploads
it and the idempotency path keeps one). Deploy + observe; revert the image tag
to `0.11.0-pathdedup` (dedup only) or `main` (stock) to undo.

---

# Patch 3 — source-mtime in chunk metadata (`retrieval.py`)

## Problem (patch 3)

The kb-gateway indexes gdrive files (rclone-preserved mtime) and stores each
file's mtime in `File.meta.data.mtime` at upload. OWUI does **not** propagate
custom `File.meta.data` into Chroma chunk metadata by default — the per-branch
doc-metadata dicts spread `**file.meta` (whose `data` is a nested dict that
Chroma's `filter_metadata` drops) or `**filter_metadata(doc.metadata)` (the
per-unit `{page}`), neither of which carries `mtime`. So a retrieve hit could
not tell when the source file was last modified.

## Fix (patch 3)

Inject `mtime` (read from `file.meta['data']['mtime']`) into the `metadata=`
dict that `process_file` passes to `save_docs_to_vector_db`. That function
merges the caller's `metadata` into every chunk
(`{**doc.metadata, **metadata, 'embedding_config': ...}`), so this **single
site covers all `process_file` paths**:

- Phase 1 (no `collection_name`): extract via the external loader → embed into
  `file-{file.id}`.
- Phase 2 (`collection_name=knowledge_id`): copy `file-{file.id}` vectors into
  the KB collection (normal path), **or** rebuild from `file.data['content']`
  (the no-results fallback, which otherwise loses `page` and `mtime`).

All three funnel through this one `save_docs_to_vector_db` call, so `mtime`
lands in every chunk the KB collection holds — including the fallback path
that a Phase-1-only patch would miss.

## What patch 3 changes (1 site in `retrieval.py`)

`apply_mtime_to_chunks.py` does one replacement, asserting the anchor occurs
exactly once (fail loud on drift).

```python
# before (the metadata= dict passed to save_docs_to_vector_db, ~line 2008)
                        metadata={
                            'file_id': file.id,
                            'name': file.filename,
                            'hash': hash,
                        },
# after
                        metadata={
                            'file_id': file.id,
                            'name': file.filename,
                            'hash': hash,
                            'mtime': ((file.meta or {}).get('data') or {}).get('mtime'),
                        },
```

The access pattern `((file.meta or {}).get('data') or {}).get('mtime')` mirrors
the existing dedup patch's `directory_id` read, and is `None`-safe for non-KB
uploads (no `mtime` in `meta.data`) — Chroma's `process_metadata` drops
`None`, so non-KB chunks are unaffected. `file` is in scope here
(`process_file`), and `file.meta` is not mutated before this point (`file.data`
is overwritten with `{'content': ...}`, not `file.meta`).

The anchor `'hash': hash,` is globally unique (count==1): the per-branch
doc-metadata dicts use `'source': file.filename,`, not `'hash': hash,`.

## No KB reset on cutover (patch 3)

Unlike patches 1–2, `mtime` only reaches **new** chunks. Existing chunks were
embedded without `mtime` and keep their old metadata until re-indexed. A full
re-OCR (`make gdrive-index INDEX_ALL=1`) repopulates every chunk with `mtime`
so the field is present on all hits. Without it, hits on pre-patch chunks have
`mtime == None` (the wrapper surfaces `None`, not a missing key).