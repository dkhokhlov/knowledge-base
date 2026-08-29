#!/usr/bin/env python3
"""KB cross-DB health check: reconcile OWUI SQLite + embedded Chroma, report
inconsistencies, and (opt-in) export-then-purge orphans.

The KB stack has two stores that are NOT ACID across each other:
  - OWUI SQLite  (webui.db: file, knowledge_file, knowledge_directory, knowledge).
  - embedded Chroma (/app/backend/data/vector_db: file-{id} + <knowledge_id>
    collections, plus the OWUI-internal `knowledge-bases` collection).

Drift this tool detects + clears: ghost rows, orphan file-{id} collections (the
files.py `delete()` no-op leak), dangling HNSW segment dirs, orphan KB vectors,
dead-KB junction rows, and more (12 classes). See the class table in the report.

`chromadb` lives only in the kb-openwebui image, so this tool runs INSIDE that
container via `docker exec -i kb-openwebui python3 - < scripts/kb_check.py` (host
script piped to container python on stdin; opts after `-`). For the maintenance
window (`--maint`) the Makefile stops kb-openwebui and runs a throwaway container
from the same image with the data dir mounted, so direct Chroma/SQLite writes
are safe (no OWUI process contention). `chromadb` is lazy-imported so the
argparse/classify/report logic imports cleanly on the host for unit tests.

Default = audit + report + advise (zero mutation). `--purge` consents to purge
the safe classes (1, 3, 11); `--purge --maint` purges the maintenance-window
classes (5b, 7, 8). `--no-backup` skips the export (backup is ON by default when
purging). Stdout is ids-only by default (PII guard); `--show-names` adds
filenames; full text is written only into the gitignored export dir.

Exit codes: 0 audit-only or purge-done; 1 fatal error; 2 bad option / missing
prerequisite (admin key for ghost purge).
"""
import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DEFAULT_DATA_DIR = "/app/backend/data"
DEFAULT_OWUI_BASE = "http://127.0.0.1:8080"
CHROMA_DB_FILENAME = "chroma.sqlite3"
WEBUI_DB_FILENAME = "webui.db"
VECTOR_DB_DIRNAME = "vector_db"
EXPORT_DIRNAME = "check-exports"
FILE_COLL_PREFIX = "file-"
KNOWLEDGE_BASES_COLL = "knowledge-bases"  # OWUI-internal; never treated as a KB
SAMPLE_CAP = 8  # max sample ids/names shown per class in the report

TIER_SAFE = "safe"        # safe while OWUI runs: class 1 (OWUI REST), 3, 11
TIER_MAINT = "maint"      # maintenance window only: class 5b, 7, 8
TIER_ADVISORY = "advisory"  # report + advise only, never auto-purged

# Repair gate (class 9 stuck-processing-while-linked subset): a file is only
# repaired to 'completed' if its last DB write is older than this. Excludes the
# ms-wide race where a genuinely-in-flight process_file is between its last
# pre-terminal write and the terminal status commit. 60s is safe -- a normal
# terminal commit lands in ms; only a stuck file is older than this.
REPAIR_STALE_SECS = 60

log = logging.getLogger("kb-check")


class _UtcISOFormatter(logging.Formatter):
    """ISO-8601 UTC timestamp on every log line (matches the rest of the stack)."""
    converter = time.gmtime

    def formatTime(self, record, datefmt=None):
        return time.strftime("%Y-%m-%dT%H:%M:%S", self.converter(record.created)) \
            + ".%03dZ" % (record.msecs)


def _configure_logging():
    h = logging.StreamHandler()
    h.setFormatter(_UtcISOFormatter("%(asctime)s %(levelname)s %(name)s - %(message)s"))
    root = logging.getLogger()
    root.handlers = [h]
    root.setLevel(logging.INFO)


# --- data row containers (plain classes; stdlib only) ---------------------

class FileRow:
    __slots__ = ("id", "filename", "knowledge_id", "directory_id",
                 "file_hash", "status", "hash")

    def __init__(self, id, filename, knowledge_id, directory_id,
                 file_hash, status, hash):
        self.id = id
        self.filename = filename
        self.knowledge_id = knowledge_id
        self.directory_id = directory_id
        self.file_hash = file_hash
        self.status = status
        self.hash = hash


class JunctionRow:
    __slots__ = ("id", "knowledge_id", "file_id", "directory_id")

    def __init__(self, id, knowledge_id, file_id, directory_id):
        self.id = id
        self.knowledge_id = knowledge_id
        self.file_id = file_id
        self.directory_id = directory_id


class ClassResult:
    """One inconsistency class: full id list (purge), capped samples (report),
    purge tier, and optional detail dict."""
    __slots__ = ("ids", "samples", "tier", "detail")

    def __init__(self, ids=None, tier=TIER_ADVISORY, detail=None):
        self.ids = ids or []
        self.samples = self.ids[:SAMPLE_CAP]
        self.tier = tier
        self.detail = detail

    @property
    def count(self):
        return len(self.ids)


# --- Stores: read + mutating access to the two DBs + disk -----------------
# Override the read methods in tests to feed in-memory fixtures.

class Stores:
    def __init__(self, data_dir, owui_base, admin_key):
        self.data_dir = data_dir
        self.owui_base = owui_base.rstrip("/")
        self.admin_key = admin_key
        self.vector_db_dir = os.path.join(data_dir, VECTOR_DB_DIRNAME)
        self.webui_db_path = os.path.join(data_dir, WEBUI_DB_FILENAME)
        self.chroma_sqlite_path = os.path.join(self.vector_db_dir, CHROMA_DB_FILENAME)
        self._webui_ro = None
        self._chroma_db = None
        self._chroma_client = None
        self._webui_rw_db = None
        self._cache = {}

    def invalidate(self):
        """Drop the in-memory read cache so the next read re-queries the DB.
        Call after any RW action (purge/repair) before a re-audit, else the
        post-action report reflects the pre-action snapshot (stale)."""
        self._cache.clear()

    # --- OWUI SQLite (read-only) -------------------------------------------

    def _webui(self):
        if self._webui_ro is None:
            self._webui_ro = sqlite3.connect(
                "file:%s?mode=ro" % self.webui_db_path, uri=True)
            self._webui_ro.row_factory = sqlite3.Row
        return self._webui_ro

    def file_rows(self):
        """{file_id: FileRow}. meta.data.{knowledge_id, directory_id, file_hash};
        data.status is top-level in the data column."""
        if "files" not in self._cache:
            out = {}
            cur = self._webui().execute(
                "SELECT id, filename, meta, data, hash FROM file")
            for r in cur:
                meta = json.loads(r["meta"]) if r["meta"] else {}
                data = json.loads(r["data"]) if r["data"] else {}
                md = meta.get("data") or {} if isinstance(meta, dict) else {}
                status = data.get("status") if isinstance(data, dict) else None
                out[r["id"]] = FileRow(
                    id=r["id"], filename=r["filename"] or "",
                    knowledge_id=md.get("knowledge_id") or None,
                    directory_id=md.get("directory_id") or None,
                    file_hash=md.get("file_hash") or None,
                    status=status, hash=r["hash"])
            self._cache["files"] = out
        return self._cache["files"]

    def junction_rows(self):
        """[JunctionRow] for every knowledge_file row."""
        if "junction" not in self._cache:
            out = []
            cur = self._webui().execute(
                "SELECT id, knowledge_id, file_id, directory_id FROM knowledge_file")
            for r in cur:
                out.append(JunctionRow(id=r["id"], knowledge_id=r["knowledge_id"],
                                       file_id=r["file_id"],
                                       directory_id=r["directory_id"]))
            self._cache["junction"] = out
        return self._cache["junction"]

    def knowledge_ids(self):
        """{kb_id: name} for every row in the knowledge table (live KBs)."""
        if "kb_ids" not in self._cache:
            out = {}
            for r in self._webui().execute("SELECT id, name FROM knowledge"):
                out[r["id"]] = r["name"]
            self._cache["kb_ids"] = out
        return self._cache["kb_ids"]

    def directory_ids(self):
        """set of every knowledge_directory.id."""
        if "dir_ids" not in self._cache:
            self._cache["dir_ids"] = set(
                r[0] for r in self._webui().execute("SELECT id FROM knowledge_directory"))
        return self._cache["dir_ids"]

    # --- Chroma SQLite (read-only; name<->uuid<->segment mapping) ----------

    def _chroma_sqlite(self):
        if self._chroma_db is None:
            self._chroma_db = sqlite3.connect(
                "file:%s?mode=ro" % self.chroma_sqlite_path, uri=True)
            self._chroma_db.row_factory = sqlite3.Row
        return self._chroma_db

    def chroma_collections(self):
        """{collection_name: collection_uuid} from the chroma collections table."""
        if "colls" not in self._cache:
            out = {}
            for r in self._chroma_sqlite().execute("SELECT id, name FROM collections"):
                out[r["name"]] = r["id"]
            self._cache["colls"] = out
        return self._cache["colls"]

    def vector_segment_ids(self):
        """set of segment ids with scope='VECTOR' (each owns an on-disk HNSW dir)."""
        if "vsegs" not in self._cache:
            self._cache["vsegs"] = set(
                r[0] for r in self._chroma_sqlite().execute(
                    "SELECT id FROM segments WHERE scope='VECTOR'"))
        return self._cache["vsegs"]

    def segment_dir_for_collection(self, coll_uuid):
        """The VECTOR segment id (on-disk dir name) for a collection uuid, or None."""
        row = self._chroma_sqlite().execute(
            "SELECT id FROM segments WHERE collection=? AND scope='VECTOR'",
            (coll_uuid,)).fetchone()
        return row[0] if row else None

    # --- Chroma client (lazy; only present in the container image) --------

    def _chroma(self):
        if self._chroma_client is None:
            import chromadb  # lazy: host has no chromadb (unit tests stub this)
            self._chroma_client = chromadb.PersistentClient(path=self.vector_db_dir)
        return self._chroma_client

    def collection_count(self, name):
        """Number of vectors in a named collection (0 if the collection is gone)."""
        try:
            return self._chroma().get_collection(name).count()
        except Exception:
            return 0

    def collection_metadatas(self, name):
        """[{file_id: ..., ...}] for every vector in a named collection."""
        try:
            res = self._chroma().get_collection(name).get(include=["metadatas"])
        except Exception:
            return []
        return list(res.get("metadatas") or [])

    def collection_documents(self, name):
        """All chunk ids + documents + metadata for export (read once)."""
        try:
            res = self._chroma().get_collection(name).get(
                include=["documents", "metadatas"])
        except Exception:
            return [], [], []
        return (list(res.get("ids") or []),
                list(res.get("documents") or []),
                list(res.get("metadatas") or []))

    # --- on-disk segment dirs ---------------------------------------------

    def disk_dirs(self):
        """set of immediate child directory names under vector_db/."""
        if not os.path.isdir(self.vector_db_dir):
            return set()
        return set(n for n in os.listdir(self.vector_db_dir)
                   if os.path.isdir(os.path.join(self.vector_db_dir, n)))

    def dir_size(self, name):
        """Total bytes of a dir under vector_db/ (best effort)."""
        root = os.path.join(self.vector_db_dir, name)
        total = 0
        for dp, _d, fs in os.walk(root):
            for f in fs:
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        return total

    # --- mutating: safe tier (OWUI running) -------------------------------

    def owui_delete_file(self, file_id):
        """OWUI REST DELETE /api/v1/files/{id} at the in-container OWUI base.
        Cleans blob + file row + KB-collection vectors (patch 4). The residual
        file-{id} Chroma collection remains (the delete() no-op leak); the caller
        drops it via delete_collection. Raises on non-2xx / missing admin key."""
        if not self.admin_key:
            raise RuntimeError("OPENWEBUI_ADMIN_API_KEY unset (ghost purge needs it)")
        url = "%s/api/v1/files/%s" % (self.owui_base, file_id)
        req = urllib.request.Request(url, method="DELETE",
                                      headers={"Authorization": "Bearer " + self.admin_key})
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status >= 300:
                raise RuntimeError("OWUI DELETE %s -> HTTP %d" % (file_id, resp.status))
        return True

    def delete_collection(self, name):
        """Drop a Chroma collection by name (drops the collections + segments rows;
        does NOT reclaim the on-disk HNSW dir; caller rm -rf it)."""
        self._chroma().delete_collection(name)
        return True

    def rm_segment_dir(self, dir_name):
        """rm -rf a dir under vector_db/ (a reclaimed or dangling HNSW dir)."""
        shutil.rmtree(os.path.join(self.vector_db_dir, dir_name), ignore_errors=True)
        return True

    # --- mutating: maintenance tier (OWUI stopped) ------------------------

    def delete_kb_vectors_by_file(self, kb_name, file_id):
        """Direct Chroma delete(where={'file_id': file_id}) on a KB collection.
        Maintenance window only (OWUI stopped). Uses the RAW Chroma Collection
        API, whose delete takes `where` (metadata filter), NOT `filter` -- that
        is the kwarg one layer up at OWUI's async VectorDB wrapper
        (apply_vector_cleanup_on_delete.py), which translates filter->where.
        Returns the Chroma DeleteResult `deleted` count (int) or None; the
        caller records it so a wrong filter key/type (a silent 0-delete)
        surfaces in the purge manifest, not just the post-purge re-audit."""
        res = self._chroma().get_collection(kb_name).delete(where={"file_id": file_id})
        return res.get("deleted") if isinstance(res, dict) else None

    def _webui_rw(self):
        if self._webui_rw_db is None:
            self._webui_rw_db = sqlite3.connect(self.webui_db_path)
            self._webui_rw_db.row_factory = sqlite3.Row
        return self._webui_rw_db

    def delete_junction_by_file(self, file_id):
        """DELETE FROM knowledge_file WHERE file_id=? (maintenance window)."""
        self._webui_rw().execute(
            "DELETE FROM knowledge_file WHERE file_id=?", (file_id,))
        self._webui_rw().commit()
        return True

    def delete_junction_by_knowledge(self, kb_id):
        """DELETE FROM knowledge_file WHERE knowledge_id=? (maintenance window)."""
        self._webui_rw().execute(
            "DELETE FROM knowledge_file WHERE knowledge_id=?", (kb_id,))
        self._webui_rw().commit()
        return True

    # --- repair (class 9 stuck-processing-while-linked) --------------------

    def file_content(self, file_id):
        """data.content for one file (lazy read; None if absent). Used by the
        repair gate to prove extraction finished (vectors alone could be a
        mid-flight snapshot; content-present confirms the pre-embedding write
        committed)."""
        row = self._webui().execute(
            "SELECT data FROM file WHERE id=?", (file_id,)).fetchone()
        if not row or not row["data"]:
            return None
        data = json.loads(row["data"])
        return data.get("content") if isinstance(data, dict) else None

    def file_updated_at(self, file_id):
        """updated_at (epoch seconds) for one file; None if the row is gone."""
        row = self._webui().execute(
            "SELECT updated_at FROM file WHERE id=?", (file_id,)).fetchone()
        return row["updated_at"] if row else None

    def repair_file_status(self, file_id):
        """Set data.status='completed' for a stuck-processing-while-linked file.
        Maintenance window (OWUI stopped). Flips only the status field; the row
        keeps its existing content / collection_name / hash (a missing File.hash
        is benign for a linked member -- the reconcile does not re-process linked
        members, and patch-2 idempotency reuses meta.data.file_hash, not
        File.hash). Returns True if updated, False if the row vanished."""
        row = self._webui_rw().execute(
            "SELECT data FROM file WHERE id=?", (file_id,)).fetchone()
        if not row:
            return False
        data = json.loads(row["data"]) if row["data"] else {}
        data["status"] = "completed"
        self._webui_rw().execute(
            "UPDATE file SET data=?, updated_at=? WHERE id=?",
            (json.dumps(data), int(time.time()), file_id))
        self._webui_rw().commit()
        return True


# --- classification (pure logic over Stores) ------------------------------

def classify(stores, kb=None):
    """Return {class_name: ClassResult} for all 12 classes + a '_totals' dict.
    `kb` scopes the KB-tagged classes to one knowledge_id; classes 3, 11, 12 are
    KB-agnostic (always global)."""
    all_files = stores.file_rows()
    all_junction = stores.junction_rows()
    kb_ids = stores.knowledge_ids()        # {kb_id: name} (live KBs)
    dir_ids = stores.directory_ids()
    colls = stores.chroma_collections()    # {name: uuid}

    all_file_ids = set(all_files)
    all_junction_file_ids = set(j.file_id for j in all_junction)
    live_kb_ids = set(kb_ids)

    # scope: KB-tagged classes use kb_files/kb_junction; classes 3/11/12 stay global
    if kb:
        kb_files = {fid: fr for fid, fr in all_files.items()
                    if fr.knowledge_id == kb}
        kb_junction = [j for j in all_junction if j.knowledge_id == kb]
        scan_kb_ids = {kb} & live_kb_ids   # only if the scoped KB is live
    else:
        kb_files = all_files
        kb_junction = all_junction
        scan_kb_ids = live_kb_ids

    kb_junction_file_ids = set(j.file_id for j in kb_junction)
    classes = {}

    # 1. ghost rows: completed + knowledge_id set, NOT in the (scoped) junction.
    c1_ids = [fid for fid, fr in kb_files.items()
              if fr.status == "completed" and fr.knowledge_id
              and fid not in kb_junction_file_ids]
    classes["ghost_rows"] = ClassResult(c1_ids, TIER_SAFE)

    # 2. stale directory_id: non-empty directory_id not in knowledge_directory.
    c2_ids = [fid for fid, fr in kb_files.items()
              if fr.directory_id and fr.directory_id not in dir_ids]
    classes["stale_directory_id"] = ClassResult(c2_ids, TIER_ADVISORY)

    # 3. orphan file-{id} collections: no file DB row. GLOBAL (KB-agnostic).
    c3_ids = []
    orphan_chunks = 0
    for name in colls:
        if not name.startswith(FILE_COLL_PREFIX) or name == KNOWLEDGE_BASES_COLL:
            continue
        fid = name[len(FILE_COLL_PREFIX):]
        if fid not in all_file_ids:
            c3_ids.append(name)
            orphan_chunks += stores.collection_count(name)
    classes["orphan_file_collections"] = ClassResult(
        c3_ids, TIER_SAFE, {"orphan_chunks": orphan_chunks})

    # 4. missing file-{id} collection: completed file row, no file-{id} collection.
    c4_ids = [fid for fid, fr in kb_files.items()
              if fr.status == "completed" and (FILE_COLL_PREFIX + fid) not in colls]
    classes["missing_file_collections"] = ClassResult(c4_ids, TIER_ADVISORY)

    # 5. orphan KB-collection vectors: KB collections enumerated strictly from
    #    the knowledge table (excludes knowledge-bases). A vector is flagged only
    #    if its metadata has a file_id key AND that file_id is not in the (global)
    #    junction. 5a = file_id has a DB row (ghost; class 1 handles the purge),
    #    5b = file_id has no DB row (leaked KB vectors; maint purge).
    c5_ids = []
    c5a = c5b = 0
    leaked_pairs = []  # (kb_id, file_id) for maint purge
    for kb_id in scan_kb_ids:
        if kb_id not in colls:
            continue  # live KB but no Chroma collection
        for md in stores.collection_metadatas(kb_id):
            fid = md.get("file_id") if isinstance(md, dict) else None
            if not fid or fid in all_junction_file_ids:
                continue  # require the file_id key (excludes knowledge-bases rows)
            c5_ids.append(fid)
            if fid in all_file_ids:
                c5a += 1
            else:
                c5b += 1
                leaked_pairs.append((kb_id, fid))
    classes["orphan_kb_vectors"] = ClassResult(
        c5_ids, TIER_MAINT, {"ghost_link": c5a, "leaked": c5b,
                              "leaked_pairs": leaked_pairs})

    # 7 + 8 before 6 (class 6 subtracts class-7 rows; class 8 rows are dead-KB).
    # 7. orphan junction rows: file_id not in the file table (uses FULL file_ids).
    c7_ids = [j.file_id for j in kb_junction if j.file_id not in all_file_ids]
    classes["orphan_junction_rows"] = ClassResult(c7_ids, TIER_MAINT)

    # 8. dead-KB junction rows: knowledge_id not in the knowledge table.
    c8_ids = [j.knowledge_id for j in kb_junction if j.knowledge_id not in live_kb_ids]
    classes["dead_kb_junction_rows"] = ClassResult(c8_ids, TIER_MAINT)

    # 6. missing KB-collection vectors: junction file_id (live KB, in file table,
    #    not class 7) with 0 vectors for that file_id in its KB collection.
    kb_vector_file_counts = {}  # kb_id -> {file_id: count}
    for kb_id in scan_kb_ids:
        if kb_id not in colls:
            continue
        counts = {}
        for md in stores.collection_metadatas(kb_id):
            fid = md.get("file_id") if isinstance(md, dict) else None
            if fid:
                counts[fid] = counts.get(fid, 0) + 1
        kb_vector_file_counts[kb_id] = counts
    # A pending/failed file legitimately has 0 vectors; class 6 flags only COMPLETED
    # linked files whose vectors are missing (lost-vector detection).
    c6_ids = []
    for j in kb_junction:
        if j.file_id not in all_file_ids or j.knowledge_id not in live_kb_ids:
            continue  # class 7 / class 8
        if all_files[j.file_id].status != "completed":
            continue  # non-completed -> class 9, not a lost-vector case
        counts = kb_vector_file_counts.get(j.knowledge_id)
        if counts is None:
            continue  # live KB with no collection
        if counts.get(j.file_id, 0) == 0:
            c6_ids.append(j.file_id)
    classes["missing_kb_vectors"] = ClassResult(c6_ids, TIER_ADVISORY)

    # 9. non-completed leftovers: knowledge_id set, status != completed.
    c9_ids = [fid for fid, fr in kb_files.items()
              if fr.knowledge_id and fr.status and fr.status != "completed"]
    # 9a. stuck-processing-while-linked (the repairable subset of 9): status=
    #     'processing' AND linked AND has vectors in its KB collection. The embed
    #     + link finished but the terminal status write failed silently (patch 6
    #     root cause: a swallowed commit in update_file_data_by_id). No reconcile
    #     retries a LINKED processing file, so these stay "in progress" forever
    #     and need `--repair`. Content + staleness are re-checked at repair time.
    #     pending/failed files are reconcile-retryable and are left alone.
    c9_stuck = []
    for fid in c9_ids:
        fr = kb_files[fid]
        if fr.status != "processing" or fid not in kb_junction_file_ids:
            continue
        kb_id = next((j.knowledge_id for j in kb_junction if j.file_id == fid), None)
        if not kb_id:
            continue
        counts = kb_vector_file_counts.get(kb_id)
        nv = counts.get(fid, 0) if counts else 0
        if nv > 0:
            c9_stuck.append({"id": fid, "kb_id": kb_id, "vectors": nv})
    classes["non_completed_leftovers"] = ClassResult(
        c9_ids, TIER_ADVISORY, {"stuck_processing_linked": c9_stuck})

    # 10. idempotency-key duplicates: (knowledge_id, directory_id, filename, hash) >1.
    groups = {}
    for fid, fr in kb_files.items():
        if not fr.knowledge_id:
            continue
        key = (fr.knowledge_id, fr.directory_id or "", fr.filename, fr.hash or "")
        groups.setdefault(key, []).append(fid)
    c10_ids = [fid for fids in groups.values() if len(fids) > 1 for fid in fids]
    classes["idempotency_duplicates"] = ClassResult(c10_ids, TIER_ADVISORY)

    # 11. dangling segment dirs: on-disk dir not a VECTOR segment id. GLOBAL.
    disk = stores.disk_dirs()
    seg_ids = stores.vector_segment_ids()
    dangling = sorted(disk - seg_ids)
    total = sum(stores.dir_size(d) for d in dangling)
    classes["dangling_segment_dirs"] = ClassResult(
        dangling, TIER_SAFE, {"total_bytes": total})

    # 12. file rows with no knowledge_id (awareness only). GLOBAL (KB-agnostic).
    c12_ids = [fid for fid, fr in all_files.items() if not fr.knowledge_id]
    classes["file_rows_no_knowledge_id"] = ClassResult(c12_ids, TIER_ADVISORY)

    classes["_totals"] = {
        "file_rows": len(all_files),
        "file_rows_scoped": len(kb_files),
        "knowledge_kbs": len(kb_ids),
        "junction_rows": len(all_junction),
        "junction_rows_scoped": len(kb_junction),
        "chroma_collections": len(colls),
        "vector_segment_dirs": len(disk),
        "vector_segments": len(seg_ids),
        "scope": kb or "ALL",
    }
    return classes


# --- report (human table + JSON) -----------------------------------------

CLASS_ORDER = [
    ("ghost_rows", "1"),
    ("stale_directory_id", "2"),
    ("orphan_file_collections", "3"),
    ("missing_file_collections", "4"),
    ("orphan_kb_vectors", "5"),
    ("missing_kb_vectors", "6"),
    ("orphan_junction_rows", "7"),
    ("dead_kb_junction_rows", "8"),
    ("non_completed_leftovers", "9"),
    ("idempotency_duplicates", "10"),
    ("dangling_segment_dirs", "11"),
    ("file_rows_no_knowledge_id", "12"),
]

GLYPH = {TIER_SAFE: "✗", TIER_MAINT: "✗", TIER_ADVISORY: "○"}


def _fmt_samples(samples, show_names, names):
    if not samples:
        return "—"
    if show_names:
        return ", ".join("%s(%s)" % (names.get(s, ""), s) for s in samples)
    return ", ".join(samples)


def advised_commands(classes):
    cmds = []
    safe = (classes["ghost_rows"].count + classes["orphan_file_collections"].count
            + classes["dangling_segment_dirs"].count) > 0
    maint = (classes["orphan_kb_vectors"].detail.get("leaked", 0)
            + classes["orphan_junction_rows"].count
            + classes["dead_kb_junction_rows"].count) > 0
    if safe:
        cmds.append("PURGE=1 make kb-check            # purge safe classes (1,3,11); backup on")
        cmds.append("PURGE=1 BACKUP=0 make kb-check   # purge safe classes, no backup export")
    if maint:
        cmds.append("PURGE=1 MAINT=1 make kb-check    # maintenance window: stop OWUI, purge 5b,7,8")
    stuck = (classes["non_completed_leftovers"].detail or {}).get("stuck_processing_linked", [])
    if stuck:
        cmds.append("REPAIR=1 make kb-check          # stop OWUI, repair stuck-processing-while-linked -> completed")
    if not safe and not maint and not stuck:
        cmds.append("# no purgeable/repairable classes found; nothing to do")
    return cmds


def report_human(classes, show_names, names):
    t = classes["_totals"]
    out = []
    out.append("KB cross-DB check  %s" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    out.append("Scope: %s" % t["scope"])
    out.append("")
    out.append("Totals:")
    out.append("  file rows:            %d  (in scope: %d)" % (
        t["file_rows"], t["file_rows_scoped"]))
    out.append("  knowledge KBs (live): %d" % t["knowledge_kbs"])
    out.append("  junction rows:        %d  (in scope: %d)" % (
        t["junction_rows"], t["junction_rows_scoped"]))
    out.append("  chroma collections:   %d" % t["chroma_collections"])
    out.append("  vector segment dirs: %d  (VECTOR segments: %d)" % (
        t["vector_segment_dirs"], t["vector_segments"]))
    out.append("")
    out.append("Inconsistencies (✓ clean / ✗ found / ○ advisory):")
    out.append("  #  class                         count   tier        samples")
    for name, num in CLASS_ORDER:
        c = classes[name]
        glyph = "✓" if c.count == 0 else GLYPH.get(c.tier, "✗")
        out.append("  %-2s %-30s %-7d %-11s %s" % (
            num, name, c.count, c.tier, _fmt_samples(c.samples, show_names, names)))
        if name == "orphan_file_collections" and c.detail:
            out.append("      orphan_chunks=%d" % c.detail.get("orphan_chunks", 0))
        if name == "orphan_kb_vectors" and c.detail:
            out.append("      ghost_link=%d  leaked=%d" % (
                c.detail.get("ghost_link", 0), c.detail.get("leaked", 0)))
        if name == "dangling_segment_dirs" and c.detail:
            out.append("      total=%.1f MB" % (c.detail.get("total_bytes", 0) / 1048576.0))
        if name == "non_completed_leftovers" and c.detail:
            _stuck = c.detail.get("stuck_processing_linked", [])
            if _stuck:
                out.append("      stuck_processing_linked=%d (repairable: REPAIR=1)" % len(_stuck))
    out.append("")
    out.append("Advised commands:")
    for c in advised_commands(classes):
        out.append("  " + c)
    return "\n".join(out)


def report_json(classes, show_names, names):
    out = {"scope": classes["_totals"]["scope"],
           "totals": classes["_totals"], "classes": {}, "advised_commands": []}
    for name, _ in CLASS_ORDER:
        c = classes[name]
        out["classes"][name] = {
            "count": c.count,
            "tier": c.tier,
            "samples": c.samples if not show_names
            else [{"name": names.get(s, ""), "id": s} for s in c.samples],
            "detail": c.detail,
        }
    out["advised_commands"] = [c.split("#")[0].strip() for c in advised_commands(classes)]
    return json.dumps(out, indent=2, ensure_ascii=False)


# --- export (backup before purge) ----------------------------------------

def _export_dir(data_dir, ts):
    d = os.path.join(data_dir, EXPORT_DIRNAME, ts)
    os.makedirs(d, exist_ok=True)
    return d


def export_collection(stores, name, export_dir):
    """Write a collection's chunks to <name>.jsonl; return a manifest entry
    (segment id + dir path + chunk count). Captured BEFORE delete_collection."""
    ids, docs, mets = stores.collection_documents(name)
    safe_name = name.replace(os.sep, "_")
    path = os.path.join(export_dir, safe_name + ".jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for i in range(len(ids)):
            f.write(json.dumps({"id": ids[i],
                                "metadata": mets[i] if i < len(mets) else None,
                                "document": docs[i] if i < len(docs) else None},
                               ensure_ascii=False) + "\n")
    coll_uuid = stores.chroma_collections().get(name)
    seg_id = stores.segment_dir_for_collection(coll_uuid) if coll_uuid else None
    return {"name": name, "collection_id": coll_uuid, "segment_id": seg_id,
            "dir_path": os.path.join(stores.vector_db_dir, seg_id) if seg_id else None,
            "chunk_count": len(ids)}


# --- purge ----------------------------------------------------------------

def purge(stores, classes, opts, export_dir):
    """Execute the purge for the active tier. Safe (no --maint): classes 1, 3, 11.
    Maintenance (--maint): classes 5b, 7, 8. Returns a manifest."""
    manifest = {"ts": opts.ts, "tier": TIER_MAINT if opts.maint else TIER_SAFE,
                "purged_collections": [], "dangling_dirs": [], "kb_vectors": []}
    if opts.maint:
        _purge_maint(stores, classes, manifest)
    else:
        _purge_safe(stores, classes, opts, export_dir, manifest)
    return manifest


def _purge_safe(stores, classes, opts, export_dir, manifest):
    # class 1 ghosts: OWUI REST DELETE, then drop the residual file-{id} collection.
    c1 = classes["ghost_rows"]
    if c1.count:
        log.info("purge ghosts: %d (OWUI REST DELETE + residual file-{id} drop)", c1.count)
    for fid in c1.ids:
        stores.owui_delete_file(fid)
        coll = FILE_COLL_PREFIX + fid
        seg = None
        if opts.backup:
            entry = export_collection(stores, coll, export_dir)
            manifest["purged_collections"].append(entry)
            seg = entry.get("segment_id")
        else:
            seg = stores.segment_dir_for_collection(stores.chroma_collections().get(coll))
        stores.delete_collection(coll)
        if seg:
            stores.rm_segment_dir(seg)

    # class 3 orphan file-{id} collections: export + delete_collection + rm dir.
    c3 = classes["orphan_file_collections"]
    if c3.count:
        log.info("purge orphan file-{id} collections: %d (%d chunks)",
                 c3.count, c3.detail.get("orphan_chunks", 0))
    for name in c3.ids:
        seg = None
        if opts.backup:
            entry = export_collection(stores, name, export_dir)
            manifest["purged_collections"].append(entry)
            seg = entry.get("segment_id")
        else:
            seg = stores.segment_dir_for_collection(stores.chroma_collections().get(name))
        stores.delete_collection(name)
        if seg:
            stores.rm_segment_dir(seg)

    # class 11 dangling segment dirs: rm -rf (record name + size in manifest).
    c11 = classes["dangling_segment_dirs"]
    if c11.count:
        log.info("purge dangling segment dirs: %d (%.1f MB)",
                 c11.count, c11.detail.get("total_bytes", 0) / 1048576.0)
    for d in c11.ids:
        sz = stores.dir_size(d)
        stores.rm_segment_dir(d)
        manifest["dangling_dirs"].append({"dir": d, "size_bytes": sz})


def _purge_maint(stores, classes, manifest):
    # class 5b leaked KB vectors: direct Chroma delete(where) on KB collections.
    c5 = classes["orphan_kb_vectors"]
    leaked_pairs = c5.detail.get("leaked_pairs", []) if c5.detail else []
    if leaked_pairs:
        log.info("purge leaked KB vectors: %d (direct Chroma delete; OWUI stopped)",
                 len(leaked_pairs))
    for kb_id, fid in leaked_pairs:
        deleted = stores.delete_kb_vectors_by_file(kb_id, fid)
        manifest["kb_vectors"].append(
            {"kb_id": kb_id, "file_id": fid, "deleted": deleted})

    # class 7 orphan junction rows: direct sqlite DELETE per orphan file_id.
    c7 = classes["orphan_junction_rows"]
    if c7.count:
        log.info("purge orphan junction rows: %d (sqlite DELETE; OWUI stopped)", c7.count)
    for fid in set(c7.ids):
        stores.delete_junction_by_file(fid)

    # class 8 dead-KB junction rows: direct sqlite DELETE per dead KB id.
    c8 = classes["dead_kb_junction_rows"]
    if c8.count:
        log.info("purge dead-KB junction rows: %d (sqlite DELETE; OWUI stopped)", c8.count)
    for kb_id in set(c8.ids):
        stores.delete_junction_by_knowledge(kb_id)


# --- repair (class 9 stuck-processing-while-linked) -----------------------

def repair(stores, classes):
    """Repair stuck-processing-while-linked files: flip data.status to
    'completed' for files that are linked + have content + have vectors in
    their KB collection + are stale (last write older than REPAIR_STALE_SECS).

    These are files whose embed + link finished but the terminal status write
    failed silently (patch 6 root cause). No reconcile retries a LINKED
    processing file, so they stay "in progress" forever. The gate proves
    completion: vectors + link + content mean the only missing step was the
    swallowed status commit. Status-only repair (File.hash is left as-is; a
    missing hash is benign for a linked member). Maintenance window (OWUI
    stopped). Returns a manifest {repaired, skipped}."""
    manifest = {"repaired": [], "skipped": []}
    c9 = classes["non_completed_leftovers"]
    stuck = (c9.detail or {}).get("stuck_processing_linked", [])
    if not stuck:
        log.info("repair: no stuck-processing-while-linked files")
        return manifest
    now = int(time.time())
    log.info("repair: %d stuck-processing-while-linked candidate(s)", len(stuck))
    for item in stuck:
        fid = item["id"]
        fr = stores.file_rows().get(fid)
        if not fr or fr.status != "processing":
            manifest["skipped"].append({"id": fid, "reason": "status changed (not processing)"})
            continue
        content = stores.file_content(fid)
        if not content:
            manifest["skipped"].append({"id": fid, "reason": "no content (extraction not done)"})
            continue
        updated_at = stores.file_updated_at(fid)
        age = (now - updated_at) if updated_at is not None else None
        if age is not None and age < REPAIR_STALE_SECS:
            manifest["skipped"].append(
                {"id": fid, "reason": "not stale (age %ds < %ds)" % (age, REPAIR_STALE_SECS)})
            continue
        if stores.repair_file_status(fid):
            log.info(
                "repair: set status=completed for %s (was processing; linked, "
                "%d vectors, content %d chars, age %ss)",
                fid, item["vectors"], len(content), age if age is not None else -1)
            manifest["repaired"].append({"id": fid, "filename": fr.filename})
        else:
            manifest["skipped"].append({"id": fid, "reason": "row vanished"})
    log.info("repair done. repaired=%d skipped=%d",
             len(manifest["repaired"]), len(manifest["skipped"]))
    return manifest


# --- main -----------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="KB cross-DB health check (OWUI SQLite + Chroma): audit + purge.")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                    help="OWUI data dir (webui.db + vector_db/); default " + DEFAULT_DATA_DIR)
    ap.add_argument("--owui-base", default=DEFAULT_OWUI_BASE,
                    help="OWUI REST base for ghost DELETE; default " + DEFAULT_OWUI_BASE)
    ap.add_argument("--kb", help="scope the KB-tagged classes to one knowledge_id")
    ap.add_argument("--json", action="store_true", help="machine-readable JSON report")
    ap.add_argument("--show-names", action="store_true",
                    help="include filenames in stdout/JSON (default: ids-only)")
    ap.add_argument("--purge", action="store_true",
                    help="consent to purge (safe classes by default; --maint adds maint classes)")
    ap.add_argument("--maint", action="store_true",
                    help="maintenance window: purge classes 5b,7,8 (OWUI must be stopped)")
    ap.add_argument("--repair", action="store_true",
                    help="maintenance window: repair class-9 stuck-processing-while-linked "
                         "files -> completed (OWUI must be stopped; may combine with --purge)")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the export (backup is ON by default when --purge)")
    args = ap.parse_args(argv)
    args.ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    args.backup = not args.no_backup

    _configure_logging()
    admin_key = os.environ.get("OPENWEBUI_ADMIN_API_KEY") or None
    stores = Stores(args.data_dir, args.owui_base, admin_key)

    log.info("auditing (scope=%s)...", args.kb or "ALL")
    classes = classify(stores, args.kb)

    # prerequisite: ghost purge (safe tier, non-maint) needs the admin key + OWUI up.
    if args.purge and not args.maint and classes["ghost_rows"].count > 0:
        if not admin_key:
            log.error("--purge will delete %d ghost row(s) via OWUI REST, but "
                      "OPENWEBUI_ADMIN_API_KEY is unset.", classes["ghost_rows"].count)
            return 2

    names = {fid: fr.filename for fid, fr in stores.file_rows().items()}

    if not args.purge and not args.repair:
        print(report_json(classes, args.show_names, names) if args.json
              else report_human(classes, args.show_names, names))
        return 0

    export_dir = None
    purge_manifest = None
    if args.purge:
        export_dir = _export_dir(args.data_dir, args.ts) if args.backup else None
        if export_dir:
            log.info("export dir: %s", export_dir)
        log.info("purging (tier=%s, backup=%s)...",
                 TIER_MAINT if args.maint else TIER_SAFE, args.backup)
        purge_manifest = purge(stores, classes, args, export_dir)
        if export_dir:
            with open(os.path.join(export_dir, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(purge_manifest, f, indent=2, ensure_ascii=False)
        log.info("purge done. purged_collections=%d dangling_dirs=%d kb_vectors=%d",
                 len(purge_manifest["purged_collections"]),
                 len(purge_manifest["dangling_dirs"]),
                 len(purge_manifest["kb_vectors"]))

    repair_manifest = None
    if args.repair:
        repair_manifest = repair(stores, classes)

    # re-audit after any action; drop the read cache first so the post-action
    # report reflects the writes (not the pre-action snapshot).
    stores.invalidate()
    classes2 = classify(stores, args.kb)
    if args.json:
        out = json.loads(report_json(classes2, args.show_names, names))
        if purge_manifest is not None:
            out["purge_manifest"] = purge_manifest
        if repair_manifest is not None:
            out["repair_manifest"] = repair_manifest
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(report_human(classes2, args.show_names, names))
        if purge_manifest is not None:
            print("\nPurge manifest:")
            print("  purged collections: %d" % len(purge_manifest["purged_collections"]))
            print("  dangling dirs rm'd: %d" % len(purge_manifest["dangling_dirs"]))
            print("  kb vector deletes:  %d" % len(purge_manifest["kb_vectors"]))
            if export_dir:
                print("  export: %s/manifest.json" % export_dir)
        if repair_manifest is not None:
            print("\nRepair manifest:")
            print("  repaired: %d" % len(repair_manifest["repaired"]))
            for r in repair_manifest["repaired"]:
                print("    %s  %s" % (r["id"], r["filename"]))
            if repair_manifest["skipped"]:
                print("  skipped:  %d" % len(repair_manifest["skipped"]))
                for s in repair_manifest["skipped"]:
                    print("    %s  %s" % (s["id"], s["reason"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)