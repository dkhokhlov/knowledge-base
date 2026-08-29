#!/usr/bin/env python3
"""Apply the resilient terminal-status patch to OWUI (build-time).

Why: a knowledge-bearing file upload runs its terminal DB writes (status=
'completed', file hash) in isolated throwaway sessions (DATABASE_ENABLE_
SESSION_SHARING=False -> get_async_db_context ignores the passed db and
opens a fresh session each call). Each helper (Files.update_file_data_by_id /
update_file_hash_by_id / update_file_metadata_by_id) does `select -> mutate ->
commit` inside `except Exception: return None`, swallowing EVERY commit/write
failure. The callers in process_file ignore the None return.

Under concurrent contention (a transient error storm), the status='completed'
commit (retrieval.py) and the hash commit can fail and be swallowed while the
earlier writes (content, status='processing', collection_name) and the later
knowledge_file link commit succeed. The file ends up linked + indexed + with
content, but status stuck at 'processing' and hash missing -- a state the
gdrive reconcile never retries (it retries unlinked pending/failed, not linked
processing), so the file looks "in progress" forever and blocks the drain.

Fix (two files):

  routers/retrieval.py -- the terminal block after `if result:` (vectors saved):
    * write collection_name once in its own session (unchanged, not retried);
    * persist status='completed' with a BOUNDED RETRY (fresh session per
      attempt, short backoff) so a transient commit failure self-heals;
    * if every attempt fails, RAISE -> the caller's failed-handler runs and the
      knowledge link in _process_handler is ABORTED, so the file never ends up
      linked-but-stuck-processing (it ends up unlinked + failed/processing,
      which the reconcile retries). This is the core invariant fix;
    * persist the hash with the same bounded retry; on exhaust, log + continue
      (completed is already durable; a missing hash only causes a one-time
      re-process on the next sync, now robust via the retry above). The hash is
      dedup-only and does not block the drain.

  models/files.py -- the three `update_file_*_by_id` swallow blocks:
    * replace the silent `except Exception: return None` with
      `log.exception(...); return None`. Behavior is unchanged (still returns
      None so every caller's contract holds) -- the only effect is that a
      swallowed commit/write failure is now VISIBLE in the log, so the root
      enabler is diagnosable instead of silent. This is the traceability fix.

No commit serialization across files (perf: the gdrive index runs 151 files
through a shared Ollama; each file's retry uses its own fresh sessions, files
stay concurrent). See open-webui/PATCH.md (Patch 6).

This script does three targeted replacements (one in retrieval.py, three in
models/files.py), asserting each anchor occurs exactly once (fail loud on
drift).

Override the target files for local testing:
  OWUI_RETRIEVAL_PY=/tmp/retrieval.py OWUI_MODELS_FILES_PY=/tmp/models_files.py \
    python3 apply_terminal_status.py
"""
import pathlib
import sys

RETRIEVAL_PY = pathlib.Path(
    __import__("os").environ.get("OWUI_RETRIEVAL_PY", "/app/backend/open_webui/routers/retrieval.py")
)
MODELS_FILES_PY = pathlib.Path(
    __import__("os").environ.get("OWUI_MODELS_FILES_PY", "/app/backend/open_webui/models/files.py")
)

# --- retrieval.py: the terminal-status block after a successful vector write --

RETRIEVAL_OLD = """\
                    if result:
                        # Fresh session for the final update.
                        async with get_async_db() as session:
                            await Files.update_file_metadata_by_id(
                                file.id,
                                {
                                    'collection_name': collection_name,
                                },
                                db=session,
                            )

                            await Files.update_file_data_by_id(
                                file.id,
                                {'status': 'completed'},
                                db=session,
                            )
                            await Files.update_file_hash_by_id(file.id, hash, db=session)

                            await publish_event(
                                request,
                                EVENTS.RETRIEVAL_CONTENT_PROCESSED,
                                actor=user,
                                subject_id=file.id,
                                subject_type='file',
                                data={'collection_name': collection_name, 'filename': file.filename},
                            )
                            return {
                                'status': True,
                                'collection_name': collection_name,
                                'filename': file.filename,
                                'content': text_content,
                            }
"""

RETRIEVAL_NEW = """\
                    if result:
                        # Fresh session for the final update.
                        # collection_name is a metadata tag; write it once in its
                        # own session. Not retried -- a missing tag is non-fatal and
                        # the KB link below does not depend on it.
                        async with get_async_db() as session:
                            await Files.update_file_metadata_by_id(
                                file.id,
                                {
                                    'collection_name': collection_name,
                                },
                                db=session,
                            )

                        # Terminal status -- the file is fully indexed. Persist
                        # 'completed' with a bounded retry; each attempt opens its
                        # own fresh session so a transient commit failure
                        # (concurrent contention) self-heals instead of leaving the
                        # file stuck at 'processing'. If every attempt fails, raise
                        # -> the caller's failed-handler runs and the knowledge link
                        # in _process_handler is aborted, so the file never ends up
                        # linked-but-stuck-processing (a state no reconcile retries).
                        # [patch-6]
                        _TERMINAL_RETRY = 3
                        _TERMINAL_BACKOFF = 0.2
                        _status_ok = False
                        for _attempt in range(_TERMINAL_RETRY):
                            async with get_async_db() as session:
                                _r = await Files.update_file_data_by_id(
                                    file.id, {'status': 'completed'}, db=session,
                                )
                            if _r is not None:
                                _status_ok = True
                                break
                            log.warning(
                                'terminal status completed did not persist for %s '
                                '(attempt %d/%d)', file.id, _attempt + 1, _TERMINAL_RETRY,
                            )
                            await asyncio.sleep(_TERMINAL_BACKOFF * (_attempt + 1))
                        if not _status_ok:
                            raise Exception(
                                'Failed to persist terminal status completed for '
                                f'{file.id} after {_TERMINAL_RETRY} attempts -- '
                                'aborting before knowledge link'
                            )

                        # Hash is dedup-only: a missing hash causes a one-time
                        # re-process on the next sync (now robust via the retry
                        # above) and does not block the drain. Retry it; if every
                        # attempt fails, log and continue -- 'completed' is already
                        # durable. [patch-6]
                        _hash_ok = False
                        for _attempt in range(_TERMINAL_RETRY):
                            async with get_async_db() as session:
                                _r = await Files.update_file_hash_by_id(
                                    file.id, hash, db=session,
                                )
                            if _r is not None:
                                _hash_ok = True
                                break
                            log.warning(
                                'file hash did not persist for %s (attempt %d/%d)',
                                file.id, _attempt + 1, _TERMINAL_RETRY,
                            )
                            await asyncio.sleep(_TERMINAL_BACKOFF * (_attempt + 1))
                        if not _hash_ok:
                            log.error(
                                'Failed to persist hash for %s after %d attempts -- '
                                'completed status is durable; file will re-process on '
                                'next sync', file.id, _TERMINAL_RETRY,
                            )

                        await publish_event(
                            request,
                            EVENTS.RETRIEVAL_CONTENT_PROCESSED,
                            actor=user,
                            subject_id=file.id,
                            subject_type='file',
                            data={'collection_name': collection_name, 'filename': file.filename},
                        )
                        return {
                            'status': True,
                            'collection_name': collection_name,
                            'filename': file.filename,
                            'content': text_content,
                        }
"""

# --- models/files.py: stop the silent swallow in the three update helpers ----
# Each block is anchored on its method-specific mutation line so the bare
# `except Exception: return None` (which repeats) resolves to exactly one site.
# Behavior is unchanged -- still returns None; only log.exception is added.

HASH_OLD = """\
                file.hash = hash
                file.updated_at = int(time.time())
                await db.commit()

                return FileModel.model_validate(file)
            except Exception:
                return None
"""

HASH_NEW = """\
                file.hash = hash
                file.updated_at = int(time.time())
                await db.commit()

                return FileModel.model_validate(file)
            except Exception as e:
                log.exception(f'Error updating file hash by id {id}: {e}')
                return None
"""

DATA_OLD = """\
                file.data = {**(file.data if file.data else {}), **data}
                file.updated_at = int(time.time())
                await db.commit()
                return FileModel.model_validate(file)
            except Exception as e:
                return None
"""

DATA_NEW = """\
                file.data = {**(file.data if file.data else {}), **data}
                file.updated_at = int(time.time())
                await db.commit()
                return FileModel.model_validate(file)
            except Exception as e:
                log.exception(f'Error updating file data by id {id}: {e}')
                return None
"""

META_OLD = """\
                file.meta = {**(file.meta if file.meta else {}), **meta}
                file.updated_at = int(time.time())
                await db.commit()
                return FileModel.model_validate(file)
            except Exception:
                return None
"""

META_NEW = """\
                file.meta = {**(file.meta if file.meta else {}), **meta}
                file.updated_at = int(time.time())
                await db.commit()
                return FileModel.model_validate(file)
            except Exception as e:
                log.exception(f'Error updating file metadata by id {id}: {e}')
                return None
"""


def _patch(path: pathlib.Path, label: str, old: str, new: str) -> None:
    if not path.exists():
        print(f"FAIL {label}: target not found: {path}", file=sys.stderr)
        sys.exit(1)
    text = path.read_text()
    n = text.count(old)
    if n != 1:
        print(f"FAIL {label}: expected exactly 1 occurrence of anchor, found {n}", file=sys.stderr)
        print(f"     anchor first line: {old.splitlines()[0]!r}", file=sys.stderr)
        sys.exit(1)
    path.write_text(text.replace(old, new))
    print(f"OK {label} applied to {path}")


def main() -> None:
    _patch(RETRIEVAL_PY, "terminal-status retry", RETRIEVAL_OLD, RETRIEVAL_NEW)
    _patch(MODELS_FILES_PY, "hash-swallow logging", HASH_OLD, HASH_NEW)
    _patch(MODELS_FILES_PY, "data-swallow logging", DATA_OLD, DATA_NEW)
    _patch(MODELS_FILES_PY, "metadata-swallow logging", META_OLD, META_NEW)
    print("OK terminal-status patch applied (retrieval.py 1 site, models/files.py 3 sites)")


if __name__ == "__main__":
    main()