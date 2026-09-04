# Open WebUI custom image — path-aware dedup hash + upload idempotency + source mtime + orphan-vector cleanup on delete + offset-aware chunking + resilient terminal status + per-request hybrid mode + top-k preservation + skip-cosine-reranker + ParadeDB BM25 FTS arm + lexical-dsl Tantivy DSL

This directory builds a thin-overlay custom Open WebUI image that applies eleven
build-time patches to the backend. Patches 1–2 target the **2b churn**: the
gdrive-indexer (oikb) re-uploading files every sync cycle. Patch 3 propagates
the source file mtime into chunk metadata so a retrieve hit can report it.
Patch 4 makes file-delete clean failed-link orphan vectors from the KB
collection, closing the root cause of the stuck `DUPLICATE_CONTENT` re-triggers.
Patch 5 makes each chunk's `start_index` a character offset into the full
document text (served by `/data/content`) so a retrieved chunk is sliceable by
offset, via a span-preserving chunker that does not mutate content. Patch 6
makes the terminal file-status write (`status='completed'`) resilient so a
transient commit failure cannot leave a file linked-but-stuck-`processing`.
Patches 7–9 fix retrieval ranking: patch 7 honors a per-request `hybrid` +
`hybrid_bm25_weight` over the global (so `mode=vector`/`lexical` work), patch 8
stops the reranker candidate cap truncating below the requested `k`, and patch
9 stops the cosine-reranker fallback re-burying exact FTS/BM25 matches when no
real reranker is configured. Patch 10 replaces the `plainto_tsquery` AND-every-
token FTS arm (multi-term → 0 hits; `ts_rank_cd` = no IDF/length-norm) with
ParadeDB `pg_search` real BM25 (`text ||| :query` tokenized OR + `pdb.score`),
fixing the hybrid collapse-to-vector on multi-term technical queries. Patch 11
adds an opt-in `lexical-dsl` retrieval mode: a sentinel-prefixed query branch
runs `paradedb.parse_with_field` (Tantivy DSL — phrase / `+AND` / `+x -y`
composite-NOT) and re-raises parse errors as HTTP 400 instead of swallowing
them into a full-collection fallback.

- **Patch 1 — path-aware dedup hash** (`retrieval.py`): OWUI rejected same-content
  files at different paths as `DUPLICATE_CONTENT`; oikb re-uploaded them every cycle.
- **Patch 2 — upload idempotency** (`files.py`): OWUI minted a new uuid + on-disk blob
  + `FileModel` row on every upload; failed-to-index files (never members) were
  re-uploaded every cycle and never cleaned up, so disk grew without bound.
- **Patch 4 — orphan-vector cleanup on delete** (`files.py`): the file-delete route
  cleaned KB vectors only for member KBs; a failed-link orphan (vectors inserted
  before the link step, no membership row) left its vectors behind, so the next
  re-upload hit `DUPLICATE_CONTENT` and failed forever. Delete now also cleans the
  collection named by `meta.data.knowledge_id`.
- **Patch 6 — resilient terminal status** (`retrieval.py` + `models/files.py`):
  the terminal `status='completed'` + hash writes run in isolated throwaway
  sessions whose commit failures are silently swallowed (`except: return None`),
  so under concurrent contention a file can end up linked + indexed but stuck at
  `processing` (a state no reconcile retries). The status write now retries with
  fresh sessions and aborts the link on exhaust; the swallow blocks now log.

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

This is a **dedup-policy** fix only. The separate failed-link-orphan defect
(vectors inserted before the link step are not cleaned when the link fails
and the file is deleted) is addressed by **patch 4** (orphan-vector cleanup on
delete). The HTTP-200-before-processing part of the lifecycle defect remains
out of scope. The dedup fix stops the duplicate-content churn; patch 4 stops
the orphan-vector recurrence.

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
build the nine patches were validated against; switching to the `:0.11.0`
release digest would change the base artifact and require re-validating all
nine apply scripts against its source (see Rebase procedure).

## Rebase procedure (when bumping the base image)

1. Pick the new base digest:
   `docker image inspect ghcr.io/open-webui/open-webui:<tag> --format '{{index .RepoDigests 0}}'`
2. Extract the patched files (`routers/retrieval.py`, `routers/files.py`,
   `retrieval/utils.py`, `retrieval/vector/dbs/pgvector.py`, `models/files.py`):
   `docker create --name x <new-ref> && docker cp x:/app/backend/open_webui/routers/retrieval.py /tmp/r.py && docker cp x:/app/backend/open_webui/routers/files.py /tmp/f.py && docker cp x:/app/backend/open_webui/retrieval/utils.py /tmp/u.py && docker cp x:/app/backend/open_webui/retrieval/vector/dbs/pgvector.py /tmp/pg.py && docker cp x:/app/backend/open_webui/models/files.py /tmp/mf.py && docker rm x`
3. Test all ten apply scripts against the extracted files (in build order):
   `OWUI_RETRIEVAL_PY=/tmp/r.py python3 open-webui/apply_path_hash.py`
   `OWUI_FILES_PY=/tmp/f.py python3 open-webui/apply_upload_idempotency.py`
   `OWUI_RETRIEVAL_PY=/tmp/r.py python3 open-webui/apply_mtime_to_chunks.py`
   `OWUI_FILES_PY=/tmp/f.py python3 open-webui/apply_vector_cleanup_on_delete.py`
   `OWUI_RETRIEVAL_PY=/tmp/r.py python3 open-webui/apply_offset_aware_chunking.py`
   `OWUI_RETRIEVAL_PY=/tmp/r.py OWUI_MODELS_FILES_PY=/tmp/mf.py python3 open-webui/apply_terminal_status.py`
   `OWUI_UTILS_PY=/tmp/u.py OWUI_RETRIEVAL_PY=/tmp/r.py python3 open-webui/apply_query_mode.py`
   `OWUI_UTILS_PY=/tmp/u.py OWUI_RETRIEVAL_PY=/tmp/r.py python3 open-webui/apply_query_top_k.py`
   `OWUI_UTILS_PY=/tmp/u.py python3 open-webui/apply_skip_cosine_reranker.py`
   `OWUI_PGVECTOR_PY=/tmp/pg.py python3 open-webui/apply_bm25_search.py`
   - If all print `OK ...` and `python3 -m py_compile /tmp/r.py /tmp/f.py /tmp/u.py /tmp/pg.py /tmp/mf.py`
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

The api-gateway indexes gdrive files (rclone-preserved mtime) and stores each
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

---

# Patch 4 — orphan-vector cleanup on delete (`files.py`)

## Problem (patch 4)

OWUI inserts a file's KB-collection vectors during `process_file` **before**
the KB-link step (`add_file_to_knowledge`). The per-upload pipeline is:

```
extract -> embed (insert vectors into collection, with dedup-hash check)
       -> link  (add_file_to_knowledge: write the knowledge_file membership row)
```

If the link step fails (sqlite "database is locked" under parallel contention,
or a transport error), the file is marked `failed` but its vectors are already
committed in the collection. The file has **no `knowledge_file` membership
row** — it is not a KB member.

The `DELETE /api/v1/files/{id}` route (`delete_file_by_id`) cleans a file's
KB-collection vectors, but only for KBs the file is a **member** of
(`Knowledges.get_knowledges_by_file_id`). A failed-link orphan is not a member,
so that loop is empty and its vectors are never removed. The route then
deletes the FileModel + blob, leaving the vectors behind, keyed by a
now-deleted `file_id`.

On the next sync re-trigger, oikb deletes the failed file (this route) and
re-uploads. The re-upload extracts, recomputes the path-aware dedup hash
(patch 1), and finds the orphan's vectors already in the collection by `hash`
(`filter={'hash': metadata['hash']}` at `retrieval.py` ~1653) ->
`DUPLICATE_CONTENT` -> the file fails **forever**. This is the root cause of
the deterministic "Duplicate content detected" stuck-failures: the orphan
vectors block every re-upload of the same logical file.

Patch 2's reclaim path does **not** cause this: it calls the pure-row
`Files.delete_file_by_id` model method only on **stale** orphans (same logical
file, different `file_hash`); same-`file_hash` failed-link orphans are
**reused** (returned as `_existing`), not deleted, so no vectors are orphaned
by that path. (Patch 2's doc note "Orphans have no KB link / no vectors" was
correct for the extraction-failure orphans it targeted, but not for the
failed-link orphans patch 4 addresses.)

Chroma is not transactional across stores: `webui.db` (FileModel + link),
`chroma.sqlite3` (embedding metadata), and the per-collection HNSW binary
(vectors) are three independent stores with no distributed transaction. No
rollback reaches the vectors when the link in `webui.db` fails. The only safe
prevention is to make the destructive path (file-delete) clean its vectors
actively — which is what this patch does.

## Fix (patch 4)

After the member-loop vector cleanup in the `DELETE /api/v1/files/{id}` route,
also clean the collection named by the upload metadata
`file.meta['data']['knowledge_id']` (the KB the file was uploaded to), deleting
by `file_id` (and by `hash` when present). This catches the failed-link
orphan's vectors that the member loop skipped.

`file.meta['data']['knowledge_id']` is set by the upload handler from oikb's
metadata (verified present on every gdrive-uploaded file). The block is
guarded by `_kid not in {k.id for k in knowledges}` so it is a no-op when the
file was a member of that KB (the member loop already cleaned it).

## What patch 4 changes (1 site in `files.py`)

`apply_vector_cleanup_on_delete.py` inserts one block in the
`delete_file_by_id` route, between the member-loop `except` tail and the
`result = await Files.delete_file_by_id(id, db=db)` line (8-space indent).

```python
# before
            except Exception as e:
                log.debug(f'KB embedding cleanup for {knowledge.id}: {e}')

        result = await Files.delete_file_by_id(id, db=db)
```

```python
# after
            except Exception as e:
                log.debug(f'KB embedding cleanup for {knowledge.id}: {e}')

        # Clean orphan vectors: a file that failed the KB-link step keeps
        # its embeddings in the collection it was uploaded to (embed runs
        # before link) but has no knowledge_file membership row, so the
        # member loop above skipped it. Derive the target collection from
        # the upload metadata (meta.data.knowledge_id) and delete by file_id;
        # also by hash when present. No-op if the file was a member of that
        # KB (the member loop already cleaned it).
        _kid = ((file.meta or {}).get('data') or {}).get('knowledge_id')
        if _kid and _kid not in {k.id for k in knowledges}:
            try:
                await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=_kid, filter={'file_id': id})
                if file.hash:
                    await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=_kid, filter={'hash': file.hash})
            except Exception as e:
                log.debug(f'Orphan KB embedding cleanup for {_kid}: {e}')

        result = await Files.delete_file_by_id(id, db=db)
```

The anchor (the `except` tail + blank line + `result =` line) asserts exactly
once. `ASYNC_VECTOR_DB_CLIENT`, `file` (the FileModel, with `.meta` and
`.hash`), `id`, `knowledges`, and `log` are all in scope in the route. No new
imports, no other file touched.

## What patch 4 does NOT do

- It does not change the pure-row `Files.delete_file_by_id` model method (it
  never cleaned vectors; patch 2's reclaim relies on that for vector-less
  stale orphans).
- It does not add a dedup-on-collision "reuse" path (an alternative, more
  efficient fix that avoids re-extraction on retry). Reuse was rejected: it
  changes OWUI dedup semantics globally and needs fragile re-link surgery.
  Cleaning on delete is the simpler, lower-risk cure. See CHANGELOG.
- It does not purge pre-existing legacy orphan vectors. The one-time purge of
  the 7224 legacy orphans was done out-of-band via the Chroma
  `collection.delete(ids=...)` API before this patch shipped. After this
  patch, failed-link re-triggers no longer create new orphans.

## No KB reset on cutover (patch 4)

No re-index needed. The patch only changes the file-**delete** path; existing
vectors and memberships are untouched. It takes effect on the next
`docker compose build openwebui` + restart, and prevents **future** orphan
accumulation. Legacy orphans (if any remain) are unaffected and require the
one-time API purge, not a re-index.

---

# Patch 5 — offset-aware chunking (`retrieval.py`)

## Problem (patch 5)

Every chunk in Chroma has `start_index = 0`, so a retrieved chunk cannot be
located inside the full document text served by
`GET /api/v1/files/{id}/data/content` — the chunk is not "sliceable by offset".
Goal:

  base_text[start_index : start_index + len(chunk_text)] == chunk_text

so `base_text[start_index - W : start_index + W]` gives surrounding context.

Root cause:

1. `process_file` joins the per-page loader docs into `text_content` and stores
   it as `file.data['content']` (served by `/data/content`), but `save_docs`'s
   split pipeline runs on the per-page `docs` and `MarkdownHeaderTextSplitter`
   rebuilds each section as a fresh `Document(metadata={**doc.metadata})` —
   discarding any offset.
2. `RecursiveCharacterTextSplitter(add_start_index=True)` then computes
   `start_index` section-relative (sections are shorter than `CHUNK_SIZE`, so
   each section yields one chunk at offset 0).
3. The KB collection users query is embedded in a **second phase**
   (`process_file` elif `collection_name`): the KB-add path queries the
   `file-{id}` collection for already-split chunks (text + metadata, no vectors)
   and re-splits + re-embeds them. So a join of the Phase-2 chunks is not the
   text `/data/content` serves.

## Two approaches that failed (verified)

1. **In-`save_docs` rebase** (`full_text = " ".join(docs)` + `str.find` per
   chunk): the Phase-2 `docs` are already-split chunks, so the join !=
   `/data/content`; and `MarkdownHeaderTextSplitter` mutates (joins section
   lines with `"  \n"`), so header chunks are not substrings of the raw text.
   Live verify: 1/12 slice-correct.
2. **Keep `MarkdownHeaderTextSplitter` and return its concat as the base**: MDS
   `.strip()`s every line (including inside fenced code blocks) and removes all
   blank lines (verified in-container). The corpus is `markitdown-ocr` fenced
   markdown, so `/data/content` would serve code/tables with indentation gone
   and no paragraph breaks — degrading the context window that is the feature's
   whole point. (MDS does **not** leak `Header 1..6` into Chroma metadata in
   0.11.0 — `save_docs` rebuilds `metadata={**doc.metadata}`, discarding the
   splitter's metadata — but the mutation + non-substring arguments stand alone.)

## Fix (patch 5): a span-preserving chunker

A new module-level function `split_docs_with_base(page_docs, config)` returns
`base_text` (the verbatim sanitized extracted text, pages joined with a single
space) plus chunks that are verbatim substrings of it. It does **not** use
`MarkdownHeaderTextSplitter`:

1. `_atx_header_spans(page_text)` scans line-by-line, toggling a ` ``` `/`~~~` fence
   flag, and records a section boundary at each ATX header line (`^#{1,6}`
   then a space/tab or EOL, outside a fence). CommonMark limits both ATX
   headers and fenced code blocks to ≤3 leading spaces, so lines indented ≥4
   spaces are skipped (an indented code block's ` ``` ` line is content, not a
   fence opener). Spans are consecutive and cover the whole page, so
   `base_text` is the page text verbatim.
2. Each `page_text[start_i:start_{i+1}]` slice is split by
   `RecursiveCharacterTextSplitter(add_start_index=True)`.
3. The splitter's section-relative `rel` is rebased to an absolute offset:
   `abs_si = page_base + start_i + rel`.
4. Chunk metadata is built from the page doc's metadata plus `start_index` only
   (never the splitter's `Document` metadata) — no `Header 1..6` leak;
   `page`/`source`/`created_by`/`file_id`/`name` preserved when present.

`process_file` computes `(base_text, chunks)` and writes `base_text` to
`file.data['content']` (one write, one hash, both from `base_text`) **before**
the embedding step, then calls `save_docs_to_vector_db` with `split=False`
(the chunks are pre-split; `save_docs` persists them as supplied). This
separates document provenance (`process_file` owns offsets + `/data/content`)
from vector persistence (`save_docs` persists exactly what it is given). The
dead `full_text`/`_cursor` rebase is not re-injected, so `save_docs_to_vector_db`
reverts to pristine 0.11.0.

### All five callers use the chunker; the mutating MDS path is removed

The same chunker is wired into **every** `save_docs_to_vector_db` caller
(`process_file`, `process_files_batch`, `process_text`, `process_web`,
`process_web_search`), each gated on `_offset_aware`. With every caller
producing non-mutating header-semantic chunks, the
`MarkdownHeaderTextSplitter` branch in `save_docs`'s `if split:` block is
redundant and is **physically removed** (root fix, not a flag-disable): the
branch, the orphaned `merge_docs_to_target_size` + `can_merge_chunks`
helpers (used only inside that branch), and the `MarkdownHeaderTextSplitter`
import. What stays in `if split:` is the `RecursiveCharacterTextSplitter` /
`TokenTextSplitter` fallback for token mode (the gate's `_offset_aware=False`
path). `ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER` remains as a **harmless no-op
API field** (config plumbing + the retrieval config GET/POST expect it); it
activates nothing because the code is gone — no `.env` or config-POST change
is needed.

### Config gate (degrade, never raise)

The chunker is used when `TEXT_SPLITTER in ('', 'character')`. Otherwise the
caller falls back to the legacy `save_docs(split=True)` path (section-relative
offsets, not sliceable) — no outage. `TokenTextSplitter` chunks are
`decode(encode)` roundtrips, not verbatim substrings, so token mode is
incompatible with sliceability; the gate degrades instead of raising so an
admin config flip cannot trigger an indexing outage + re-fail loop. (The gate
previously also required `CHUNK_MIN_SIZE_TARGET == 0`; that conjunct was
dropped because the chunk-level merge it gated was nested inside the removed
MDS branch. Min-size merging is now done at the caller — see "Span coalescing"
below — so `CHUNK_MIN_SIZE_TARGET` is a live knob again, not a fallback guard.)

### Span coalescing (min-size merge at the caller)

Splitting at every ATX header with no merge produces tiny chunks for
header-heavy docs (a 50-line TOC of `### Item N` → 50 ten-char chunks that crowd
out the `retrieve` top-k budget). The removed MDS branch carried
`merge_docs_to_target_size` as its mitigation; removing it deleted that
mitigation. `_coalesce_spans(spans, config)` restores it at the caller,
span-level + verbatim: merge adjacent spans forward while a span is smaller than
`CHUNK_MIN_SIZE_TARGET` and the combined span fits in `CHUNK_SIZE`. Because
spans are contiguous substrings, a merged span is still a verbatim substring —
offsets stay exact, nothing is mutated. With `CHUNK_MIN_SIZE_TARGET <= 0` this
is a no-op (header-strict, matching the legacy `=0` behavior); setting it > 0
activates the coalesce. This is the "min-size merging reimplemented at the
caller (`split_docs_with_base`), not in `save_docs`" placement the design calls
for — chunking at the caller, storage in `save_docs`.

### Branch handling

- **Branch C** (primary upload, the gdrive path): loader docs → chunker →
  `base_text` + chunks; `save_docs(split=False, add=False, overwrite=False)`
  (keeping the idempotent-reuse early-return; `overwrite=True` would re-embed
  every reuse AND `delete_collection` before embedding → a data-loss window).
- **Branch B** (KB-add, Phase 2): queries `file-{id}` and **copies** the
  Phase-1 chunks verbatim (`split=False`, no re-split) so their absolute
  `start_index` carries through; `/data/content` stays the Phase-1 base. The
  `file-{id}`-empty fallback rechunks `file.data['content']` (span-preserving
  is verbatim → `base_text` == the input → no desync, no write-back).
- **Branch A** (content-update / audio): rechunks `form_data.content` (with
  `<br/>` → `\n`) → `/data/content` serves the `\n` form (fixes a pre-existing
  `<br/>` divergence). `overwrite` stays default `False` (Branch A may target a
  KB `collection_name`; `overwrite=True` would `delete_collection(<kb_id>)` and
  destroy every other file's vectors).
- **`process_files_batch`**: per-file rechunk + one `save_docs(all_chunks,
  split=False, add=True, overwrite=False)`.
- **`process_text`**: rechunks `form_data.content`; the response `content`
  becomes the sanitized `base_text`. No `file-{id}` collection, so
  `start_index` is set but not served via `/data/content` (the collection is
  keyed by a content hash or a KB id).
- **`process_web`**: rechunks the fetched page (`content, docs =
  split_docs_with_base(...)`); the response `file.data.content` becomes
  `base_text`.
- **`process_web_search`**: rechunks the loaded results (`_, docs = ...`,
  discarding `base_text` — not stored); the `web-search-<user>-<hash>`
  collection is ephemeral. `start_index` is set but `base_text` is not served.

## Retrieval semantics change

Chunk text **changes**: Phase-1 chunks become raw section substrings (no MDS
normalization), and Phase-2 KB chunks become the Phase-1 form (today Branch B
re-splits, collapsing `"  \n"` → `"\n"`; `split=False` removes that second
pass). So **KB embeddings change and a full re-embed is required** (not
optional) — covered by the forced re-index. Embedding *quality* is unaffected
(raw-vs-normalized is negligible for `nomic-embed-text`). `/data/content` now
serves the sanitized raw extracted text — indentation, blank lines, and code
fences intact.

Removing the MDS branch also changes the **non-file callers**
(`process_text`, `process_web`, `process_web_search`): their chunks were
MDS-sectioned + mutated (strip / `isprintable` filter / blank-drop); they are
now header-span-sectioned verbatim substrings (non-mutating). Their
collections are transient/regenerated (content-hash, web, or
`web-search-<user>-<hash>`), so this is a chunk-boundary + embedding change on
ephemeral data, not on the durable KB. `make test` retrieval output for these
paths may differ.

## Deployment model: greenfield + forced re-index

Clean upgrade, no backward compatibility, no version marker. Every chunk gets
correct offsets after the re-index; there is no mixed-generation window to
discriminate. `start_index` is consumed (`flatten_chroma` in the `/kb` skill
uses `file_id + start_index` as chunk identity) — it is the feature, not a
write-only field.

## What patch 5 changes (12 sites + 4 deletions in `retrieval.py`)

`apply_offset_aware_chunking.py` does 12 targeted replacements + 4 deletions,
each asserting its anchor occurs exactly once (fail loud on drift). The script
runs last in the build, so the `process_file`/`process_files_batch` anchors
target the post-patch text (after `apply_path_hash`, `apply_mtime_to_chunks`);
the three new caller sites + the four deletions anchor on spans those earlier
patches never touch. After applying, the script asserts structurally:
`MarkdownHeaderTextSplitter` == 0, `merge_docs_to_target_size` == 0,
`can_merge_chunks` == 0, and `split_docs_with_base(docs, config)` called
exactly 5 times.

- Insert `_atx_header_spans` + `_coalesce_spans` + `split_docs_with_base` above
  `save_docs_to_vector_db`.
- `process_file`: config gate (`_offset_aware`, `_copy_phase1`) before the branch
  dispatch; Branch B copy flag; unified transform after the branch chain;
  `split` flag at the call site.
- `process_files_batch`: config gate; per-file rechunk; `split` flag.
- `process_text` / `process_web` / `process_web_search`: each gets the config
  gate + a rechunk + `split=not _offset_aware` at its `save_docs` call.
- **Deletions**: the `MarkdownHeaderTextSplitter` branch in `if split:`; the
  `merge_docs_to_target_size` + `can_merge_chunks` defs; the
  `MarkdownHeaderTextSplitter` import line.

## Re-index needed on cutover (patch 5)

`start_index` only reaches **new** chunks. The forced-rechunk mechanism is
`reindex_all` (drains the KB, then re-uploads every file fresh, bypassing the
idem skip and re-running the loader → chunker → Branch B copy):

- Trigger: `make gdrive-index INDEX_ALL=1` (Makefile maps `INDEX_ALL=1` →
  `&reindex_all=1`; walks the local `./gdrive` mirror, no Drive re-pull).
- Run during a maintenance window: the KB is incomplete during the drain +
  extract + embed.
- Poll `make gdrive-status` until pending + processing == 0. Re-run
  `make gdrive-index` (self-heals FAILED files) until `failed == 0`.

New uploads after the patch get correct offsets automatically; project KBs are
out of scope (not re-indexed here).

## markitdown-ocr compatibility

OCR is synchronous and serialized (`threading._lock`, `temperature=0` /
`seed=0`); the chunker and `/data/content` derive from the same per-page
`page_content` in one `process_file` call, so they stay aligned per-index. OCR
is non-deterministic across runs, so re-indexing regenerates different text and
offsets — expected; `/data/content` and chunks regenerate together.

## Risks / edge cases (patch 5)

- **KB chunk text + embeddings change**: re-embed required (forced re-index);
  embedding quality unchanged.
- **Non-file caller chunking change**: `process_text` / `process_web` /
  `process_web_search` chunks change from MDS-sectioned+mutated to
  header-span-sectioned verbatim. Their collections are ephemeral, so this is a
  transient-regeneration change, not durable KB data loss. `make test` may
  reflect it.
- **`/data/content` content change**: now sanitized raw extracted text (was raw
  unsanitized). Differs only by null/surrogate removal — cleaner.
- **Token-mode `TEXT_SPLITTER`**: incompatible with sliceability
  (`TokenTextSplitter` chunks are `decode(encode)`, not substrings); the gate
  falls back to the legacy `split=True` path (not sliceable).
- **`CHUNK_MIN_SIZE_TARGET` reactivated**: it now gates `_coalesce_spans`
  (span-level min-size merge at the caller). At `=0` (current live value) the
  chunker is header-strict and tiny chunks on header-heavy docs (TOC, changelog)
  remain — a pre-existing pathology, not a regression (legacy `=0` did not merge
  either). Setting it > 0 activates coalescing and fixes it; that is a config-rag
  change (persisted in `webui.db`, not just `.env`) + a re-embed.
- **`ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER` is a no-op**: the flag stays in the
  config API (plumbing + GET/POST expect it) but activates nothing. Setting it
  has no effect.
- **Double-apply guard**: the chunker is non-idempotent — applying it to its own
  chunk output corrupts `base_text` (duplicated overlap regions + joiners) while
  the slice invariant still holds against the corrupted base. Each caller
  applies it exactly once; the build asserts `split_docs_with_base(docs,
  config)` is called exactly 5 times, and `test_transform_is_not_idempotent`
  pins the failure mode.
- **`save_docs` early-return desync** (Branch C, `file-{id}` exists):
  pre-existing, benign (idem prevents reuse reaching here; re-index deletes
  `file-{id}` first). Not made worse — `overwrite=False` kept.
- **`page` absent in production**: the external loader returns one
  `Document` per file (no `page` metadata across the live `file-*` collections).
  The per-page loop is correct but dead code in prod; multi-page is covered by
  unit fixtures only.
- **`_atx_header_spans` correctness**: the one new piece of logic; covered by
  fence-aware + multi-section fixtures and a fuzz loop in
  `tests/test_offset_aware_chunking.py`.

---

# Patch 6 — resilient terminal file status (`retrieval.py` + `models/files.py`)

## Problem (patch 6)

A knowledge-bearing file upload (`_process_handler` in `files.py`) runs the
terminal DB writes for a file in **isolated throwaway sessions**. With
`DATABASE_ENABLE_SESSION_SHARING=False` (the OWUI default),
`get_async_db_context(db)` ignores the passed `db` and opens a **new** session
per call (`internal_db.py`). So every `Files.update_file_*_by_id(...)` is an
independent `select → mutate → commit → close` cycle; there is no shared
identity map and no stale-session overwrite.

The three update helpers (`models/files.py`) each wrap that cycle in:

```python
except Exception:
    return None          # swallows EVERY commit/write failure
```

and their callers in `process_file` (`retrieval.py`) do
`await Files.update_file_data_by_id(...)` and **ignore the `None` return**. A
failed commit (or `file is None` → `AttributeError`) is silently lost.

The write sequence for one knowledge file (`_process_handler`, KB path):

| # | site | writes | fate under contention |
|---|---|---|---|
| 1 | `retrieval.py` content write | `data.content` | commits |
| 2 | `files.py:229` | `data.status='processing'` | commits |
| 3a | `retrieval.py:2045` `save_docs_to_vector_db` | Chroma vectors | commits (Chroma) |
| 4 | `retrieval.py:2066` | `meta.collection_name` | commits |
| 5 | `retrieval.py:2074` | `data.status='completed'` | **swallowed on failure** |
| 6 | `retrieval.py:2079` | `File.hash` | **swallowed on failure** |
| 7 | `files.py:236` `add_file_to_knowledge_by_id` | `knowledge_file` link | commits |

Under a transient concurrent-contention storm (two other files errored in the
same ~2 s window), writes #5 and #6 failed and were swallowed while #1–4 and #7
committed. The file ended up **linked + indexed + with content, but
`status='processing'` and `hash=None`** — an inconsistent state:

- The drain poll (`pending=0 AND processing=0`) blocks forever on the stuck
  `processing` row.
- The gdrive reconcile retries **unlinked** pending/failed/processing files; a
  **linked** `processing` file looks "in progress" forever and is never retried.
- The file IS searchable (vectors + link present), so only its status field lies.

This is flaky, not deterministic: the same corpus passed 151/151 on the prior
gate (no storm). It is not a regression of the chunk knobs (chunking config
was identical across both gates). Root-cause confirmed by reading the overlay
source + querying the e2e `webui.db`.

## Fix (patch 6)

Two files; no commit serialization across files (the gdrive index runs 151
files through a shared Ollama; each file's retry uses its own fresh sessions,
files stay concurrent).

**`retrieval.py` — the terminal block after `if result:` (vectors saved):**

- `collection_name` is written once in its own session (unchanged; not retried —
  a missing tag is non-fatal and the KB link does not depend on it).
- `status='completed'` is persisted with a **bounded retry** (`_TERMINAL_RETRY=3`,
  fresh session per attempt, `_TERMINAL_BACKOFF=0.2 * (attempt+1)`). A transient
  commit failure self-heals. Each failed attempt logs
  `terminal status completed did not persist for <id> (attempt N/3)` (WARNING).
- If every attempt fails, **raise**. The raise propagates to `process_file`'s
  failed-handler (`retrieval.py:2100`, sets `status='failed'` in a fresh
  session) and re-raises; `_process_handler`'s inner `except` re-raises before
  the link at `files.py:236` runs. So the file ends up **unlinked + failed**
  (or `processing` if the failed-write also swallows) — both **retryable by the
  reconcile**, never linked-but-stuck. This is the core invariant fix.
- `File.hash` gets the same bounded retry; on exhaust, **log + continue**
  (`completed` is already durable). A missing hash only causes a one-time
  re-process on the next sync (now robust via the retry above); it does not
  block the drain. Each failed attempt logs
  `file hash did not persist for <id> (attempt N/3)` (WARNING); exhaust logs
  `Failed to persist hash for <id> after 3 attempts ...` (ERROR).

**`models/files.py` — the three `update_file_*_by_id` swallow blocks:**

- Replace the silent `except Exception: return None` with
  `log.exception(...); return None`. **Behavior is unchanged** — still returns
  `None`, so every caller's contract holds; the only effect is that a swallowed
  commit/write failure is now **visible in the log**. This is the traceability
  fix for the root enabler (without it, even with the retry, the underlying
  cause of an exhausted retry stays silent).

## What patch 6 changes (1 site in `retrieval.py` + 3 sites in `models/files.py`)

`apply_terminal_status.py` does four targeted replacements, each asserting its
anchor occurs exactly once (fail loud on drift):

- `retrieval.py`: the `if result:` → `return {...}` terminal block (anchored on
  the `# Fresh session for the final update.` comment + the
  `'collection_name': collection_name` field — unique).
- `models/files.py`: the three swallow blocks, each anchored on its
  method-specific mutation line (`file.hash = hash` / `file.data = {...}` /
  `file.meta = {...}`) so the repeating bare `except Exception: return None`
  resolves to exactly one site each.

The script reads `OWUI_RETRIEVAL_PY` (default
`/app/backend/open_webui/routers/retrieval.py`) and `OWUI_MODELS_FILES_PY`
(default `/app/backend/open_webui/models/files.py`). `asyncio` and `log` are
already in scope in both files; no new imports.

## What patch 6 does NOT do

- **No commit serialization across files.** Files stay concurrent; only the
  terminal writes of one file are retried in sequence.
- **No atomic per-file transaction.** The terminal fields + the junction link
  are still separate commits (the link is in `_process_handler`, a different
  function). An atomic per-file transaction (one session: mutate
  collection_name + status + hash + insert the junction row, commit once) is
  the higher-correctness option, but it needs no-commit variants of the update
  helpers + the junction helper and introduces a Chroma/SQL non-atomicity
  (orphan vectors if the SQL tx fails) — more surface for a flaky transient.
  The narrow fix already prevents the stuck-linked state by aborting the link.
- **`collection_name` is not retried.** It persisted in the incident; the storm
  hit #5/#6. A missing tag is non-fatal (KB retrieval uses the KB collection
  directly); out of scope.
- **The broad `except: return None` contract is preserved.** Only logging is
  added; no caller's control flow changes.
- **The failed-handler's own writes (`retrieval.py:2104`, `files.py:252`) are
  not retried.** If those also swallow under the same storm, the file is left
  unlinked + `processing` — which the reconcile retries, and `make kb-check
  --repair` (see `scripts/kb_check.py`) catches as the safety net.

## No KB reset on cutover (patch 6)

Patch 6 changes write resilience, not chunk content or embeddings. Existing
rows are untouched. A file stuck at `processing` from a pre-patch run is
repaired by `make kb-check --repair` (strong gate: linked + content present +
vectors in the KB collection + stale > 60 s → set `completed` + backfill
`File.hash`), or self-heals on the next `make gdrive-index` (the reconcile
retries unlinked non-completed; a stuck **linked** file needs the repair). No
re-index is required for the patch itself.

## Risks / edge cases (patch 6)

- **Retry adds latency on failure only.** A successful first attempt is
  unchanged (one session, one commit). A transient failure adds up to
  ~0.2+0.4+0.6 = 1.2 s before the retry succeeds or the file routes to
  `failed`. Acceptable for a background task.
- **Abort-before-link leaves orphan vectors.** If the status write exhausts and
  raises, the KB-collection vectors (write #3a) are already committed with no
  junction link — the same failed-link-orphan state patch 4 cleans on delete.
  The reconcile re-triggers the file (unlinked + failed/processing); the
  retry's delete + re-embed cleans the orphans. No new failure mode.
- **`_TERMINAL_RETRY`/`_TERMINAL_BACKOFF` are literals.** Three attempts + a
  0.2 s linear backoff. Not a runtime config; tuning needs an image rebuild.
- **`log.exception` on every failed helper call.** A real failure now emits a
  stack trace per attempt (up to 3 per file for status, 3 for hash). Noisy on a
  genuine outage, silent in normal operation. Correct for an error path.
- **Residual gap.** If the status write exhausts AND the failed-handler's
  `status='failed'` write also swallows, the file is unlinked + `processing`
  (retryable by reconcile + caught by `make kb-check --repair`). Not
  stuck-forever-linked. Quad coverage: retry → abort-before-link → reconcile →
  kb-check repair.

---

# Patch 7 — per-request hybrid-mode override (`retrieval/utils.py` + `routers/retrieval.py`)

## Problem (patch 7)

`query_collection()` (`retrieval/utils.py`) re-reads the **global**
`rag.enable_hybrid_search` and ignores the per-request `hybrid` flag:

```python
if request and config.get('rag.enable_hybrid_search'):
    # -> query_collection_with_hybrid_search(... hybrid_bm25_weight=config.get(...))
```

So `hybrid=False` (pure vector) is a no-op when the global is on — the api-gateway
`/retrieve` `mode=vector` cannot override the global. The handler's else-branch
(non-hybrid) also never passes `hybrid`/`hybrid_bm25_weight` into
`query_collection`, so the per-request value is dropped before it can be honored.

## Fix (patch 7)

1. Add `hybrid: bool|None=None` + `hybrid_bm25_weight: float|None=None` params to
   `query_collection`.
2. Resolve an effective flag + weight (per-request overrides global):
   `hybrid=False` → pure vector; `hybrid=True, weight=1.0` → pure FTS (lexical);
   `hybrid=True, weight omitted` → the global `RAG_HYBRID_BM25_WEIGHT`; `hybrid`
   omitted → the global. Gate on the effective flag.
3. Thread the resolved weight into `query_collection_with_hybrid_search`.
4. Pass `hybrid`/`hybrid_bm25_weight` from the handler else-branch so the
   per-request flag reaches `query_collection` (vector mode lands here).

`QueryCollectionsForm` (`routers/retrieval.py`) already has `hybrid`,
`hybrid_bm25_weight`, `k`, `k_reranker` — no new OWUI field.

## What patch 7 changes (3 sites in `retrieval/utils.py` + 1 in `routers/retrieval.py`)

`apply_query_mode.py` does four targeted replacements, each asserting its anchor
occurs exactly once (fail loud on drift):

- Site 1 — `query_collection` signature: add the two per-request params.
- Site 2 — the global-only gate: replace
  `if request and config.get('rag.enable_hybrid_search'):` with the
  `_hybrid_enabled` / `_effective_bm25_weight` resolution block + an
  `if request and _hybrid_enabled:` gate.
- Site 3 — the hybrid-search call kwarg: replace
  `hybrid_bm25_weight=config.get('rag.hybrid_bm25_weight'),` with
  `hybrid_bm25_weight=_effective_bm25_weight,`.
- Site 4 (`routers/retrieval.py`) — the `/query/collection` handler else-branch:
  add `hybrid=form_data.hybrid,` + `hybrid_bm25_weight=form_data.hybrid_bm25_weight,`
  to the `query_collection(...)` call.

Anchors come from the **running image** `kb-openwebui`, NOT the upstream clone
— the image uses `ThreadPoolExecutor` +
f-string logging; the clone uses `asyncio.gather` + `%s`, and the clone anchors
fail the count check. Test override: `OWUI_UTILS_PY=… OWUI_RETRIEVAL_PY=…`.

## No KB reset on cutover (patch 7)

Patch 7 changes routing logic, not chunk content or embeddings. Existing
vectors are untouched. The per-request override takes effect on the next
`docker compose build openwebui` + restart. `mode=vector` is inert until this
patch ships — land P7/P8/P9 + the gateway `/retrieve` route in the same release.

---

# Patch 8 — top-k preservation (`retrieval/utils.py` + `routers/retrieval.py`)

## Problem (patch 8)

The reranker candidate cap `k_reranker` is set to the **global** `TOP_K_RERANKER`
(default 3) regardless of the request `k`. A request with `k=10` gets its
hybrid/lexical result truncated to 3. The api-gateway `/retrieve` route honors a
per-request `k` up to `KB_RETRIEVE_K_MAX`, so the cap must never fall below the
request `k`.

## Fix (patch 8)

`k_reranker = max(k, global)` — the reranker never truncates below the requested
`k`. The global still raises the cap when it is larger (e.g. 50). With patch 9
(skip the cosine reranker when no real reranker is configured) the cap is moot in
the no-reranker case, but `max()` keeps the contract correct when a real
reranker IS configured. `or 0` guards a `None` config value (`max()` raises
`TypeError` on `None`).

## What patch 8 changes (1 site in `retrieval/utils.py` + 2 in `routers/retrieval.py`)

`apply_query_top_k.py` replaces the `k_reranker=` kwarg:

- `retrieval/utils.py` (the `query_collection_with_hybrid_search` call, 1
  occurrence): `k_reranker=config.get('rag.top_k_reranker'),` →
  `k_reranker=max(k, config.get('rag.top_k_reranker') or 0),`.
- `routers/retrieval.py` (the single-doc `/query` + the `/query/collection`
  handlers, 2 identical occurrences): `k_reranker=form_data.k_reranker or
  config.TOP_K_RERANKER,` → `k_reranker=max(form_data.k if form_data.k else
  config.TOP_K, form_data.k_reranker or config.TOP_K_RERANKER),`.

The utils anchor is independent of patch 7's site-3 kwarg (different line in the
same call), so the build order P7→P8 is safe. Test override:
`OWUI_UTILS_PY=… OWUI_RETRIEVAL_PY=…`.

## No KB reset on cutover (patch 8)

Routing logic only; no chunk/embedding change. Takes effect on rebuild + restart.

---

# Patch 9 — skip cosine reranker when no real reranker (`retrieval/utils.py`)

## Problem (patch 9)

When no real reranker is configured (`RAG_RERANKING_ENGINE=""`),
`RerankCompressor.acompress_documents` falls back to embedding-cosine: it
re-embeds the query + every candidate and re-sorts by pure-semantic similarity.
That is an **extra embedding pass** (a cost, not a perf hack), reused unchanged
on the hybrid path where it defeats lexical ranking: the cosine fallback re-sorts
the BM25/RRF-fused results and buries the exact keyword/register chunks BM25
surfaced (measured: `CAP_ENGAGE` exact chunk dropped to rank 10, `0x1c05`
absent). The fallback exists to keep the langchain
`ContextualCompressionRetriever` pipeline uniform, not because cosine rerank
helps. **No rerank means no rerank.**

## Fix (patch 9)

A **single-site** patch to the compressor's `else:` branch (the cosine
fallback). When `reranking_function is None`, do NOT cosine-rerank. Preserve the
input (RRF-fused) order: keep any existing per-doc `score` (the native pgvector
path and BM25 both attach one via `_search_result_to_documents` /
`merge_and_sort_query_results`), else assign a decreasing rank score
`float(len(documents) - idx)` so the downstream sort-by-distance preserves this
order. Cap at `self.top_n` (≥ `k` via patch 8). Return the pass-through directly.

A single-site patch (not gating the two call sites) is the safer choice: the
legacy ensemble path's `ainvoke` may not set `metadata['score']`, so gating at
the call sites would yield `None` distances downstream (`TypeError` on sort).
Patching the compressor else-branch covers BOTH the native pgvector path and the
legacy ensemble path uniformly, and the pass-through keeps/assigns a `score` in
both.

## What patch 9 changes (1 site in `retrieval/utils.py`)

`apply_skip_cosine_reranker.py` replaces the cosine-fallback body (12-space
indent) inside `acompress_documents`:

```python
# before
            from sentence_transformers import util as st_util

            query_embedding = await self.embedding_function(query, RAG_EMBEDDING_QUERY_PREFIX)
            doc_texts = [doc.page_content for doc in documents]
            document_embedding = await self.embedding_function(doc_texts, RAG_EMBEDDING_CONTENT_PREFIX)
            scores = st_util.cos_sim(query_embedding, document_embedding)[0]
# after
            final_results = []
            for idx, doc in enumerate(documents[: self.top_n]):
                metadata = doc.metadata
                if 'score' not in metadata:
                    metadata['score'] = float(len(documents) - idx)
                final_results.append(
                    Document(page_content=doc.page_content, metadata=metadata)
                )
            return final_results
```

The `else:` returns early, so the trailing `if scores is not None:` block (the
real-reranker sort + truncate) is now reached only when `reranking=True` (scores
set). The pre-existing unreachable trailing `else:` (`log.warning` + `return
documents`) is left untouched. `Document` is already imported in the file.
Anchor comes from the running image; the clone's `sentence_transformers` import
line differs. Test override: `OWUI_UTILS_PY=…`.

## No KB reset on cutover (patch 9)

Routing logic only; no chunk/embedding change. Takes effect on rebuild + restart.
With `RAG_RERANKING_ENGINE=""` (the default), retrieval now returns the
RRF-fused (hybrid/lexical) or cosine-distance (vector) order the upstream search
produced — no extra embedding pass, no re-burying of exact matches.

---

# Patch 10 — ParadeDB BM25 FTS arm (`retrieval/vector/dbs/pgvector.py`)

## Problem (patch 10)

The FTS arm of `hybrid_search` built its query with
`plainto_tsquery('simple', :query)` — PostgreSQL **ANDs every token**, so a
multi-term query (`rotating_thread DMA_WRR_VEC CAP_ENGAGE`) returns **0 rows**.
With the FTS arm empty, hybrid collapses to the vector arm alone (RRF
`0.5/(rank+60)`, pure-vector order) — the exact technical multi-term queries the
`/kb` skill steers agents toward return nothing lexical. It scored with
`ts_rank_cd` (cover-density rank) — **no IDF, no length normalization**; not real
BM25, so common terms swamped rare identifiers even when the AND did not zero
the result.

## Fix (patch 10)

Replace `plainto_tsquery` + `ts_rank_cd` + `@@` with ParadeDB `pg_search`:

```sql
SELECT document_chunk.id AS id, document_chunk.text AS text,
       document_chunk.vmetadata AS vmetadata,
       pdb.score(document_chunk.id) AS rank
FROM document_chunk
WHERE document_chunk.collection_name = :collection_name
  AND document_chunk.text ||| :query
ORDER BY pdb.score(document_chunk.id) DESC
LIMIT :limit
```

- `text ||| :query` is the **match-any OR** operator. It **tokenizes its RHS**
  (no Tantivy-DSL parsing), so a bare multi-term query ORs every token (the
  recall fix) and a natural-language query cannot misparse as query syntax
  (`error: DMA_WRR_VEC` ORs `error`/`dma`/`wrr`/`vec` → rows, no silent zero).
  Colon/dash/quote-safe — the C1 differential (below).
- `pdb.score(id)` is **real BM25** (IDF + length norm). The value is used for
  `ORDER BY` + psql probes only; **RRF fusion discards it** (`merge_hybrid_
  search_results` reads only ordinal rank), so hybrid order = RRF, not BM25 raw.
- **One static SQL serves both arms.** Hybrid (`bm25_weight=0.5`, global) and
  lexical (`bm25_weight=1.0`, the gateway `mode=lexical`) reach the same block;
  the mode difference is the downstream ORDER, not this SQL. At `bm25_weight=1.0`
  (`vector_weight = 1.0 - 1.0 = 0`), the vector arm is skipped
  (`if vector_weight > 0 and vectors:`), so lexical = pure BM25 order
  (single-arm RRF preserves it). Hybrid (`0.5`) = RRF fusion of BM25 + vector.
- `LIMIT :limit` unchanged (no bump — a larger LIMIT defeats ParadeDB TopN
  pushdown). The outer guard `if bm25_weight > 0 and query and query.strip():`,
  `fts_results`, `self.session.rollback()`, `merge_hybrid_search_results`, and
  the trailing `except Exception → return None` are all preserved.

## Why no Lucene DSL in patch 10 (C1)

`paradedb.parse_with_field('text', :query, lenient => true)` was the lexical-arm
DSL candidate. Verified empirically against the pinned `pg_search` 0.25.6:
`lenient` means "drop what doesn't parse, no error" — a `term:` prefix is
consumed as an unknown field name and the clause (incl. the next token) is
discarded; a leading `-` becomes must-not. So `DMA_WRR_VEC: CAP_ENGAGE` and
`-capsule scheduler` return **0 hits, no error** — re-creating the silent-zero
on the exact technical queries (`:`, leading `-`) common in the corpus.
(`lenient => false` raises; the only two states are loud error or silent zero.)
→ DSL deferred to **patch 11** as an explicit opt-in mode where the agent is
responsible for quoting/escaping. Patch 10's `|||` is the safe recall default.

## The BM25 index (operator script, NOT OWUI init)

A `USING bm25` index is required for `|||` / `pdb.score` — without it every
query errors (`document_chunk does not contain a USING bm25 index`) → caught by
`hybrid_search`'s `except → return None` → silent langchain
`BM25Retriever.from_texts` fallback (a per-request full-collection load + in-
process BM25 under the 60s timeout). So the index is a **release gate**, not a
nicety.

`scripts/kb-bm25-init.sh` (idempotent, container-targeted via
`${POSTGRES_CONTAINER:-kb-postgres}`) creates:

```sql
CREATE EXTENSION IF NOT EXISTS vector;      -- pg_search depends on vector
CREATE EXTENSION IF NOT EXISTS pg_search;
CREATE INDEX IF NOT EXISTS idx_document_chunk_bm25
  ON document_chunk USING bm25 (id, (text::pdb.simple), (collection_name::pdb.literal))
  WITH (key_field='id');
DROP INDEX IF EXISTS idx_document_chunk_text_search;  -- dead GIN FTS index (Site 2)
```

- `(text::pdb.simple)` — the tokenizer; splits `DMA_WRR_VEC` → `dma`/`wrr`/`vec`.
- `(collection_name::pdb.literal)` — exact-match fast field → per-collection `=`
  pushes into the BM25 scan (verified `TopKScanExecState` / `TopK Limit`).
- `key_field='id'` — required for `pdb.score(id)`.
- `vmetadata` is **NOT indexed** → no metadata leak via a `field:` DSL clause.
- ONE index serves `|||` (and the future patch-11 DSL). The index is
  **incremental** — no REINDEX / finalize needed after a bulk drain (unlike
  ivfflat, which still needs its REINDEX — `kb-finalize.sh` keeps that).
- It is created by the operator script, **not** at OWUI init: `__init__` runs
  `Base.metadata.create_all` + `_ensure_vector_index` in one transaction with
  `except: rollback; raise`, so an extension/index failure there would take the
  whole vector client down. Wired into `make provision` + both e2e provision
  paths (`scripts/lib-e2e-env.sh`) — an index-less provision silently degrades
  retrieval.
- `pdb.*` (v2, non-deprecated) confirmed in 0.25.6: `pdb.parse`,
  `pdb.parse_with_field`, `pdb.score` (alongside legacy `paradedb.*`).

## What patch 10 changes (2 sites in `pgvector.py`)

`apply_bm25_search.py` does two targeted replacements, each asserting the anchor
occurs exactly once (fail loud on drift):

- **Site 1** — the `plainto_tsquery` / `ts_rank_cd` / `@@` FTS block → the `|||`
  + `pdb.score` block above.
- **Site 2** — removes the `self._ensure_text_search_index()` call from
  `__init__`. That method created the GIN index
  `idx_document_chunk_text_search` — the ONLY user was the old
  `to_tsvector @@ plainto_tsquery` query (Site 1). With Site 1 gone it is dead,
  but OWUI init re-ran `CREATE INDEX IF NOT EXISTS` on every start (GIN is slow
  to update during a 9703-chunk drain) + it was REINDEXed by `kb-finalize`.
  Site 2 stops its creation; `kb-bm25-init` `DROP INDEX IF EXISTS` is the one-
  time live cleanup. The method body is left in place (unused dead code — do not
  delete unless asked). No extension/index creation is added to OWUI init.

## Dead GIN index removal (M3)

The GIN FTS index `idx_document_chunk_text_search` is dropped across the stack:
Site 2 removes its creation; `kb-bm25-init` `DROP INDEX IF EXISTS` (one-time
live cleanup, idempotent); `kb-finalize.sh` + `tests/test_09_gdrive_index.sh` +
`docs/operations.md` drop the GIN REINDEX. The ivfflat
`idx_document_chunk_vector` REINDEX **stays** in all three (still required after
a bulk drain — vector=0 → hybrid=0 without it). Rollback self-heals: the
reverted stock image re-creates + uses the GIN index.

## Release gate (patch 10)

`scripts/kb_check.py` `--bm25-gate` (wired to `make kb-bm25-check`) runs five
probes via the vector store — `pg_extension extname='pg_search'` = 1;
`idx_document_chunk_bm25` exists; the **ranking-path** query
(`SELECT id, pdb.score(id) ... WHERE collection_name=:c AND text ||| :q ORDER BY
pdb.score(id) DESC LIMIT 5`) executes without error; a **colon-safe** query
(`text ||| 'error: x'`) executes without error; a **zero-token** query
(`text ||| '???'`) → 0, no error. This is the only detector for the silent-
zero / silent-langchain-fallback class (`hybrid_search` swallows paradedb
errors → HTTP 200 + 0). A red gate = do not ship.

## No KB reset on cutover (patch 10)

The `USING bm25` index is built over the **existing** `document_chunk` rows —
no re-embed, no chunk change, no re-index. `pdb.simple` tokenizes the stored
`text` column as-is. The index builds in seconds (~9703 chunks) and is
queryable immediately. Existing vectors + chunks are untouched; only the FTS
arm's query + scoring change. Run `make kb-bm25-init` after the image restart;
the gate confirms it.

## Rollback (patch 10)

`scripts/kb-bm25-rollback.sh`: `DROP INDEX IF EXISTS idx_document_chunk_bm25;
DROP EXTENSION IF EXISTS pg_search;` (DROP INDEX **before** DROP EXTENSION —
the extension refuses to drop while the index depends on it, verified). Does
NOT drop `vector`. Refuses to run if `pg_search` is already absent (image
already reverted). Then revert the OWUI + kb-postgres images; the stock image
re-creates + uses the GIN index (Site 2's removal is gone with the revert).

## AGPL — kb-postgres image is local-only

pg_search is **AGPL-3.0**. The `kb-postgres` image (`docker/postgres/`) bundles
the pg_search `.deb` (version + sha256 pinned in `.env.template`:
`PG_SEARCH_VERSION`, `PG_SEARCH_DEB_SHA256`) fetched at build time — the `.deb`
is **never committed** to this (MIT, public) repo, and the image is **never
pushed** to a registry (`compose.yml` postgres `pull_policy: never`; `make pull`
skips it). It stays local. The OWUI overlay image is unaffected (it only emits
the `|||` SQL; the extension + index live in kb-postgres).

# Patch 11 — lexical-dsl Tantivy DSL (`pgvector.py` + `retrieval/utils.py`)

## Problem (patch 11)

Patch 10's `text ||| :query` is the match-any OR recall default — it cannot
express phrase or boolean constraints. An agent that needs "the exact phrase
`rotating thread`", "`termA` AND `termB`", or "`termA` but NOT `termB`" has no
way to ask. The DSL candidate (`paradedb.parse_with_field`) was cut from patch 10
because `lenient => true` silent-zeros on `:` / leading `-` (the C1 blocker); an
explicit opt-in mode with `lenient => false` (loud raise) is the safe path.

## Fix (patch 11)

An opt-in `lexical-dsl` retrieval mode. The plumbing is a **query-string
sentinel prefix** (not a threaded `syntax` param): the gateway
(`docker/gateway/app.py`, `RETRIEVE_MODES["lexical-dsl"] = (True, 1.0)`) prefixes
the query with `LEXICAL_DSL_PREFIX = "KB_LEXICAL_DSL_V1::"` before forwarding to
OWUI. OWUI recognizes the sentinel, strips it, and runs the remainder through
the Tantivy `QueryParser`:

```sql
SELECT document_chunk.id AS id, document_chunk.text AS text,
       document_chunk.vmetadata AS vmetadata,
       pdb.score(document_chunk.id) AS rank
FROM document_chunk
WHERE document_chunk.collection_name = :collection_name
  AND document_chunk.id @@@ paradedb.parse_with_field(
         'text', :query, lenient => false)
ORDER BY pdb.score(document_chunk.id) DESC
LIMIT :limit
```

- `paradedb.parse_with_field('text', :query, lenient => false)` rewrites to
  `text:(:query)` and parses it as a Tantivy query string. `lenient => false`
  **raises** on bad syntax (the only safe choice for an explicit opt-in; `true`
  silent-zeros on `:` / leading `-`).
- Shares the patch-10 `idx_document_chunk_bm25` index — **no kb-postgres
  rebuild, no kb-bm25-init change**.
- `bm25_weight = 1.0` (same as `lexical`), so `vector_weight = 0` → the vector
  arm is skipped → the sentinel never reaches embeddings (no semantic
  pollution). The FTS arm branches on the sentinel; the `else` branch is the
  patch-10 `|||` block verbatim (`hybrid` + `lexical` regression preserved).
- `:query` stays a bound parameter (no interpolation). The agent owns
  quoting/escaping (`:` and leading `-` are operators, not literals).

## DSL scope — 4 operators (verified against pg_search 0.25.6)

A codex deep-dive against the `v0.25.6` tag (commit `d06d83a`; Tantivy fork
`c3caae3`) confirmed there is **no higher-level parser**: `paradedb.parse()` and
`parse_with_field()` use the SAME Tantivy `QueryParser` (only field scoping
differs). Four operators work through `parse_with_field` on this index:

| Operator | Example | Behavior |
|---|---|---|
| phrase | `"zenith rotating zephyr"` | exact phrase (token order) |
| phrase-slop | `"zenith rotating"~2` | phrase within N token edits |
| `+AND` | `+dslwordalpha +dslwordbeta` | both terms required |
| composite-NOT | `+dslwordalpha -dslwordbeta` | first required, second excluded |

Four operators were cut (they error or silently return 0 on this index — parser/
grammar grounds, NOT escaping; a tokenizer change does not fix them): fuzzy
`term~N` (`~` is a bare-word char; fuzzy is per-field `set_field_fuzzy`), regex
`/re/` (`regexes_allowed=false`; `allow_regex()` never called), wildcard `pre*`
(`*` stays in the word; `pdb.simple` strips it), pure-NOT `-x` alone
(`AllButQueryForbidden`; needs a positive anchor — composite-NOT covers it).
Full-DSL support is a gateway-side **compiler** to `paradedb.boolean(...)`
builders (one `@@@`, no reindex) — a larger follow-on; `luqum` is the candidate
parser library. See the memory note `pgsearch-no-higher-level-dsl-parser`.

## C1 — the error-swallow fix (mandatory)

A DSL parse error is swallowed by a **three-layer** except/None chain and would
fall through to a full-collection in-memory `BM25Retriever` load with the raw DSL
as query → the agent gets confident **wrong** results, not an error. Each swallow
site is gated on the sentinel (so `hybrid`/`lexical` keep their fault-tolerant
fallback) and re-raises so the error propagates to `query_collection_handler`
(`routers/retrieval.py`) → `HTTPException(400)` → the gateway maps 4xx verbatim
→ the agent sees a clear parse error:

1. **`pgvector.py` `hybrid_search` broad except** (`except → rollback; log;
   return None`): re-raise after `rollback` if `query` carries the sentinel.
2. **`utils.py` `query_doc_with_native_hybrid_search` except** (`except →
   log.debug; return None`): the re-raise from site 1 propagates through
   `asyncio.gather`; re-raise again if `query` carries the sentinel.
3. **`utils.py` `query_collection` hybrid-fallback except** (`except →
   log.debug` then falls through to vector search): this is the **third** swallow
   site — it catches the re-raise from site 2 and would fall back to embedding
   the sentinel-prefixed query. Re-raise if any of `queries` carries the
   sentinel.
4. **`utils.py` `query_collection_with_hybrid_search` enriched-texts bypass**
   (the `/retrieve` entry point): when an admin sets
   `rag.enable_hybrid_search_enriched_texts=true`, this function skips the native
   path (where sites 1–3 live) and runs the in-memory `BM25Retriever` on the raw
   sentinel-prefixed query → no `lenient => false` raise fires → a malformed DSL
   returns 200 with wrong results. Guard: force the native path for any
   sentinel-prefixed query regardless of the enriched setting
   (`if not enable_enriched_texts or any(q.startswith(LEXICAL_DSL_PREFIX) for q
   in queries)`). Found by a codex blocker review.

`lenient => false` is what makes the raise fire on a real parse error.

## Sentinel contract + drift detection

The sentinel is a contract between two separately-versioned images (gateway +
kb-openwebui). The gateway is the ONLY writer; the agent never types it. Drift
breaks silently → a `kb_check` sentinel-agreement probe + the `test_11`
direct-OWUI contract catch it at gate time: a sentinel-prefixed valid DSL query
→ ≥1 hit; a sentinel-prefixed malformed query → HTTP 400. If the sentinel
changes, both images must rebuild together.

## What patch 11 changes (3 sites in `pgvector.py` + 4 in `utils.py`)

`apply_lexical_dsl.py` (runs AFTER `apply_bm25_search.py` in the Dockerfile
chain) does seven targeted replacements, each asserting the anchor occurs exactly
once (fail loud on drift):

- **`pgvector.py`** — inject the `LEXICAL_DSL_PREFIX` constant; branch the FTS
  arm on the sentinel (DSL → `parse_with_field`, else the patch-10 `|||` block
  verbatim); re-raise at the `hybrid_search` except.
- **`utils.py`** — inject the `LEXICAL_DSL_PREFIX` constant; re-raise at the
  `query_doc_with_native_hybrid_search` except; re-raise at the
  `query_collection` hybrid-fallback except; guard the
  `query_collection_with_hybrid_search` enriched-texts bypass (force the native
  path for sentinel queries).

## No KB reset on cutover (patch 11)

The `@@@` predicate queries the **existing** patch-10 BM25 index — no re-embed,
no chunk change, no re-index. Existing vectors + chunks are untouched; only the
FTS arm gains a branch and three excepts gain a gate. Run after a `kb-bm25-init`
provision (the index the predicate queries is already present).

## Rollback (patch 11)

Revert the OWUI image to the patch-10 tag (`-lexicaldsl` suffix dropped). The
`else` branch IS the patch-10 `|||` block, so a sentinel-less query is identical
to patch 10; the sentinel gates are dead code without a writer. No kb-postgres
change to revert.
