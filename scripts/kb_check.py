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
more (12 classes). See the class table in the report.

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
the safe class (3); `--purge --maint` purges the maintenance-window
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

TIER_SAFE = "safe"        # safe while OWUI runs: class 3
TIER_MAINT = "maint"      # maintenance window only: class 5b, 7, 8
TIER_ADVISORY = "advisory"  # report + advise only, never auto-purged

# Repair gate (class 9 stuck-processing-while-linked subset): a file is only
# repaired to 'completed' if its last DB write is older than this. Excludes the
# ms-wide race where a genuinely-in-flight process_file is between its last
# pre-terminal write and the terminal status commit. 60s is safe -- a normal
# terminal commit lands in ms; only a stuck file is older than this.
REPAIR_STALE_SECS = 60

log = logging.getLogger("kb-check")


def _parse_kb_source(desc):
    """Parse the KB source attribute from the `description` string.

    The source attribute lives in the writable `description` as
    `<prose lead> | <kv>` (OWUI's REST API cannot write the `meta` JSONB field).
    kv-order-agnostic: split on `|`, read each `k=v`, ignore non-kv prose. A
    `source=` kv is authoritative. Else prefix-detect the legacy prose lead:
    `Indexed from local root/` (new-migration) or `Indexed from local <name>/`
    (pre-migration, no `root/`) -> `root`; `Claude projects memory` ->
    `projects-memory`; else `unknown`. Mirrors `_parse_kb_desc` in
    skills/claude/scripts/kb.py (kept local here: kb.py lives in the skill, not
    in the container this tool runs in)."""
    d = desc or ""
    kv = {}
    for tok in d.split("|"):
        tok = tok.strip()
        if "=" in tok:
            k, _, v = tok.partition("=")
            kv[k.strip()] = v.strip()
    if "source" in kv:
        return kv["source"]
    if d.startswith("Indexed from local root/"):
        return "root"
    if d.startswith("Indexed from local "):
        return "root"
    if d.startswith("Claude projects memory"):
        return "projects-memory"
    return "unknown"


def _parse_kb_path(desc):
    """Parse the `path` kv from the KB description -- the source dir name for a
    source=root KB. Returns None when absent (a legacy description without the
    path kv; the caller falls back to the KB name). The `path` kv is immutable
    through an OWUI UI KB rename (`PUT /knowledge/{id}/update` changes `name`
    but preserves `description`), so it is the rename-safe identity for the
    class-11 stale check."""
    d = desc or ""
    for tok in d.split("|"):
        tok = tok.strip()
        if "=" in tok:
            k, _, v = tok.partition("=")
            if k.strip() == "path":
                return v.strip() or None
    return None


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

    def knowledge_rows(self):
        """[(kb_id, name, description)] for every row in the knowledge table.
        The `description` carries the source-attribute kv (parsed to identify
        root-backed KBs for the stale-root-KB check)."""
        if "kb_rows" not in self._cache:
            out = []
            for r in self._webui().execute(
                    "SELECT id, name, description FROM knowledge"):
                out.append((r["id"], r["name"], r["description"] or ""))
            self._cache["kb_rows"] = out
        return self._cache["kb_rows"]

    def kb_in_flight(self, kb_id):
        """Count of a KB's file rows with status pending/processing (a drain is
        in flight). Used by the prune path as a TOCTOU re-check immediately
        before export+delete. Reads via a FRESH connection (not the cached
        file_rows snapshot, and not the long-lived _webui() RO connection whose
        snapshot is frozen at its first read) so a drain started AFTER the
        classify snapshot IS detected."""
        con = sqlite3.connect("file:%s?mode=ro" % self.webui_db_path, uri=True)
        try:
            cur = con.execute(
                "SELECT count(*) FROM file "
                "WHERE json_extract(meta, '$.data.knowledge_id') = ? "
                "AND json_extract(data, '$.status') "
                "IN ('pending', 'processing')",
                (kb_id,))
            return cur.fetchone()[0]
        finally:
            con.close()

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

    def collection_count(self, name):
        """Live chunk count for a collection (strict: raises on DB error, unlike
        collection_documents which swallows them). Used by export_collection_strict
        to verify the backup captured every row."""
        cur = self._q(
            "SELECT count(*) FROM document_chunk WHERE collection_name=%s", (name,))
        return cur.fetchone()[0]

    def export_collection_strict(self, name, export_dir):
        """Strict backup of a collection's chunks before a prune DELETE. Unlike
        export_collection (which feeds the fail-open collection_documents that
        swallows DB errors -> empty lists), this raises on any query error and
        asserts the exported row count == the live count, so a pgvector read
        failure can NEVER produce an empty-looking backup followed by a delete.
        Returns a manifest entry {name, chunk_count, path}."""
        cur = self._q(
            "SELECT id, text, vmetadata FROM document_chunk WHERE collection_name=%s",
            (name,))
        rows = list(cur)
        safe_name = name.replace(os.sep, "_")
        path = os.path.join(export_dir, safe_name + ".jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({"id": r[0], "metadata": r[2],
                                    "document": r[1]}, ensure_ascii=False) + "\n")
        live = self.collection_count(name)
        if len(rows) != live:
            raise RuntimeError(
                "strict export count mismatch for %s: wrote %d, live count %d "
                "-- aborting (concurrent write or DB error)" % (name, len(rows), live))
        return {"name": name, "chunk_count": len(rows), "path": path}

    def owui_delete_kb(self, kb_id):
        """OWUI REST DELETE /api/v1/knowledge/{id}/delete at the in-container OWUI
        base. The route returns the DB-delete result as a JSON bool (HTTP 200 +
        body `false` if the row delete fails), so a plain status check is NOT
        enough -- require body == true. OWUI wraps its vector delete_collection
        in try/except: pass, so a vector-cleanup failure is silently swallowed
        by OWUI; the KB-row delete (body `true`) is what we require, and any
        residual vectors surface as class 5b on the next kb-check. Raises on
        non-200, body != true, or missing admin key."""
        if not self.admin_key:
            raise RuntimeError("OPENWEBUI_ADMIN_API_KEY unset (prune needs it)")
        url = "%s/api/v1/knowledge/%s/delete" % (self.owui_base, kb_id)
        req = urllib.request.Request(url, method="DELETE",
                                     headers={"Authorization": "Bearer " + self.admin_key})
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status >= 300:
                raise RuntimeError("OWUI DELETE KB %s -> HTTP %d" % (kb_id, resp.status))
            body = resp.read().decode().strip()
        if body != "true":
            raise RuntimeError("OWUI DELETE KB %s -> body %r (row not deleted)"
                               % (kb_id, body[:64]))
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

def classify(stores, kb=None, root_dirs=None):
    """Return {class_name: ClassResult} for all 12 classes + a '_totals' dict.
    `kb` scopes the KB-tagged classes to one knowledge_id; classes 3, 11, 12 are
    KB-agnostic (always global). `root_dirs` (set of ./root/<name> top dirs, or
    None to skip) drives class 11 (stale root KBs)."""
    all_files = stores.file_rows()
    all_junction = stores.junction_rows()
    kb_ids = stores.knowledge_ids()        # {kb_id: name} (live KBs)
    dir_ids = stores.directory_ids()
    colls = stores.chroma_collections()    # {name: uuid}

    all_file_ids = set(all_files)
    all_junction_file_ids = set(j.file_id for j in all_junction)
    # Per-KB junction membership, built from the GLOBAL junction (all_junction,
    # not the scoped kb_junction) so a cross-KB leak is detected regardless of
    # a --kb scope (a file linked to K2 but leaked into K1 is flagged in K1).
    kb_junction_file_ids_by_kb = {}
    for j in all_junction:
        kb_junction_file_ids_by_kb.setdefault(j.knowledge_id, set()).add(j.file_id)
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

    # 1. ghost rows: completed + knowledge_id set, NOT in the GLOBAL junction.
    # Global (not the scoped junction) so a --kb scope does not flag a file that
    # is a live member of a different KB (matches classes 5/7). Advisory, not
    # purgeable: /knowledge/{id}/file/remove?delete_file=false leaves
    # meta.data.knowledge_id + status=completed after unlinking the junction, and
    # the upload path sets status=completed before the junction insert. Both read
    # as "ghost" but are legitimate; a purge would delete a live file (blob + row
    # + vectors, irreversible).
    c1_ids = [fid for fid, fr in kb_files.items()
              if fr.status == "completed" and fr.knowledge_id
              and fid not in all_junction_file_ids]
    classes["ghost_rows"] = ClassResult(c1_ids, TIER_ADVISORY)

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
    #    the knowledge table (excludes knowledge-bases). A vector is flagged when
    #    its metadata has a file_id key. 5a = file_id has a DB row but is not
    #    linked to THIS kb (ghost link; per-KB membership, not the global junction,
    #    so a cross-KB leak -- a file linked to K2 but leaked into K1 -- is caught;
    #    5a is report-only, never purged). 5b = file_id has no DB row (leaked KB
    #    vectors; maint purge), flagged regardless of any junction row (a junction
    #    row for a gone file is a class-7 orphan, not a mask -- was: the global
    #    `fid in all_junction_file_ids` test masked same-KB 5b when the fid also
    #    had a class-7 orphan junction row, so a 2nd MAINT pass was needed). Per-
    #    file dedup via `seen` (was: one c5_ids entry per chunk -- a 312-chunk file
    #    yielded 312 entries + 311 self-inflicted zero-deletes, corrupting the
    #    zero-delete manifest guard).
    c5_ids = []
    c5a = c5b = 0
    leaked_pairs = []  # (kb_id, file_id) for maint purge, deduped per file
    for kb_id in scan_kb_ids:
        if kb_id not in colls:
            continue  # live KB but no vector collection
        seen = set()  # per-file dedup within this kb collection
        for md in stores.collection_metadatas(kb_id):
            fid = md.get("file_id") if isinstance(md, dict) else None
            if not fid or fid in seen:
                continue  # require the file_id key (excludes knowledge-bases rows)
            seen.add(fid)
            if fid in all_file_ids:
                if fid in kb_junction_file_ids_by_kb.get(kb_id, ()):
                    continue  # linked to this kb
                c5a += 1
            else:
                c5b += 1
                leaked_pairs.append((kb_id, fid))
            c5_ids.append(fid)
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
    #    not class 7) with 0 vectors for that file_id in its KB collection. A live
    #    KB whose vector collection is entirely gone (total vector loss) is
    #    represented as an empty count map so its completed linked files flag --
    #    was: skipped -> every /query/collection returned 200 + empty while class 6
    #    reported clean (silent). (A reindex deletes-then-rebuilds the whole
    #    collection, so a transient false positive during a reindex drain is
    #    possible; class 6 is advisory, so this is noise, not data loss.)
    kb_vector_file_counts = {}  # kb_id -> {file_id: count}; {} = collection gone
    for kb_id in scan_kb_ids:
        counts = {}
        if kb_id in colls:
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
        counts = kb_vector_file_counts.get(j.knowledge_id, {})
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

    # 10. idempotency-key duplicates: groups on the idempotency IDENTITY
    #    (knowledge_id, directory_id, filename) -- the match key in
    #    apply_upload_idempotency.py. file_hash only chooses reuse-vs-reclaim, so
    #    a 4-tuple key (with any hash) MISSES the residue of a failed reclaim (two
    #    rows, same name/KB/dir, different file_hash). The 3-tuple catches it; the
    #    differing file_hash values are recorded in detail.
    groups = {}
    for fid, fr in kb_files.items():
        if not fr.knowledge_id:
            continue
        key = (fr.knowledge_id, fr.directory_id or "", fr.filename)
        groups.setdefault(key, []).append(fid)
    c10_ids = [fid for fids in groups.values() if len(fids) > 1 for fid in fids]
    c10_detail = [{"key": list(k), "file_ids": fids,
                   "file_hashes": [kb_files[f].file_hash for f in fids]}
                  for k, fids in groups.items() if len(fids) > 1]
    classes["idempotency_duplicates"] = ClassResult(
        c10_ids, TIER_ADVISORY, {"dup_groups": c10_detail})

    # 11. stale root KBs: a source=root KB whose ./root/<path>/ dir is gone. The
    # source + path attributes live in the KB description kv (parsed by
    # _parse_kb_source / _parse_kb_path); the path kv is rename-safe (an OWUI UI
    # KB rename changes `name` but not `description`), so the stale test uses path
    # (name is the legacy fallback when path is absent). Only source=root KBs are
    # root-backed (source=projects-memory KBs are backed by ~/.claude/projects/,
    # never stale here; source=unknown is fail-safe-not-stale). KB-agnostic
    # (global; orthogonal to the KB=<id> vector-store scope). root_dirs is None ->
    # check skipped (the Makefile always passes it; a direct kb_check.py run
    # without --root-dirs skips gracefully). root_dirs may be an empty set
    # (./root/ exists but has no children -> every source=root KB is stale). ids
    # are kb_id STRINGS (report joins them as strings); the (id, name) pairs live
    # in detail for the prune path + SHOW_NAMES rendering.
    if root_dirs is None:
        classes["stale_root_kb"] = ClassResult(
            [], TIER_ADVISORY, {"skipped": True})
    else:
        stale = []
        for kid, name, desc in stores.knowledge_rows():
            if _parse_kb_source(desc) != "root":
                continue
            # path kv is authoritative + rename-safe; fall back to name only for
            # legacy descriptions that lack the path kv (preserves prior behavior).
            key = _parse_kb_path(desc) or name
            if key not in root_dirs:
                stale.append((kid, name))
        classes["stale_root_kb"] = ClassResult(
            [kid for kid, _ in stale], TIER_ADVISORY, {"stale_kbs": stale})

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
    ("stale_root_kb", "11"),
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
    safe = classes["orphan_file_collections"].count > 0
    maint = (classes["orphan_kb_vectors"].detail.get("leaked", 0)
            + classes["orphan_junction_rows"].count
            + classes["dead_kb_junction_rows"].count) > 0
    if safe:
        cmds.append("PURGE=1 make kb-check            # purge class 3 (orphan file-{id} collections); backup on")
        cmds.append("PURGE=1 BACKUP=0 make kb-check   # purge class 3, no backup export")
    if maint:
        cmds.append("PURGE=1 MAINT=1 make kb-check    # maintenance window: stop OWUI, purge 5b,7,8")
    stuck = (classes["non_completed_leftovers"].detail or {}).get("stuck_processing_linked", [])
    if stuck:
        cmds.append("REPAIR=1 make kb-check          # stop OWUI, repair stuck-processing-while-linked -> completed")
    stale = classes["stale_root_kb"]
    if stale.detail and not stale.detail.get("skipped") and stale.count > 0:
        cmds.append("PRUNE_KB=1 make kb-check        # delete stale root KBs (backup first; irreversible w/o source dir)")
    if not safe and not maint and not stuck and not (stale.count > 0):
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
        if name == "stale_root_kb" and c.detail:
            if c.detail.get("skipped"):
                out.append("      skipped (pass --root-dirs to enable)")
            elif c.detail.get("stale_kbs"):
                _names = ", ".join("%s(%s)" % (n, kid)
                                   for kid, n in c.detail["stale_kbs"][:SAMPLE_CAP])
                out.append("      stale root KBs: %s" % _names)
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
    """Execute the purge for the active tier. Safe (no --maint): class 3.
    Maintenance (--maint): classes 5b, 7, 8. Returns a manifest."""
    manifest = {"ts": opts.ts, "tier": TIER_MAINT if opts.maint else TIER_SAFE,
                "purged_collections": [], "kb_vectors": []}
    if opts.maint:
        _purge_maint(stores, classes, manifest)
    else:
        _purge_safe(stores, classes, opts, export_dir, manifest)
    return manifest


def _purge_safe(stores, classes, opts, export_dir, manifest):
    # class 3 orphan file-{id} collections: export + delete_collection. (Class 1
    # ghosts are advisory -- not purged; see classify().)
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


# --- prune stale root KBs (class 11; --prune-kb) --------------------------
# Separate from purge(): a dedicated PRUNE_KB=1 flag, NOT a purge tier, so the
# routine PURGE (orphan-vector cleanup) never deletes a whole KB. Destructive:
# the KB + its index are gone (re-indexing needs the source dir, which is gone).
# A timestamped backup is MANDATORY (export_collection_strict: no fail-open).
# Per-KB in-flight guard: refuse if a drain is running for that KB (TOCTOU
# re-check from SQLite, not the classify snapshot). OWUI must be running (REST
# DELETE); the Makefile rejects PRUNE_KB with MAINT/REPAIR (which stop OWUI).

def prune_stale_kbs(stores, classes, export_dir):
    """Delete every class-11 stale root KB: strict backup -> in-flight re-check
    -> OWUI REST DELETE. Returns a manifest {pruned_kbs, skipped_in_flight}."""
    manifest = {"pruned_kbs": [], "skipped_in_flight": []}
    stale = (classes["stale_root_kb"].detail or {}).get("stale_kbs", [])
    if not stale:
        log.info("prune stale root KBs: 0 (none stale)")
        return manifest
    log.info("prune stale root KBs: %d (backup=%s)", len(stale), export_dir)
    for kb_id, name in stale:
        # 1. in-flight guard (TOCTOU re-check; never reuse the classify snapshot).
        inflight = stores.kb_in_flight(kb_id)
        if inflight > 0:
            log.warning("SKIP stale root KB %s (%s): %d file(s) pending/processing "
                        "(a drain is in flight); wait: make kb-status KB=%s",
                        name, kb_id, inflight, name)
            manifest["skipped_in_flight"].append(
                {"kb_id": kb_id, "name": name, "pending": inflight})
            continue
        # 2. mandatory strict backup (raises on DB error / count mismatch -> abort
        # this KB; owui_delete_kb is NOT called).
        entry = stores.export_collection_strict(kb_id, export_dir)
        # 3. OWUI REST DELETE (requires body == true).
        stores.owui_delete_kb(kb_id)
        log.info("PRUNED stale root KB %s (%s) chunks=%d backup=%s",
                 name, kb_id, entry["chunk_count"], entry["path"])
        manifest["pruned_kbs"].append(
            {"kb_id": kb_id, "name": name,
             "chunk_count": entry["chunk_count"], "backup": entry["path"]})
    return manifest


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
        if updated_at is None:
            manifest["skipped"].append(
                {"id": fid, "reason": "updated_at missing (cannot prove stale)"})
            continue
        age = now - updated_at
        if age < REPAIR_STALE_SECS:
            manifest["skipped"].append(
                {"id": fid, "reason": "not stale (age %ds < %ds)" % (age, REPAIR_STALE_SECS)})
            continue
        if stores.repair_file_status(fid):
            log.info(
                "repair: set status=completed for %s (was processing; linked, "
                "%d vectors, content %d chars, age %ss)",
                fid, item["vectors"], len(content), age)
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
#   6. (patch 11) DSL phrase path: `id @@@ parse_with_field('text', :q,
#      lenient => false)` + pdb.score + ORDER BY executes without error.
#   7. (patch 11) malformed-DSL-raises: an unmatched `"` MUST raise
#      (lenient => false); not raising is the C1 silent-zero regression.
# Probes 3-7 bind the query as a parameter (production binds through psycopg2,
# not SQL literals). A 0-row execution is green -- the gate detects a
# broken/missing index or predicate, which errors regardless of row count;
# recall is verified by test_09/test_11, not here.

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

    # Patch-11 lexical-dsl probes (same BM25 index, same env). DB-level only --
    # the end-to-end sentinel-agreement (HTTP 400) is covered by test_11's
    # direct-OWUI contract (the release-gate env has no OWUI user key).
    results.extend(lexical_dsl_gate(stores, coll))

    return results


# --- lexical-dsl release gate (patch 11) -----------------------------------
# Patch 11 adds the `lexical-dsl` mode: a sentinel-prefixed query branch runs
# `paradedb.parse_with_field('text', :q, lenient => false)` (Tantivy DSL). The
# critical property is C1: `lenient => false` RAISES on bad syntax (unlike
# patch-10's `|||`, which is colon/dash/quote-safe). A broken predicate or a
# `lenient => true` regression would silently zero or swallow -- this gate is
# the only DB-level detector. Probes (bound params, like the ||| probes):
#   6. DSL phrase path: `id @@@ parse_with_field` + pdb.score + ORDER BY
#      executes without error (0 rows is green -- the gate detects a broken
#      predicate, which errors regardless of row count).
#   7. malformed raises: an unmatched `"` MUST raise (lenient => false). Wrapped
#      in try/except + rollback so the aborted transaction does not red-cascade
#      every later _q in the run (InFailedSqlTransaction). NOT raising is the
#      C1 regression (red).

_DSL_PROBE_PHRASE = '"kb check probe phrase"'  # valid phrase; not in the corpus


def lexical_dsl_gate(stores, coll):
    """Run the patch-11 lexical-dsl DB-level release-gate probes. Returns a list
    of (name, ok, detail); ok=False if the predicate errors or a malformed query
    does NOT raise (the C1 regression)."""
    results = []

    def probe(name, fn):
        try:
            results.append((name, True, fn()))
        except Exception as e:  # gate must capture every failure, never raise
            results.append((name, False, "%s: %s" % (type(e).__name__, e)))

    # 6. DSL phrase path: @@@ parse_with_field + pdb.score + ORDER BY.
    def _phrase():
        cur = stores._q(
            "SELECT id, pdb.score(id) AS s FROM document_chunk "
            "WHERE collection_name=%s "
            "  AND id @@@ paradedb.parse_with_field('text', %s, lenient => false) "
            "ORDER BY pdb.score(id) DESC LIMIT 5",
            (coll, _DSL_PROBE_PHRASE))
        return "%d rows" % len(cur.fetchall())
    probe("DSL phrase path (@@@ parse_with_field + pdb.score)", _phrase)

    # 7. malformed DSL MUST raise (lenient => false). Rollback after so the
    # aborted transaction does not poison later probes.
    def _malformed():
        try:
            stores._q(
                "SELECT count(*) FROM document_chunk "
                "WHERE collection_name=%s "
                "  AND id @@@ paradedb.parse_with_field('text', %s, lenient => false)",
                (coll, '"unmatched'))
        except Exception:
            stores._pg_conn().rollback()  # clear the aborted txn
            return "raised (lenient => false)"
        stores._pg_conn().rollback()
        raise RuntimeError(
            "malformed DSL did NOT raise; lenient => false expected a parse "
            "error (the C1 silent-zero regression)")
    probe("malformed DSL raises (lenient => false)", _malformed)

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
    ap.add_argument("--root-dirs", default=None,
                    help="JSON array of ./root/<name> top dirs (host-computed); "
                         "drives the class-11 stale-root-KB check. None (omit) skips "
                         "class 11; [] means ./root/ has no children (every "
                         "source=root KB is stale).")
    ap.add_argument("--prune-kb", action="store_true",
                    help="delete stale root KBs (class 11) via OWUI REST "
                         "DELETE /knowledge/{id}/delete; always backs up first "
                         "(strict, mandatory); needs OWUI running + admin key; "
                         "separate from --purge (orphan-vector cleanup).")
    ap.add_argument("--bm25-gate", action="store_true",
                    help="run ONLY the BM25 release-gate probes (patch 10 + "
                         "patch 11): pg_search extension + idx_document_chunk_bm25 "
                         "index + the ||| / pdb.score ranking path + colon-safe + "
                         "zero-token + the lexical-dsl @@@ parse_with_field phrase "
                         "path + malformed-DSL-raises (lenient => false); skip the "
                         "class audit. Exit 0 if all green, 1 if any red. A red "
                         "probe = do not ship (a broken/missing index silently "
                         "degrades every query to the langchain full-collection "
                         "fallback).")
    args = ap.parse_args(argv)
    args.ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    args.backup = not args.no_backup

    # --prune-kb validations: backup is mandatory; OWUI must be running (so it
    # is incompatible with --maint/--repair, which stop OWUI). The Makefile also
    # enforces these; defend here for direct kb_check.py runs.
    if args.prune_kb and args.no_backup:
        log.error("--prune-kb requires a backup (incompatible with --no-backup).")
        return 2
    if args.prune_kb and (args.maint or args.repair):
        log.error("--prune-kb needs OWUI running (incompatible with --maint/--repair).")
        return 2

    _configure_logging()
    admin_key = os.environ.get("OPENWEBUI_ADMIN_API_KEY") or None
    if args.prune_kb and not admin_key:
        log.error("--prune-kb needs OPENWEBUI_ADMIN_API_KEY (OWUI REST DELETE).")
        return 2
    try:
        stores = make_stores(args.data_dir, args.owui_base, admin_key)
    except RuntimeError as e:
        log.error("%s", e)
        return 2

    if args.bm25_gate:
        log.info("BM25 release gate (patch 10 + patch 11)...")
        results = bm25_gate(stores)
        green = all(ok for _, ok, _ in results)
        for name, ok, detail in results:
            print("%s  %-40s %s" % ("OK  " if ok else "FAIL", name, detail))
        if not green:
            log.error("BM25 gate RED -- do not ship "
                      "(run make kb-bm25-init, then re-run).")
        return 0 if green else 1

    root_dirs = None
    if args.root_dirs is not None:
        try:
            root_dirs = set(json.loads(args.root_dirs))
        except (ValueError, TypeError) as e:
            log.error("bad --root-dirs JSON: %s", e)
            return 2

    log.info("auditing (scope=%s)...", args.kb or "ALL")
    classes = classify(stores, args.kb, root_dirs=root_dirs)

    names = {fid: fr.filename for fid, fr in stores.file_rows().items()}
    # class-11 stale KB ids -> KB names for SHOW_NAMES rendering (ids are kb_ids,
    # not file ids; _fmt_samples joins them as strings via this map).
    for _kid, _kname in (classes["stale_root_kb"].detail or {}).get("stale_kbs", []):
        names[_kid] = _kname

    if not args.purge and not args.repair and not args.prune_kb:
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

    prune_manifest = None
    prune_export_dir = None
    if args.prune_kb:
        # backup is mandatory for prune (validated above: --no-backup rejected).
        prune_export_dir = _export_dir(args.data_dir, args.ts)
        log.info("prune export dir: %s", prune_export_dir)
        prune_manifest = prune_stale_kbs(stores, classes, prune_export_dir)
        with open(os.path.join(prune_export_dir, "prune-manifest.json"), "w",
                  encoding="utf-8") as f:
            json.dump(prune_manifest, f, indent=2, ensure_ascii=False)
        log.info("prune done. pruned_kbs=%d skipped_in_flight=%d",
                 len(prune_manifest["pruned_kbs"]),
                 len(prune_manifest["skipped_in_flight"]))

    # re-audit after any action; drop the read cache first so the post-action
    # report reflects the writes (not the pre-action snapshot).
    stores.invalidate()
    classes2 = classify(stores, args.kb, root_dirs=root_dirs)
    if args.json:
        out = json.loads(report_json(classes2, args.show_names, names))
        if purge_manifest is not None:
            out["purge_manifest"] = purge_manifest
        if repair_manifest is not None:
            out["repair_manifest"] = repair_manifest
        if prune_manifest is not None:
            out["prune_manifest"] = prune_manifest
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
        if prune_manifest is not None:
            print("\nPrune manifest:")
            print("  pruned stale root KBs: %d" % len(prune_manifest["pruned_kbs"]))
            for r in prune_manifest["pruned_kbs"]:
                print("    %s  %s  chunks=%d  backup=%s"
                      % (r["kb_id"], r["name"], r["chunk_count"], r["backup"]))
            if prune_manifest["skipped_in_flight"]:
                print("  skipped (in-flight drain): %d"
                      % len(prune_manifest["skipped_in_flight"]))
                for s in prune_manifest["skipped_in_flight"]:
                    print("    %s  %s  pending=%d"
                          % (s["kb_id"], s["name"], s["pending"]))
            if prune_export_dir:
                print("  export: %s/prune-manifest.json" % prune_export_dir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)