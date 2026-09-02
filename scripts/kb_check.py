#!/usr/bin/env python3
"""KB cross-DB health check: reconcile OWUI SQLite + the pgvector vector store,
report inconsistencies, and (opt-in) export-then-purge orphans.

The KB stack has two stores that are NOT ACID across each other:
  - OWUI SQLite  (webui.db: file, knowledge_file, knowledge_directory, knowledge).
  - vector store (pgvector only; Chroma was removed): Postgres table
    document_chunk(collection_name, text, vmetadata jsonb, vector).
    collection_name holds the OWUI knowledge_id or `file-{id}`.

Drift this tool detects + clears: ghost rows, orphan file-{id} collections (the
files.py `delete()` no-op leak), orphan KB vectors, dead-KB junction rows, and
more (11 classes). See the class table in the report.

`psycopg2` lives only in the kb-openwebui image, so this tool runs INSIDE that
container via `docker exec -i kb-openwebui python3 - < scripts/kb_check.py`
(host script piped to container python on stdin; opts after `-`). For the
maintenance window (`--maint`) the Makefile stops kb-openwebui and runs a
throwaway container from the same image with the data dir mounted, so direct
vector/SQLite writes are safe (no OWUI process contention); that throwaway
container also joins the owui_net network to reach `postgres`. `psycopg2` is
lazy-imported so the argparse/classify/report logic imports cleanly on the host
for unit tests.

Default = audit + report + advise (zero mutation). `--purge` consents to purge
the safe classes (1, 3); `--purge --maint` purges the maintenance-window
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
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DEFAULT_DATA_DIR = "/app/backend/data"
DEFAULT_OWUI_BASE = "http://127.0.0.1:8080"
WEBUI_DB_FILENAME = "webui.db"
EXPORT_DIRNAME = "check-exports"
FILE_COLL_PREFIX = "file-"
KNOWLEDGE_BASES_COLL = "knowledge-bases"  # OWUI-internal; never treated as a KB
SAMPLE_CAP = 8  # max sample ids/names shown per class in the report

TIER_SAFE = "safe"        # safe while OWUI runs: class 1 (OWUI REST), 3
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
    """pgvector store: vectors + chunk text live in the Postgres
    `document_chunk(collection_name, text, vmetadata jsonb, vector)` table,
    read via a lazy psycopg2 connection. The SQLite webui.db reads, OWUI REST
    delete, and the repair gate are backend-independent.

    `chroma_collections` (the read entry point) returns {collection_name: None}
    over DISTINCT collection_name -- the uuid value is a Chroma concept pgvector
    has none of, so None. pgvector has no on-disk segment dirs, so class 11
    (dangling segment dirs) is N/A and dropped.

    Mutations (delete_collection, delete_kb_vectors_by_file) run only in the
    maintenance window (OWUI stopped). `delete_kb_vectors_by_file` counts
    matching rows BEFORE the DELETE so a zero-delete (wrong filter key/type)
    surfaces in the purge manifest."""

    def __init__(self, data_dir, owui_base, admin_key, pg_dsn=None):
        self.data_dir = data_dir
        self.owui_base = owui_base.rstrip("/")
        self.admin_key = admin_key
        self.webui_db_path = os.path.join(data_dir, WEBUI_DB_FILENAME)
        self._pg_dsn = pg_dsn
        self._pg = None
        self._webui_ro = None
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

    # --- pgvector reads (lazy psycopg2; name->collection over document_chunk) -

    def _pg_conn(self):
        if self._pg is None:
            import psycopg2  # lazy: host has no psycopg2 (unit tests stub this)
            from psycopg2.extras import register_default_jsonb
            self._pg = psycopg2.connect(self._pg_dsn)
            # vmetadata jsonb -> Python dict (NULL stays None).
            register_default_jsonb(self._pg)
        return self._pg

    def _q(self, sql, params=None):
        cur = self._pg_conn().cursor()
        cur.execute(sql, params)
        return cur

    def chroma_collections(self):
        """{collection_name: None} for every DISTINCT collection_name.
        The uuid value is a Chroma concept; pgvector has none, so None.
        (Method name kept for call-site stability; pgvector only.)"""
        if "colls" not in self._cache:
            cur = self._q("SELECT DISTINCT collection_name FROM document_chunk")
            self._cache["colls"] = {r[0]: None for r in cur}
        return self._cache["colls"]

    def collection_count(self, name):
        """Number of chunks in a collection (0 if the collection is gone)."""
        try:
            return self._q(
                "SELECT count(*) FROM document_chunk WHERE collection_name=%s",
                (name,)).fetchone()[0]
        except Exception:
            return 0

    def collection_metadatas(self, name):
        """[vmetadata dict] for every chunk in a collection."""
        try:
            cur = self._q(
                "SELECT vmetadata FROM document_chunk WHERE collection_name=%s",
                (name,))
            return [r[0] for r in cur if r[0] is not None]
        except Exception:
            return []

    def collection_documents(self, name):
        """All chunk ids + texts + metadata for export (read once)."""
        try:
            cur = self._q(
                "SELECT id, text, vmetadata FROM document_chunk "
                "WHERE collection_name=%s", (name,))
            rows = list(cur)
        except Exception:
            return [], [], []
        return ([r[0] for r in rows],
                [r[1] for r in rows],
                [r[2] for r in rows])

    # --- mutating: safe tier (OWUI running) -------------------------------

    def owui_delete_file(self, file_id):
        """OWUI REST DELETE /api/v1/files/{id} at the in-container OWUI base.
        Cleans blob + file row + KB-collection vectors (patch 4). The residual
        file-{id} collection may remain; the caller drops it via
        delete_collection. Raises on non-2xx / missing admin key."""
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
        """DELETE every chunk in a collection (drops the logical collection)."""
        cur = self._pg_conn().cursor()
        cur.execute("DELETE FROM document_chunk WHERE collection_name=%s", (name,))
        self._pg_conn().commit()
        return True

    # --- mutating: maintenance tier (OWUI stopped) ------------------------

    def delete_kb_vectors_by_file(self, kb_name, file_id):
        """DELETE chunks in a KB collection filtered by vmetadata file_id.
        Maintenance window only (OWUI stopped). Counts matching rows BEFORE
        the DELETE so a zero-delete (wrong filter key/type) surfaces in the
        purge manifest. Returns the pre-delete count (int)."""
        cur = self._pg_conn().cursor()
        cur.execute(
            "SELECT count(*) FROM document_chunk WHERE collection_name=%s "
            "AND vmetadata->>'file_id'=%s", (kb_name, file_id))
        n = cur.fetchone()[0]
        cur.execute(
            "DELETE FROM document_chunk WHERE collection_name=%s "
            "AND vmetadata->>'file_id'=%s", (kb_name, file_id))
        self._pg_conn().commit()
        return n

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


# --- backend selection ----------------------------------------------------

def make_stores(data_dir, owui_base, admin_key):
    """Select the vector store. pgvector is the only supported backend; fail
    loud on missing/unknown VECTOR_DB (NO silent default)."""
    vector_db = os.environ.get("VECTOR_DB")
    if vector_db == "pgvector":
        for v in ("PGVECTOR_USER", "PGVECTOR_PASSWORD", "PGVECTOR_DB",
                  "PGVECTOR_DB_URL"):
            if not os.environ.get(v):
                raise RuntimeError(
                    "%s unset (VECTOR_DB=pgvector requires it)" % v)
        return Stores(data_dir, owui_base, admin_key,
                      os.environ["PGVECTOR_DB_URL"])
    raise RuntimeError(
        "VECTOR_DB=%r (must be 'pgvector'; Chroma was removed)" % vector_db)


# --- classification (pure logic over Stores) ------------------------------

def classify(stores, kb=None):
    """Return {class_name: ClassResult} for all 11 classes + a '_totals' dict.
    `kb` scopes the KB-tagged classes to one knowledge_id; classes 3, 12 are
    KB-agnostic (always global)."""
    all_files = stores.file_rows()
    all_junction = stores.junction_rows()
    kb_ids = stores.knowledge_ids()        # {kb_id: name} (live KBs)
    dir_ids = stores.directory_ids()
    colls = stores.chroma_collections()    # {name: uuid}

    all_file_ids = set(all_files)
    all_junction_file_ids = set(j.file_id for j in all_junction)
    live_kb_ids = set(kb_ids)

    # scope: KB-tagged classes use kb_files/kb_junction; classes 3/12 stay global
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
            continue  # live KB but no vector collection
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

    # (class 11, dangling on-disk segment dirs, was Chroma-only: pgvector has no
    # per-collection HNSW dirs, so it is dropped. The document_chunk table is the
    # sole vector store.)

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
    safe = (classes["ghost_rows"].count + classes["orphan_file_collections"].count) > 0
    maint = (classes["orphan_kb_vectors"].detail.get("leaked", 0)
            + classes["orphan_junction_rows"].count
            + classes["dead_kb_junction_rows"].count) > 0
    if safe:
        cmds.append("PURGE=1 make kb-check            # purge safe classes (1,3); backup on")
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
    out.append("  collections:          %d" % t["chroma_collections"])
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
    (chunk count). Captured BEFORE delete_collection."""
    ids, docs, mets = stores.collection_documents(name)
    safe_name = name.replace(os.sep, "_")
    path = os.path.join(export_dir, safe_name + ".jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for i in range(len(ids)):
            f.write(json.dumps({"id": ids[i],
                                "metadata": mets[i] if i < len(mets) else None,
                                "document": docs[i] if i < len(docs) else None},
                               ensure_ascii=False) + "\n")
    return {"name": name, "chunk_count": len(ids)}


# --- purge ----------------------------------------------------------------

def purge(stores, classes, opts, export_dir):
    """Execute the purge for the active tier. Safe (no --maint): classes 1, 3.
    Maintenance (--maint): classes 5b, 7, 8. Returns a manifest."""
    manifest = {"ts": opts.ts, "tier": TIER_MAINT if opts.maint else TIER_SAFE,
                "purged_collections": [], "kb_vectors": []}
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
        if opts.backup:
            entry = export_collection(stores, coll, export_dir)
            manifest["purged_collections"].append(entry)
        stores.delete_collection(coll)

    # class 3 orphan file-{id} collections: export + delete_collection.
    c3 = classes["orphan_file_collections"]
    if c3.count:
        log.info("purge orphan file-{id} collections: %d (%d chunks)",
                 c3.count, c3.detail.get("orphan_chunks", 0))
    for name in c3.ids:
        if opts.backup:
            entry = export_collection(stores, name, export_dir)
            manifest["purged_collections"].append(entry)
        stores.delete_collection(name)


def _purge_maint(stores, classes, manifest):
    # class 5b leaked KB vectors: direct delete on KB collections.
    c5 = classes["orphan_kb_vectors"]
    leaked_pairs = c5.detail.get("leaked_pairs", []) if c5.detail else []
    if leaked_pairs:
        log.info("purge leaked KB vectors: %d (direct delete; OWUI stopped)",
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


# --- BM25 release gate (patch 10) ------------------------------------------
# The OWUI hybrid_search FTS arm swallows every paradedb error in a trailing
# `except -> return None`, which falls back to the legacy langchain
# BM25Retriever (a per-request full-collection load + in-process BM25 build
# under the 60s retrieve timeout). A missing pg_search preload, a dropped
# extension, or a missing idx_document_chunk_bm25 index otherwise silently
# degrades every query with no error anywhere. This gate is the only detector.
# Run as a release gate: `make kb-bm25-check` (or `kb_check.py --bm25-gate`).
# A red probe = do not ship. Probes:
#   1. pg_search extension present.
#   2. idx_document_chunk_bm25 index present.
#   3. ranking path: a collection-scoped `text ||| :q` + pdb.score + ORDER BY
#      executes without error (a missing index makes pdb.score error too).
#   4. colon/dash-safe: `text ||| 'error: x'` executes without error (the C1
#      silent-zero class for the ||| operator -- it must NOT error on colons).
#   5. zero-token: `text ||| '???'` -> 0 rows, no error.
# Probes 3-5 bind the query as a parameter (production binds through psycopg2,
# not SQL literals). A 0-row execution is green -- the gate detects a
# broken/missing index, which errors regardless of row count; recall is verified
# by test_09/test_11, not here.

_BM25_PROBE_Q = "kb_check_probe_token"  # synthetic; not expected in the corpus


def bm25_gate(stores):
    """Run the patch-10 BM25 release-gate probes. Returns a list of
    (name, ok, detail); ok=False on any broken/missing component."""
    results = []

    def probe(name, fn):
        try:
            results.append((name, True, fn()))
        except Exception as e:  # gate must capture every failure, never raise
            results.append((name, False, "%s: %s" % (type(e).__name__, e)))

    # 1. pg_search extension present.
    def _ext():
        n = stores._q("SELECT count(*) FROM pg_extension "
                      "WHERE extname='pg_search'").fetchone()[0]
        if n != 1:
            raise RuntimeError(
                "pg_search extension not installed (count=%d); "
                "run make kb-bm25-init" % n)
        return "installed"
    probe("pg_search extension", _ext)

    # 2. idx_document_chunk_bm25 index present.
    def _idx():
        n = stores._q(
            "SELECT count(*) FROM pg_indexes WHERE schemaname='public' "
            "AND indexname='idx_document_chunk_bm25'").fetchone()[0]
        if n != 1:
            raise RuntimeError(
                "idx_document_chunk_bm25 missing; run make kb-bm25-init")
        return "present"
    probe("idx_document_chunk_bm25", _idx)

    # A collection name to scope the ranking-path query (any; 0 rows is green).
    colls = list(stores.chroma_collections())
    coll = colls[0] if colls else "__kb_check_no_such_collection__"

    # 3. ranking path: ||| + pdb.score + ORDER BY (the production FTS path).
    def _rank():
        cur = stores._q(
            "SELECT id, pdb.score(id) AS s FROM document_chunk "
            "WHERE collection_name=%s AND text ||| %s "
            "ORDER BY pdb.score(id) DESC LIMIT 5",
            (coll, _BM25_PROBE_Q))
        return "%d rows" % len(cur.fetchall())
    probe("ranking path (||| + pdb.score + ORDER BY)", _rank)

    # 4. colon/dash-safe: a query with a colon must NOT error (C1 class).
    def _colon():
        cur = stores._q(
            "SELECT count(*) FROM document_chunk "
            "WHERE collection_name=%s AND text ||| %s",
            (coll, "error: kb_check_probe"))
        return "%d rows" % cur.fetchone()[0]
    probe("colon/dash-safe (||| 'error: x')", _colon)

    # 5. zero-token: ??? -> 0 rows, no error.
    def _zero():
        cur = stores._q(
            "SELECT count(*) FROM document_chunk "
            "WHERE collection_name=%s AND text ||| %s",
            (coll, "???"))
        n = cur.fetchone()[0]
        if n != 0:
            raise RuntimeError("expected 0 rows for '???', got %d" % n)
        return "0 rows"
    probe("zero-token (||| '???')", _zero)

    return results


# --- main -----------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="KB cross-DB health check (OWUI SQLite + pgvector vector "
                    "store): audit + purge.")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                    help="OWUI data dir (webui.db); default " + DEFAULT_DATA_DIR)
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
    ap.add_argument("--bm25-gate", action="store_true",
                    help="run ONLY the patch-10 BM25 release-gate probes "
                         "(pg_search extension + idx_document_chunk_bm25 index + "
                         "the ||| / pdb.score ranking path + colon-safe + "
                         "zero-token); skip the class audit. Exit 0 if all green, "
                         "1 if any red. A red probe = do not ship (a broken/missing "
                         "index silently degrades every query to the langchain "
                         "full-collection fallback).")
    args = ap.parse_args(argv)
    args.ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    args.backup = not args.no_backup

    _configure_logging()
    admin_key = os.environ.get("OPENWEBUI_ADMIN_API_KEY") or None
    try:
        stores = make_stores(args.data_dir, args.owui_base, admin_key)
    except RuntimeError as e:
        log.error("%s", e)
        return 2

    if args.bm25_gate:
        log.info("BM25 release gate (patch 10)...")
        results = bm25_gate(stores)
        green = all(ok for _, ok, _ in results)
        for name, ok, detail in results:
            print("%s  %-40s %s" % ("OK  " if ok else "FAIL", name, detail))
        if not green:
            log.error("BM25 gate RED -- do not ship "
                      "(run make kb-bm25-init, then re-run).")
        return 0 if green else 1

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
        log.info("purge done. purged_collections=%d kb_vectors=%d",
                 len(purge_manifest["purged_collections"]),
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