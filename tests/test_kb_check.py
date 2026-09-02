#!/usr/bin/env python3
"""Unit tests for scripts/kb_check.py (KB cross-DB health check).

The classify/report/purge logic is pure over a `Stores` interface; `psycopg2`
is lazy-imported so the tool imports cleanly on the host. These tests feed an
in-memory `FakeStores` (no real DBs, no network, no gdrive — see
[[tests-use-fixtures-not-gdrive]]) and assert: every one of the 11 classes is
detected with the correct counts; `knowledge-bases` is never flagged (blocker 1);
class 5 requires a `file_id` metadata key; class 7 is subtracted before class 6
(blocker 7); dead-KB junction rows are flagged (class 8); `--kb` scopes the
KB-tagged classes; `--purge` drops only the right tier and calls OWUI DELETE for
ghosts; the export is written iff backup is on; stdout is ids-only unless
`--show-names`; JSON is valid.

Run:  python3 tests/test_kb_check.py -v
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import kb_check as kc  # noqa: E402


# --- FakeStores (in-memory; overrides every Stores read + mutate method) --

class FakeStores(kc.Stores):
    """In-memory Stores. `data_dir` is a real temp dir so export_collection can
    write the JSONL + manifest there. Mutations record into lists (no real
    Postgres/SQLite/OWUI is touched)."""

    def __init__(self, data_dir, files, junction, kb_ids, dir_ids, colls,
                 coll_meta, coll_docs, content=None, updated_at=None,
                 admin_key="ADM"):
        super().__init__(data_dir, "http://owui", admin_key)
        self._files = files
        self._junction = junction
        self._kb_ids = kb_ids
        self._dir_ids = dir_ids
        self._colls = colls                # {name: uuid}
        self._coll_meta = coll_meta        # {name: [metadata dicts]}
        self._coll_docs = coll_docs         # {name: (ids, docs, mets)}
        # repair-gate data (lazy reads in the real Stores; in-memory here)
        self._content = content or {}       # {file_id: content str}
        self._updated_at = updated_at or {}  # {file_id: epoch seconds}
        # mutation record
        self.owui_deletes = []
        self.deleted_collections = []
        self.deleted_kb_vectors = []
        self.deleted_junction_files = []
        self.deleted_junction_kbs = []
        self.repaired = []

    # reads
    def file_rows(self):
        return dict(self._files)

    def junction_rows(self):
        return list(self._junction)

    def knowledge_ids(self):
        return dict(self._kb_ids)

    def directory_ids(self):
        return set(self._dir_ids)

    def chroma_collections(self):
        return dict(self._colls)

    def collection_count(self, name):
        return len(self._coll_meta.get(name, []))

    def collection_metadatas(self, name):
        return list(self._coll_meta.get(name, []))

    def collection_documents(self, name):
        ids, docs, mets = self._coll_docs.get(name, ([], [], []))
        return list(ids), list(docs), list(mets)

    # mutations (record, do not touch real stores)
    def owui_delete_file(self, file_id):
        self.owui_deletes.append(file_id)
        return True

    def delete_collection(self, name):
        self.deleted_collections.append(name)
        return True

    def delete_kb_vectors_by_file(self, kb_name, file_id):
        self.deleted_kb_vectors.append((kb_name, file_id))
        return True

    def delete_junction_by_file(self, file_id):
        self.deleted_junction_files.append(file_id)
        return True

    def delete_junction_by_knowledge(self, kb_id):
        self.deleted_junction_kbs.append(kb_id)
        return True

    # repair-gate reads + the status flip (record, do not touch real stores)
    def file_content(self, file_id):
        return self._content.get(file_id)

    def file_updated_at(self, file_id):
        return self._updated_at.get(file_id)

    def repair_file_status(self, file_id):
        fr = self._files.get(file_id)
        if not fr:
            return False
        fr.status = "completed"      # FileRow is slotted but mutable in place
        self.repaired.append(file_id)
        return True

    def invalidate(self):
        pass  # FakeStores holds no read cache


def _fr(fid, kb="kb-1", directory_id="d1", status="completed", name=None, h="h"):
    return kc.FileRow(id=fid, filename=name or fid, knowledge_id=kb,
                     directory_id=directory_id, file_hash="fh", status=status, hash=h)


def _build_fixture():
    """Build the canonical fixture (see module docstring for the expected
    per-class counts). Returns a populated dict ready for FakeStores."""
    files = {
        "f1":   _fr("f1", status="completed"),                 # clean
        "f2":   _fr("f2", status="completed"),                 # class 4 (no file-f2 coll)
        "f3":   _fr("f3", status="completed", directory_id="dstale"),  # class 2
        "f6":   _fr("f6", status="completed"),                 # class 6 (0 vectors in kb-1)
        "g1":   _fr("g1", status="completed"),                 # class 1 ghost
        "p1":   _fr("p1", status="pending"),                   # class 9
        "n1":   _fr("n1", kb=None, status=None),                 # class 12 (no kb)
        "dup1a": _fr("dup1a", name="dup", h="HH"),             # class 10 (dup)
        "dup1b": _fr("dup1b", name="dup", h="HH"),             # class 10 (dup)
        "fd1":  _fr("fd1", kb="kb-dead", status="completed"),  # dead-KB file
    }
    junction = [
        kc.JunctionRow("j1", "kb-1", "f1", "d1"),
        kc.JunctionRow("j2", "kb-1", "f2", "d1"),
        kc.JunctionRow("j3", "kb-1", "f3", "d1"),
        kc.JunctionRow("j6", "kb-1", "f6", "d1"),
        kc.JunctionRow("jp", "kb-1", "p1", "d1"),
        kc.JunctionRow("jx", "kb-1", "fX", "d1"),    # class 7 (fX not a file)
        kc.JunctionRow("jd", "kb-dead", "fd1", "d1"),  # class 8 (dead KB)
        kc.JunctionRow("ja", "kb-1", "dup1a", "d1"),
        kc.JunctionRow("jb", "kb-1", "dup1b", "d1"),
    ]
    kb_ids = {"kb-1": "KB One"}  # kb-dead NOT in knowledge table (live = {kb-1})
    dir_ids = {"d1"}            # dstale absent -> class 2
    colls = {
        "file-f1": "cf1", "file-f3": "cf3", "file-f6": "cf6",
        "file-g1": "cg1", "file-p1": "cp1", "file-fd1": "cfd1",
        "file-dup1a": "cda", "file-dup1b": "cdb",
        "file-ORPHAN1": "co1", "file-ORPHAN2": "co2",  # class 3 (no file row)
        "kb-1": "ckb1", "kb-dead": "ckbd",
        "knowledge-bases": "ckbb",  # OWUI-internal; must never be flagged
    }
    # kb-1 vectors: f1,f2,f3,dup1a,dup1b,g1(clean+ghost),LEAK1(5b), one w/o file_id
    coll_meta = {
        "kb-1": [{"file_id": "f1"}, {"file_id": "f2"}, {"file_id": "f3"},
                 {"file_id": "dup1a"}, {"file_id": "dup1b"},
                 {"file_id": "g1"}, {"file_id": "LEAK1"},
                 {"knowledge_base_id": "kb-1"}],
        "kb-dead": [{"file_id": "fd1"}],   # dead-KB collection: NOT scanned by class 5
        "knowledge-bases": [{"knowledge_base_id": "kb-1"}] * 16,
        "file-ORPHAN1": [{"file_id": "o1a"}, {"file_id": "o1b"}],
        "file-ORPHAN2": [{"file_id": "o2a"}],
        "file-f1": [{"file_id": "f1"}], "file-f3": [{"file_id": "f3"}],
        "file-f6": [{"file_id": "f6"}], "file-g1": [{"file_id": "g1"}],
        "file-p1": [{"file_id": "p1"}], "file-fd1": [{"file_id": "fd1"}],
        "file-dup1a": [{"file_id": "dup1a"}], "file-dup1b": [{"file_id": "dup1b"}],
    }
    coll_docs = {
        "file-ORPHAN1": (["c1", "c2"], ["doc1", "doc2"], [{"file_id": "o1a"}, {"file_id": "o1b"}]),
        "file-ORPHAN2": (["c3"], ["doc3"], [{"file_id": "o2a"}]),
        "file-g1": (["g1c"], ["ghostdoc"], [{"file_id": "g1"}]),
    }
    return dict(files=files, junction=junction, kb_ids=kb_ids, dir_ids=dir_ids,
                colls=colls, coll_meta=coll_meta, coll_docs=coll_docs)


def _make_stores(fix, tmp, admin_key="ADM"):
    return FakeStores(tmp, **fix, admin_key=admin_key)


# --- tests ----------------------------------------------------------------

class TestClassify(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fix = _build_fixture()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _classes(self, kb=None, stores=None):
        stores = stores or _make_stores(self.fix, self.tmp)
        return kc.classify(stores, kb), stores

    def test_all_class_counts_scope_all(self):
        c, _ = self._classes()
        self.assertEqual(c["ghost_rows"].count, 1)
        self.assertEqual(c["stale_directory_id"].count, 1)
        self.assertEqual(c["orphan_file_collections"].count, 2)
        self.assertEqual(c["orphan_file_collections"].detail["orphan_chunks"], 3)
        self.assertEqual(c["missing_file_collections"].count, 1)   # f2
        self.assertEqual(c["orphan_kb_vectors"].count, 2)         # g1 + LEAK1
        self.assertEqual(c["orphan_kb_vectors"].detail["ghost_link"], 1)
        self.assertEqual(c["orphan_kb_vectors"].detail["leaked"], 1)
        self.assertEqual(c["missing_kb_vectors"].count, 1)        # f6 (completed, 0 vectors)
        self.assertEqual(c["orphan_junction_rows"].count, 1)      # fX
        self.assertEqual(c["dead_kb_junction_rows"].count, 1)    # kb-dead
        self.assertEqual(c["non_completed_leftovers"].count, 1) # p1
        self.assertEqual(c["idempotency_duplicates"].count, 2)   # dup1a+dup1b
        self.assertEqual(c["file_rows_no_knowledge_id"].count, 1)  # n1

    def test_knowledge_bases_never_flagged(self):
        """Blocker 1: the OWUI-internal `knowledge-bases` collection must not be
        treated as a KB collection (class 5) or a file-* orphan (class 3)."""
        c, stores = self._classes()
        # not in class 3 (does not start with file-)
        self.assertNotIn("knowledge-bases", c["orphan_file_collections"].ids)
        # not scanned by class 5 (its single row has no file_id -> would be 0 anyway,
        # but confirm it is never enumerated as a KB collection)
        self.assertNotIn("knowledge-bases", stores.knowledge_ids())
        # class 5 leaked_pairs only contains kb-1 (the live KB)
        for kb_id, _ in c["orphan_kb_vectors"].detail["leaked_pairs"]:
            self.assertEqual(kb_id, "kb-1")

    def test_class5_requires_file_id_key(self):
        """Blocker 1: a vector with no file_id key is ignored by class 5."""
        c, _ = self._classes()
        # kb-1 has 6 metadata rows but only 5 have file_id; 1 is skipped.
        # g1 (in file table) + LEAK1 (not in file table) = 2 flagged.
        self.assertEqual(c["orphan_kb_vectors"].count, 2)

    def test_class7_subtracted_before_class6(self):
        """Blocker 7: a junction row whose file_id is not in the file table is
        class 7, not class 6 (even though it also has 0 vectors)."""
        c, _ = self._classes()
        self.assertIn("fX", c["orphan_junction_rows"].ids)
        self.assertNotIn("fX", c["missing_kb_vectors"].ids)
        # p1 is pending -> class 9, NOT class 6 (completed-only)
        self.assertNotIn("p1", c["missing_kb_vectors"].ids)
        self.assertIn("p1", c["non_completed_leftovers"].ids)

    def test_dead_kb_junction_and_not_scanned_by_class5(self):
        """Blocker 7: dead-KB junction rows are class 8; the dead-KB Chroma
        collection is NOT scanned by class 5 (enumerated from knowledge table)."""
        c, _ = self._classes()
        self.assertIn("kb-dead", c["dead_kb_junction_rows"].ids)
        # kb-dead collection has a vector (fd1) but kb-dead is not live -> not flagged
        for kb_id, _ in c["orphan_kb_vectors"].detail["leaked_pairs"]:
            self.assertNotEqual(kb_id, "kb-dead")

    def test_kb_scoping(self):
        """--kb scopes the KB-tagged classes; classes 3, 12 stay global."""
        c, _ = self._classes(kb="kb-dead")
        self.assertEqual(c["ghost_rows"].count, 0)        # fd1 is linked
        self.assertEqual(c["dead_kb_junction_rows"].count, 1)
        self.assertEqual(c["orphan_kb_vectors"].count, 0)  # dead KB not scanned
        self.assertEqual(c["orphan_junction_rows"].count, 0)
        # global classes unchanged
        self.assertEqual(c["orphan_file_collections"].count, 2)
        self.assertEqual(c["file_rows_no_knowledge_id"].count, 1)

    def test_totals(self):
        c, _ = self._classes()
        self.assertEqual(c["_totals"]["file_rows"], 10)
        self.assertEqual(c["_totals"]["knowledge_kbs"], 1)
        self.assertEqual(c["_totals"]["junction_rows"], 9)
        self.assertEqual(c["_totals"]["chroma_collections"], 13)
        self.assertEqual(c["_totals"]["scope"], "ALL")


class TestPurgeSafe(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fix = _build_fixture()
        self.stores = _make_stores(self.fix, self.tmp)
        self.classes = kc.classify(self.stores)
        self.opts = _opts(purge=True, maint=False, backup=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_safe_purge_drops_ghosts_and_orphans(self):
        export_dir = kc._export_dir(self.stores.data_dir, self.opts.ts)
        manifest = kc.purge(self.stores, self.classes, self.opts, export_dir)
        # ghost -> OWUI DELETE + residual file-g1 collection drop
        self.assertEqual(self.stores.owui_deletes, ["g1"])
        self.assertIn("file-g1", self.stores.deleted_collections)
        # class 3 orphans dropped
        for name in ("file-ORPHAN1", "file-ORPHAN2"):
            self.assertIn(name, self.stores.deleted_collections)
        # maint classes NOT touched in safe tier
        self.assertEqual(self.stores.deleted_kb_vectors, [])
        self.assertEqual(self.stores.deleted_junction_files, [])
        self.assertEqual(self.stores.deleted_junction_kbs, [])
        # manifest + export written (backup on)
        self.assertEqual(len(manifest["purged_collections"]), 3)  # g1 + 2 orphans
        self.assertTrue(os.path.isdir(export_dir))
        for entry in manifest["purged_collections"]:
            self.assertTrue(os.path.isfile(
                os.path.join(export_dir, entry["name"].replace(os.sep, "_") + ".jsonl")))

    def test_no_backup_skips_export(self):
        opts = _opts(purge=True, maint=False, backup=False)
        manifest = kc.purge(self.stores, self.classes, opts, None)
        # drops still happen
        self.assertIn("file-g1", self.stores.deleted_collections)
        self.assertIn("file-ORPHAN1", self.stores.deleted_collections)
        # no export files written
        self.assertEqual(len(manifest["purged_collections"]), 0)

    def test_advisory_classes_not_purged(self):
        export_dir = kc._export_dir(self.stores.data_dir, self.opts.ts)
        kc.purge(self.stores, self.classes, self.opts, export_dir)
        # class 4 (f2), class 2 (f3), class 6 (f6), class 9 (p1), class 10, class 12
        # are advisory -> no collections deleted for them, no OWUI delete
        self.assertNotIn("file-f2", self.stores.deleted_collections)
        self.assertNotIn("file-f6", self.stores.deleted_collections)
        self.assertNotIn("file-p1", self.stores.deleted_collections)
        self.assertNotIn("file-f3", self.stores.deleted_collections)
        self.assertNotIn("f3", self.stores.owui_deletes)
        self.assertNotIn("p1", self.stores.owui_deletes)


class TestPurgeMaint(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fix = _build_fixture()
        self.stores = _make_stores(self.fix, self.tmp)
        self.classes = kc.classify(self.stores)
        self.opts = _opts(purge=True, maint=True, backup=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_maint_purges_5b_7_8_only(self):
        manifest = kc.purge(self.stores, self.classes, self.opts, None)
        # 5b: leaked KB vectors via direct delete (kb-1, LEAK1)
        self.assertEqual(self.stores.deleted_kb_vectors, [("kb-1", "LEAK1")])
        # 7: orphan junction rows (fX)
        self.assertEqual(self.stores.deleted_junction_files, ["fX"])
        # 8: dead-KB junction (kb-dead)
        self.assertEqual(self.stores.deleted_junction_kbs, ["kb-dead"])
        # safe-tier classes NOT touched in maint mode
        self.assertEqual(self.stores.owui_deletes, [])
        self.assertEqual(self.stores.deleted_collections, [])
        self.assertEqual(len(manifest["kb_vectors"]), 1)


class TestReport(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fix = _build_fixture()
        self.stores = _make_stores(self.fix, self.tmp)
        self.classes = kc.classify(self.stores)
        self.names = {fid: fr.filename for fid, fr in self.stores.file_rows().items()}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_human_report_ids_only_default(self):
        out = kc.report_human(self.classes, show_names=False, names=self.names)
        # ghost id shown, not its filename
        self.assertIn("g1", out)
        self.assertNotIn("ghostdoc", out)
        self.assertIn("Advised commands", out)
        self.assertIn("PURGE=1 make kb-check", out)
        self.assertIn("PURGE=1 MAINT=1 make kb-check", out)

    def test_human_report_show_names(self):
        out = kc.report_human(self.classes, show_names=True, names=self.names)
        self.assertIn("g1", out)  # id still present

    def test_json_valid_and_schema(self):
        s = kc.report_json(self.classes, show_names=False, names=self.names)
        obj = json.loads(s)
        self.assertEqual(obj["scope"], "ALL")
        self.assertIn("totals", obj)
        self.assertIn("classes", obj)
        self.assertEqual(obj["classes"]["ghost_rows"]["count"], 1)
        self.assertEqual(obj["classes"]["ghost_rows"]["tier"], "safe")
        # ids-only: samples is a flat list of ids
        self.assertEqual(obj["classes"]["ghost_rows"]["samples"], ["g1"])
        self.assertIn("PURGE=1 make kb-check", obj["advised_commands"])

    def test_json_show_names_schema(self):
        s = kc.report_json(self.classes, show_names=True, names=self.names)
        obj = json.loads(s)
        self.assertEqual(obj["classes"]["ghost_rows"]["samples"],
                         [{"name": "g1", "id": "g1"}])

    def test_advised_commands_when_clean(self):
        clean = {name: kc.ClassResult([], kc.TIER_ADVISORY) for name, _ in kc.CLASS_ORDER}
        clean["_totals"] = {"scope": "ALL"}
        clean["orphan_file_collections"] = kc.ClassResult([], kc.TIER_SAFE, {"orphan_chunks": 0})
        clean["orphan_kb_vectors"] = kc.ClassResult([], kc.TIER_MAINT,
                                                    {"ghost_link": 0, "leaked": 0,
                                                     "leaked_pairs": []})
        cmds = kc.advised_commands(clean)
        self.assertTrue(any("nothing to do" in c for c in cmds))


class TestMain(unittest.TestCase):
    """End-to-end main() on a FakeStores, via monkeypatch of the Stores ctor."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fix = _build_fixture()
        self._orig_stores = kc.Stores
        # main() -> make_stores() checks VECTOR_DB=pgvector + the 4 PG env vars
        # BEFORE the kc.Stores lambda patch intercepts the Stores() call, so set
        # them here (the lambda ignores make_stores' args and builds FakeStores).
        self._snap = {k: os.environ.get(k) for k in
                      ("OPENWEBUI_ADMIN_API_KEY", "VECTOR_DB", "PGVECTOR_USER",
                       "PGVECTOR_PASSWORD", "PGVECTOR_DB", "PGVECTOR_DB_URL")}
        os.environ["OPENWEBUI_ADMIN_API_KEY"] = "ADM"
        os.environ["VECTOR_DB"] = "pgvector"
        os.environ["PGVECTOR_USER"] = "u"
        os.environ["PGVECTOR_PASSWORD"] = "p"
        os.environ["PGVECTOR_DB"] = "d"
        os.environ["PGVECTOR_DB_URL"] = "postgresql://u:p@postgres:5432/d"
        kc.Stores = lambda *a, **k: _make_stores(self.fix, self.tmp, admin_key="ADM")

    def tearDown(self):
        kc.Stores = self._orig_stores
        for k, v in self._snap.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_main_audit_only_human(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = kc.main(["--data-dir", self.tmp])
        self.assertEqual(rc, 0)
        self.assertIn("KB cross-DB check", buf.getvalue())
        self.assertIn("ghost_rows", buf.getvalue())

    def test_main_audit_only_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = kc.main(["--data-dir", self.tmp, "--json"])
        self.assertEqual(rc, 0)
        obj = json.loads(buf.getvalue())
        self.assertEqual(obj["classes"]["orphan_file_collections"]["count"], 2)

    def test_main_purge_safe(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = kc.main(["--data-dir", self.tmp, "--purge"])
        self.assertEqual(rc, 0)
        self.assertIn("Purge manifest", buf.getvalue())


def _opts(purge, maint, backup):
    ns = type("O", (), {})()
    ns.purge = purge
    ns.maint = maint
    ns.backup = backup
    ns.ts = "20260828T000000Z"
    return ns


class TestRepair(unittest.TestCase):
    """Class-9 stuck-processing-while-linked detection + the --repair gate.

    The canonical fixture's class-9 file (p1) is `pending` with 0 vectors in
    kb-1, so it is NOT repairable. These tests build a dedicated stuck file and
    exercise the strong gate (processing + linked + content + vectors + stale).
    See [[no-implicit-workarounds-report-blockers]] -- the gate proves
    completion; it does not blindly mark long-running files complete."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # test_main_repair -> main() -> make_stores() needs VECTOR_DB=pgvector +
        # the 4 PG env vars to reach the kc.Stores lambda patch.
        self._snap = {k: os.environ.get(k) for k in
                      ("VECTOR_DB", "PGVECTOR_USER", "PGVECTOR_PASSWORD",
                       "PGVECTOR_DB", "PGVECTOR_DB_URL")}
        os.environ["VECTOR_DB"] = "pgvector"
        os.environ["PGVECTOR_USER"] = "u"
        os.environ["PGVECTOR_PASSWORD"] = "p"
        os.environ["PGVECTOR_DB"] = "d"
        os.environ["PGVECTOR_DB_URL"] = "postgresql://u:p@postgres:5432/d"

    def tearDown(self):
        for k, v in self._snap.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stores(self, files=None, junction=None, coll_meta=None,
                 content=None, updated_at=None):
        fix = _build_fixture()
        if files:
            fix["files"] = {**fix["files"], **files}
        if junction:
            fix["junction"] = fix["junction"] + junction
        if coll_meta:
            fix["coll_meta"] = {**fix["coll_meta"],
                                **{k: fix["coll_meta"].get(k, []) + v
                                   for k, v in coll_meta.items()}}
        fix["content"] = content or {}
        fix["updated_at"] = updated_at or {}
        return _make_stores(fix, self.tmp)

    def _stuck_stores(self, status="processing", age=3600, content="extracted text",
                      linked=True, vectors=True):
        files = {"stuck1": _fr("stuck1", status=status, name="stuck.docx")}
        junction = [kc.JunctionRow("jst", "kb-1", "stuck1", "d1")] if linked else []
        coll_meta = {"kb-1": [{"file_id": "stuck1"}]} if vectors else {}
        updated_at = {"stuck1": int(time.time()) - age} if age is not None else {}
        return self._stores(files=files, junction=junction, coll_meta=coll_meta,
                            content={"stuck1": content} if content is not None else {},
                            updated_at=updated_at)

    def test_detect_stuck_processing_linked(self):
        s = self._stuck_stores()
        c = kc.classify(s)
        stuck = c["non_completed_leftovers"].detail["stuck_processing_linked"]
        self.assertEqual([x["id"] for x in stuck], ["stuck1"])
        self.assertEqual(stuck[0]["vectors"], 1)

    def test_repair_flips_status_to_completed(self):
        s = self._stuck_stores()
        c = kc.classify(s)
        manifest = kc.repair(s, c)
        self.assertEqual(manifest["repaired"], [{"id": "stuck1", "filename": "stuck.docx"}])
        self.assertEqual(s.repaired, ["stuck1"])
        self.assertEqual(s.file_rows()["stuck1"].status, "completed")
        self.assertEqual(manifest["skipped"], [])

    def test_gate_skips_pending(self):
        # pending (not processing) is reconcile-retryable; left alone.
        s = self._stuck_stores(status="pending")
        c = kc.classify(s)
        self.assertEqual(c["non_completed_leftovers"].detail["stuck_processing_linked"], [])
        manifest = kc.repair(s, c)
        self.assertEqual(manifest["repaired"], [])

    def test_gate_skips_unlinked(self):
        # unlinked + processing is reconcile-retryable; not repaired here.
        s = self._stuck_stores(linked=False)
        c = kc.classify(s)
        self.assertEqual(c["non_completed_leftovers"].detail["stuck_processing_linked"], [])
        self.assertEqual(kc.repair(s, c)["repaired"], [])

    def test_gate_skips_no_vectors(self):
        # 0 vectors -> genuinely not embedded yet; must not be marked complete.
        s = self._stuck_stores(vectors=False)
        c = kc.classify(s)
        self.assertEqual(c["non_completed_leftovers"].detail["stuck_processing_linked"], [])
        self.assertEqual(kc.repair(s, c)["repaired"], [])

    def test_gate_skips_no_content(self):
        # no content -> extraction not done; not repaired.
        s = self._stuck_stores(content=None)
        c = kc.classify(s)
        # detected as stuck (vectors + linked), but repair re-checks content.
        manifest = kc.repair(s, c)
        self.assertEqual(manifest["repaired"], [])
        self.assertEqual(manifest["skipped"][0]["id"], "stuck1")

    def test_gate_skips_not_stale(self):
        # fresh updated_at (in-flight race window) -> not repaired.
        s = self._stuck_stores(age=10)  # < REPAIR_STALE_SECS (60)
        c = kc.classify(s)
        manifest = kc.repair(s, c)
        self.assertEqual(manifest["repaired"], [])
        self.assertIn("not stale", manifest["skipped"][0]["reason"])

    def test_advised_repair_command(self):
        s = self._stuck_stores()
        c = kc.classify(s)
        cmds = kc.advised_commands(c)
        self.assertTrue(any("REPAIR=1" in cmd for cmd in cmds))

    def test_main_repair(self):
        # end-to-end: main(["--repair"]) repairs + re-audits.
        s = self._stuck_stores()
        self._orig_stores = kc.Stores
        kc.Stores = lambda *a, **k: s
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kc.main(["--data-dir", self.tmp, "--repair"])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("Repair manifest", out)
            self.assertIn("repaired: 1", out)
        finally:
            kc.Stores = self._orig_stores


class TestRealStoresRepair(unittest.TestCase):
    """Exercise the REAL Stores repair methods (file_content, file_updated_at,
    repair_file_status) against a temp sqlite webui.db. FakeStores overrides
    these, so they have no coverage otherwise -- this test exists because a
    missing row_factory on the RW connection (tuple vs sqlite3.Row) slipped
    past the FakeStores tests and only surfaced on a real DB."""

    SCHEMA = ("CREATE TABLE file (id TEXT PRIMARY KEY, hash TEXT, filename TEXT, "
              "data TEXT, meta TEXT, created_at INTEGER, updated_at INTEGER)")

    def setUp(self):
        import sqlite3
        self.tmp = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.tmp, "webui.db")
        con = sqlite3.connect(self.dbpath)
        con.execute(self.SCHEMA)
        con.commit()
        con.close()
        self.stores = kc.Stores(self.tmp, "http://owui", "ADM")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _insert(self, fid, status="processing", content="hello world",
                fhash="abc123", updated_at=None):
        import sqlite3, json
        if updated_at is None:
            updated_at = int(time.time()) - 3600
        con = sqlite3.connect(self.dbpath)
        con.execute(
            "INSERT INTO file (id, hash, filename, data, meta, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (fid, fhash, "f.docx", json.dumps({"status": status, "content": content}),
             json.dumps({"data": {"knowledge_id": "kb-1"}}), updated_at, updated_at))
        con.commit()
        con.close()

    def _status(self, fid):
        import sqlite3
        return sqlite3.connect(self.dbpath).execute(
            "SELECT json_extract(data,'$.status'), hash FROM file WHERE id=?", (fid,)
        ).fetchone()

    def test_file_content_and_updated_at(self):
        self._insert("f1")
        self.assertEqual(self.stores.file_content("f1"), "hello world")
        self.assertAlmostEqual(self.stores.file_updated_at("f1"),
                               int(time.time()) - 3600, delta=5)
        self.assertIsNone(self.stores.file_content("missing"))
        self.assertIsNone(self.stores.file_updated_at("missing"))

    def test_repair_file_status_flips_status_keeps_hash(self):
        self._insert("f1", status="processing", fhash="HH")
        self.assertTrue(self.stores.repair_file_status("f1"))
        st, h = self._status("f1")
        self.assertEqual(st, "completed")
        self.assertEqual(h, "HH")  # hash untouched (left as-is by design)

    def test_repair_file_status_missing_row(self):
        self.assertFalse(self.stores.repair_file_status("nope"))


# --- pgvector store: fake psycopg2 + FakePgStores (no real Postgres) -------

class _FakePgCursor:
    """Scriptable psycopg2 cursor stand-in. Dispatches on the SQL string so
    call-order does not matter; mutates `conn._chunks` on DELETE to mirror the
    real document_chunk table."""

    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        s = sql.strip()
        chunks = self._conn._chunks
        if s.startswith("SELECT DISTINCT collection_name"):
            self._rows = [(name,) for name in chunks]
        elif s.startswith("SELECT count(*)"):
            name = params[0]
            if "vmetadata->>'file_id'" in s:
                kb, fid = params
                self._rows = [(sum(1 for c in chunks.get(kb, [])
                                  if c[2].get("file_id") == fid),)]
            else:
                self._rows = [(len(chunks.get(name, [])),)]
        elif s.startswith("SELECT vmetadata"):
            name = params[0]
            self._rows = [(c[2],) for c in chunks.get(name, [])]
        elif s.startswith("SELECT id, text, vmetadata"):
            name = params[0]
            self._rows = list(chunks.get(name, []))
        elif s.startswith("DELETE"):
            if "vmetadata->>'file_id'" in s:
                kb, fid = params
                chunks[kb] = [c for c in chunks.get(kb, [])
                             if c[2].get("file_id") != fid]
            else:
                name = params[0]
                chunks.pop(name, None)
            self._rows = []
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class FakePgConn:
    """psycopg2 connection stand-in. `chunks` = {collection_name:
    [(chunk_id, text, vmetadata_dict), ...]}. commit() is recorded."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.commits = 0
        self.executed = []

    def cursor(self):
        return _FakePgCursor(self)

    def commit(self):
        self.commits += 1


class FakePgStores(kc.Stores):
    """Stores (pgvector-only) with the SQLite reads overridden by fixtures + a
    fake Postgres connection (no real psycopg2, no real DB, no network)."""

    def __init__(self, data_dir, files, junction, kb_ids, dir_ids, chunks):
        super().__init__(data_dir, "http://owui", "ADM", "dsn")
        self._pg = FakePgConn(chunks)   # bypass _pg_conn (no psycopg2 import)
        self._files = files
        self._junction = junction
        self._kb_ids = kb_ids
        self._dir_ids = dir_ids

    # SQLite reads (backend-independent; mirror FakeStores)
    def file_rows(self):
        return dict(self._files)

    def junction_rows(self):
        return list(self._junction)

    def knowledge_ids(self):
        return dict(self._kb_ids)

    def directory_ids(self):
        return set(self._dir_ids)

    def invalidate(self):
        self._cache.clear()


def _pg_fixture():
    """Compact pgvector fixture. `chunks` = {collection_name:
    [(id, text, vmetadata)]}. Synthetic ids only (no PII)."""
    files = {
        "f1": _fr("f1", status="completed"),
        "f2": _fr("f2", status="completed"),   # class 4 (no file-f2 chunks)
    }
    junction = [
        kc.JunctionRow("j1", "kb-1", "f1", "d1"),
        kc.JunctionRow("j2", "kb-1", "f2", "d1"),
    ]
    kb_ids = {"kb-1": "KB One"}
    dir_ids = {"d1"}
    chunks = {
        "kb-1": [
            ("c1", "t1", {"file_id": "f1"}),
            ("c2", "t2", {"file_id": "LEAK"}),       # not in junction -> class 5b
            ("c3", "t3", {"knowledge_base_id": "kb-1"}),  # no file_id -> skipped
        ],
        "file-f1": [("c4", "t4", {"file_id": "f1"})],
        "file-ORPHAN": [("c5", "t5", {"file_id": "o1"})],  # no file row -> class 3
        "knowledge-bases": [("c6", "t6", {"knowledge_base_id": "kb-1"})],
    }
    return dict(files=files, junction=junction, kb_ids=kb_ids,
                dir_ids=dir_ids, chunks=chunks)


class TestBackendSelection(unittest.TestCase):
    """VECTOR_DB selects the store; missing/unknown fail loud (exit 2)."""

    def setUp(self):
        self._snap = {k: os.environ.get(k) for k in
                      ("VECTOR_DB", "PGVECTOR_USER", "PGVECTOR_PASSWORD",
                       "PGVECTOR_DB", "PGVECTOR_DB_URL")}
        for k in self._snap:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._snap.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_missing_vector_db_raises(self):
        with self.assertRaises(RuntimeError):
            kc.make_stores("/tmp", "http://owui", "ADM")

    def test_unknown_vector_db_raises(self):
        os.environ["VECTOR_DB"] = "redis"
        with self.assertRaises(RuntimeError):
            kc.make_stores("/tmp", "http://owui", "ADM")

    def test_chroma_unsupported_raises(self):
        # Chroma was removed; VECTOR_DB=chroma now fails loud (no silent store).
        os.environ["VECTOR_DB"] = "chroma"
        with self.assertRaises(RuntimeError):
            kc.make_stores("/tmp", "http://owui", "ADM")

    def test_pgvector_missing_pg_env_raises(self):
        os.environ["VECTOR_DB"] = "pgvector"
        with self.assertRaises(RuntimeError):
            kc.make_stores("/tmp", "http://owui", "ADM")

    def test_pgvector_returns_stores(self):
        os.environ["VECTOR_DB"] = "pgvector"
        os.environ["PGVECTOR_USER"] = "u"
        os.environ["PGVECTOR_PASSWORD"] = "p"
        os.environ["PGVECTOR_DB"] = "d"
        os.environ["PGVECTOR_DB_URL"] = "postgresql://u:p@postgres:5432/d"
        s = kc.make_stores("/tmp", "http://owui", "ADM")
        self.assertIsInstance(s, kc.Stores)


class TestPgVectorStore(unittest.TestCase):
    """pgvector store methods over a fake document_chunk (no real Postgres)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fix = _pg_fixture()
        self.stores = FakePgStores(self.tmp, **self.fix)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_distinct_collections(self):
        colls = self.stores.chroma_collections()
        self.assertEqual(set(colls),
                         {"kb-1", "file-f1", "file-ORPHAN", "knowledge-bases"})
        self.assertTrue(all(v is None for v in colls.values()))

    def test_collection_count(self):
        self.assertEqual(self.stores.collection_count("kb-1"), 3)
        self.assertEqual(self.stores.collection_count("file-f1"), 1)
        self.assertEqual(self.stores.collection_count("absent"), 0)

    def test_collection_metadatas(self):
        mds = self.stores.collection_metadatas("kb-1")
        self.assertEqual([md.get("file_id") for md in mds],
                         ["f1", "LEAK", None])

    def test_collection_documents(self):
        ids, texts, mets = self.stores.collection_documents("file-f1")
        self.assertEqual(ids, ["c4"])
        self.assertEqual(texts, ["t4"])
        self.assertEqual(mets, [{"file_id": "f1"}])

    def test_delete_collection_removes_chunks(self):
        self.assertTrue(self.stores.delete_collection("file-ORPHAN"))
        self.assertNotIn("file-ORPHAN", self.stores._pg._chunks)
        self.assertGreater(self.stores._pg.commits, 0)

    def test_delete_kb_vectors_counts_before_delete(self):
        n = self.stores.delete_kb_vectors_by_file("kb-1", "LEAK")
        self.assertEqual(n, 1)   # count BEFORE delete
        remaining = [c[2].get("file_id") for c in self.stores._pg._chunks["kb-1"]]
        self.assertNotIn("LEAK", remaining)
        self.assertIn("f1", remaining)
        self.assertGreater(self.stores._pg.commits, 0)

    def test_delete_kb_vectors_zero_delete_returns_zero(self):
        n = self.stores.delete_kb_vectors_by_file("kb-1", "NOPE")
        self.assertEqual(n, 0)
        # nothing removed
        self.assertEqual(len(self.stores._pg._chunks["kb-1"]), 3)


class TestPgVectorClassify(unittest.TestCase):
    """classify() over a pgvector store: classes 3/4/5/6 via SQL over
    document_chunk."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fix = _pg_fixture()
        self.stores = FakePgStores(self.tmp, **self.fix)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_classes_over_pgvector(self):
        c = kc.classify(self.stores)
        # class 3: orphan file-{id} collections (no file row)
        self.assertEqual(c["orphan_file_collections"].count, 1)
        self.assertIn("file-ORPHAN", c["orphan_file_collections"].ids)
        # knowledge-bases never flagged
        self.assertNotIn("knowledge-bases", c["orphan_file_collections"].ids)
        # class 4: completed file, no file-{id} collection in document_chunk
        self.assertEqual(c["missing_file_collections"].count, 1)
        self.assertIn("f2", c["missing_file_collections"].ids)
        # class 5: orphan KB vectors (file_id not in junction); LEAK leaked
        self.assertEqual(c["orphan_kb_vectors"].count, 1)
        self.assertEqual(c["orphan_kb_vectors"].detail["leaked"], 1)
        self.assertEqual(c["orphan_kb_vectors"].detail["leaked_pairs"],
                         [("kb-1", "LEAK")])
        # class 6: completed + linked + 0 vectors in KB collection
        self.assertEqual(c["missing_kb_vectors"].count, 1)
        self.assertIn("f2", c["missing_kb_vectors"].ids)
        # totals: chroma_collections counts distinct collection_name rows
        self.assertEqual(c["_totals"]["chroma_collections"], 4)


# --- BM25 release gate (patch 10) -----------------------------------------

class _FakeCur:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _GateStores:
    """Minimal stub for bm25_gate: scripts _q results for each probe."""

    def __init__(self, ext=1, idx=1, rank_rows=(), colon_rows=0,
                 zero_rows=0, rank_exc=None, colls=None):
        self._ext = ext
        self._idx = idx
        self._rank = rank_rows
        self._colon = colon_rows
        self._zero = zero_rows
        self._rank_exc = rank_exc
        self._colls = colls if colls is not None else {"c1": None}

    def chroma_collections(self):
        return dict(self._colls)

    def _q(self, sql, params=None):
        if params is None:
            if "pg_extension" in sql:
                return _FakeCur([(self._ext,)])
            if "pg_indexes" in sql:
                return _FakeCur([(self._idx,)])
        # bound-param probes: params = (collection_name, query)
        if "pdb.score" in sql:  # ranking path
            if self._rank_exc:
                raise self._rank_exc
            return _FakeCur(self._rank)
        q = params[1] if params else ""
        if q == "???":
            return _FakeCur([(self._zero,)])
        return _FakeCur([(self._colon,)])  # colon/dash-safe


class TestBm25Gate(unittest.TestCase):
    """The gate is the only detector for a silent paradedb failure (hybrid_search
    swallows the error -> langchain full-collection fallback). Prove the
    green/red logic over a stubbed _q; the live probes run via test_09/test_11."""

    def setUp(self):
        self._orig = kc.make_stores
        # Bypass make_stores' env checks; return the gate stub directly.
        self._stub = None
        kc.make_stores = lambda *a, **k: self._stub

    def tearDown(self):
        kc.make_stores = self._orig

    def _run(self, **kw):
        self._stub = _GateStores(**kw)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = kc.main(["--bm25-gate"])
        return rc, buf.getvalue()

    def test_green(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("OK  "), 5)
        self.assertNotIn("FAIL", out)

    def test_red_missing_extension(self):
        rc, out = self._run(ext=0)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", out)
        self.assertIn("pg_search extension", out)

    def test_red_missing_index(self):
        rc, out = self._run(idx=0)
        self.assertEqual(rc, 1)
        self.assertIn("idx_document_chunk_bm25", out)

    def test_red_ranking_path_error(self):
        rc, out = self._run(rank_exc=RuntimeError("no USING bm25 index"))
        self.assertEqual(rc, 1)
        self.assertIn("ranking path", out)

    def test_red_zero_token_nonzero(self):
        rc, out = self._run(zero_rows=5)
        self.assertEqual(rc, 1)
        self.assertIn("zero-token", out)

    def test_empty_corpus_is_green(self):
        # No collections: the ranking path still executes (0 rows) -> green.
        rc, out = self._run(colls={})
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)