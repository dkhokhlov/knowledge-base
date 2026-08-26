#!/usr/bin/env python3
"""Unit tests for the agentic-first JSON output of the /kb skill scripts.

Covers every report/status/retrieve subcommand of skills/claude/scripts/{owui.py,
kb_gateway.py}: asserts the success path prints valid JSON with the expected
top-level schema, and (for the agent-facing scripts) that the JSON is COMPACT
(single line, no indent — whitespace costs an agent tokens). `rag` and `file`
are asserted to stay raw text. No stack required: the HTTP layer is monkeypatched
(unittest.mock), and cmd_file's direct urllib.request.urlopen call is patched too.

Run:  python3 tests/test_output_json.py -v   (or: make test-output)
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                       "skills", "claude", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS))
import kb_gateway  # noqa: E402
import owui  # noqa: E402

BASE = "http://testhost"
KEY = "testkey"


def _run(patches, func, ns):
    """Apply patches (list of (module, name, value)) then call func(BASE, KEY, ns),
    capturing stdout. A `value` that is a unittest.mock.Mock is installed as the
    new attribute (use for side_effect / custom behavior); any other value is set
    as the patch return_value. Returns the captured stdout string."""
    stack = contextlib.ExitStack()
    for mod, name, val in patches:
        if isinstance(val, mock.Mock):
            stack.enter_context(mock.patch.object(mod, name, new=val))
        else:
            stack.enter_context(mock.patch.object(mod, name, return_value=val))
    buf = io.StringIO()
    with stack, contextlib.redirect_stdout(buf):
        func(BASE, KEY, ns)
    return buf.getvalue()


class _Assertions(unittest.TestCase):
    def assert_json(self, out):
        try:
            return json.loads(out)
        except Exception as e:
            self.fail("output is not valid JSON: %r (%s)" % (out[:200], e))

    def assert_compact(self, out):
        # Compact json.dumps has no real newlines (embedded newlines are escaped
        # as the two chars \n); only print()'s trailing newline is present.
        stripped = out.rstrip()
        if "\n" in stripped:
            self.fail("output is not compact (contains a real newline): %r"
                      % stripped[:200])


class KbGatewayTests(_Assertions):
    # Every kb_gateway cmd_* calls jget(); patch kb_gateway.jget with canned JSON.

    def test_whoami(self):
        ns = mock.Mock(spec=[])
        out = _run([(kb_gateway, "jget", {"email": "a@b", "role": "user", "id": "u1"})],
                   kb_gateway.cmd_whoami, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(set(d), {"email", "role", "id"})

    def test_groups(self):
        ns = mock.Mock(spec=[])
        out = _run([(kb_gateway, "jget", {"groups": ["g1", "g2"]})],
                   kb_gateway.cmd_groups, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d, {"groups": ["g1", "g2"]})

    def test_add(self):
        ns = mock.Mock(text="t", name="n", group=None, source_description=None)
        out = _run([(kb_gateway, "jget", {"group": "user:a@b", "ok": True})],
                   kb_gateway.cmd_add, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["group"], "user:a@b")

    def test_retrieve(self):
        ns = mock.Mock(query="q", k=5)
        out = _run([(kb_gateway, "jget", {"facts": [{"uuid": "f1"}]})],
                   kb_gateway.cmd_retrieve, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d, {"facts": [{"uuid": "f1"}]})

    def test_episodes(self):
        ns = mock.Mock(max=10)
        out = _run([(kb_gateway, "jget", {"episodes": [{"uuid": "e1"}]})],
                   kb_gateway.cmd_episodes, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d, {"episodes": [{"uuid": "e1"}]})

    def test_status(self):
        ns = mock.Mock(spec=[])
        out = _run([(kb_gateway, "jget", {"status": {"neo4j": "healthy"}})],
                   kb_gateway.cmd_status, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d, {"status": {"neo4j": "healthy"}})

    def test_forget(self):
        ns = mock.Mock(group="user:a@b")
        out = _run([(kb_gateway, "jget", {"group": "user:a@b"})],
                   kb_gateway.cmd_forget, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["group"], "user:a@b")

    def test_delete_edge(self):
        ns = mock.Mock(uuid="u1")
        out = _run([(kb_gateway, "jget", {"uuid": "u1", "group": "g"})],
                   kb_gateway.cmd_delete_edge, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["uuid"], "u1")

    def test_delete_episode(self):
        ns = mock.Mock(uuid="e1")
        out = _run([(kb_gateway, "jget", {"uuid": "e1", "group": "g"})],
                   kb_gateway.cmd_delete_episode, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["uuid"], "e1")


class OwuiTests(_Assertions):
    def test_whoami(self):
        ns = mock.Mock(spec=[])
        out = _run([(owui, "jget", {"email": "a@b", "role": "user"})],
                   owui.cmd_whoami, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(set(d), {"email", "role"})

    def test_kbs(self):
        items = [{"id": "k1", "name": "KB1", "file_count": 3, "write_access": True,
                  "user": {"email": "o@x"}}]
        ns = mock.Mock(spec=[])
        out = _run([(owui, "jget", {"items": items})], owui.cmd_kbs, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["kbs"], [{"id": "k1", "name": "KB1", "file_count": 3,
                                     "write_access": True, "owner": "o@x"}])

    def test_kbs_empty(self):
        ns = mock.Mock(spec=[])
        out = _run([(owui, "jget", {"items": []})], owui.cmd_kbs, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d, {"kbs": []})  # JSON empty form, not prose

    def test_kb(self):
        ns = mock.Mock(id="k1")
        # cmd_kb calls jget twice when user is None: detail, then list to fill user.
        jget = mock.Mock(side_effect=[
            {"id": "k1", "name": "KB1", "user": None},
            {"items": [{"id": "k1", "user": {"email": "o@x"}}]},
        ])
        out = _run([(owui, "jget", jget)], owui.cmd_kb, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["id"], "k1")
        self.assertEqual(d["user"]["email"], "o@x")

    def test_search_kbs(self):
        items = [{"id": "k1", "name": "KB1"}, {"id": "k2", "name": "KB2"}]
        ns = mock.Mock(query="kb")
        out = _run([(owui, "jget", {"items": items})], owui.cmd_search_kbs, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d, {"kbs": [{"id": "k1", "name": "KB1"},
                                     {"id": "k2", "name": "KB2"}]})

    def test_retrieve(self):
        chroma = {"documents": [["t1", "t2"]],
                  "distances": [[0.1, 0.2]],
                  "metadatas": [[{"file_name": "f1", "file_id": "fid1", "page": 3,
                                  "start_index": 0, "source": "upload",
                                  "mtime": "2025-10-30T16:50:57Z"},
                                 {"file_name": "f2"}]],
                  "ids": [["id1", "id2"]]}
        ns = mock.Mock(kb="k1", query="q", k=4, no_hybrid=False)
        # _resolve_kb supplies the provenance (kb_id, kb_name); jget serves the
        # Chroma collection query. retrieve no longer trusts a hand-copied id.
        out = _run([(owui, "_resolve_kb", ("k1", "KB1")),
                   (owui, "jget", chroma)], owui.cmd_retrieve, ns)
        d = self.assert_json(out); self.assert_compact(out)
        # top-level provenance (#3): resolved kb_id + kb_name echo, plus hits.
        self.assertEqual(set(d), {"kb_id", "kb_name", "hits"})
        self.assertEqual(d["kb_id"], "k1")
        self.assertEqual(d["kb_name"], "KB1")
        self.assertEqual(len(d["hits"]), 2)
        # file_id/page/start_index/source/mtime propagate from Chroma metadata
        # so the agent can round-trip a hit to the original page (file <file_id>
        # + pdftotext/pdftoppm -f <page>) and see the source mtime; absent
        # metadata defaults to None / "".
        self.assertEqual(set(d["hits"][0]),
                         {"id", "distance", "file", "file_id", "page",
                          "start_index", "source", "mtime", "text"})
        self.assertEqual(d["hits"][0]["file"], "f1")
        self.assertEqual(d["hits"][0]["file_id"], "fid1")
        self.assertEqual(d["hits"][0]["page"], 3)
        self.assertEqual(d["hits"][0]["mtime"], "2025-10-30T16:50:57Z")
        self.assertIsNone(d["hits"][1]["page"])
        self.assertIsNone(d["hits"][1]["mtime"])
        self.assertEqual(d["hits"][1]["file_id"], "")

    def test_resolve_kb(self):
        # Resolution order: exact id; valid-but-unknown UUID FAILS (no
        # fallthrough to name matching); exact name; else fail. Guards the
        # structural fix: a wrong hand-copied id cannot silently query the
        # wrong KB.
        items = [{"id": "k1", "name": "KB1"}, {"id": "k2", "name": "KB2"}]
        def _r(arg):
            with mock.patch.object(owui, "jget", return_value={"items": items}):
                return owui._resolve_kb(BASE, KEY, arg)
        self.assertEqual(_r("k1"), ("k1", "KB1"))   # exact id
        self.assertEqual(_r("KB2"), ("k2", "KB2"))  # exact name
        with mock.patch.object(owui, "jget", return_value={"items": items}):
            with self.assertRaises(SystemExit):    # valid UUID, unknown -> fail
                owui._resolve_kb(BASE, KEY, "00000000-0000-0000-0000-000000000000")
            with self.assertRaises(SystemExit):    # no match -> fail
                owui._resolve_kb(BASE, KEY, "no-such-kb")

    def test_rag_is_raw_text(self):
        # rag prints the LLM answer verbatim — NOT JSON-wrapped (lossy for an agent).
        # Proxied by the kb-gateway: one POST /memory/rag, no `model` key (the
        # gateway inserts it). Asserting the route + body means this cannot pass
        # against the old direct-/api/chat/completions endpoint (codex #5).
        jget = mock.Mock(return_value={"content": "the answer"})
        ns = mock.Mock(question="q", kb=[])
        out = _run([(owui, "jget", jget)], owui.cmd_rag, ns)
        self.assertEqual(out.strip(), "the answer")
        self.assertFalse(out.lstrip().startswith("{"))
        jget.assert_called_once()
        args, _ = jget.call_args
        self.assertEqual(args[:4], (BASE, KEY, "POST", "/memory/rag"))
        body = args[4]
        self.assertEqual(body["messages"], [{"role": "user", "content": "q"}])
        self.assertNotIn("model", body)
        self.assertNotIn("files", body)  # kb empty -> no files key

    def test_file_text_is_raw(self):
        # file bypasses owui.call() -> urllib.request.urlopen directly.
        class _Resp:
            def __init__(self, body, ctype):
                self._b = body
                self.headers = {"Content-Type": ctype}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self._b
        urlopen = mock.Mock(return_value=_Resp(b"hello world text", "text/plain"))
        ns = mock.Mock(id="f1")
        out = _run([(owui.urllib.request, "urlopen", urlopen)], owui.cmd_file, ns)
        self.assertEqual(out, "hello world text")  # raw, not JSON

    def test_file_binary_fallback_exits(self):
        # Undecodable bytes -> save to temp + sys.exit("NOTE ...") (prose, non-JSON).
        class _Resp:
            def __init__(self, body, ctype):
                self._b = body
                self.headers = {"Content-Type": ctype}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self._b
        urlopen = mock.Mock(return_value=_Resp(b"\xff\xfe\x00\x01", "application/pdf"))
        ns = mock.Mock(id="f1")
        buf = io.StringIO()
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(owui.urllib.request, "urlopen", new=urlopen))
        with stack, contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            owui.cmd_file(BASE, KEY, ns)

    def test_file_hits_content_url_with_bearer(self):
        # The raw-download endpoint: GET /api/v1/files/{id}/content with the
        # Bearer key. Proves the skill targets the right URL + auth (the core
        # "download a raw file" capability), not just the decode branch.
        class _Resp:
            def __init__(self, body, ctype):
                self._b = body
                self.headers = {"Content-Type": ctype}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self._b
        captured = {}
        def _urlopen(req, timeout=None):
            captured["req"] = req
            return _Resp(b"plain text body", "text/plain")
        urlopen = mock.Mock(side_effect=_urlopen)
        ns = mock.Mock(id="f1")
        out = _run([(owui.urllib.request, "urlopen", urlopen)], owui.cmd_file, ns)
        self.assertEqual(out, "plain text body")  # raw text, not JSON
        req = captured["req"]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.full_url, BASE + "/api/v1/files/f1/content")
        # Bearer auth (case-insensitive on the header name — urllib's casing
        # varies by Python version).
        hdrs = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(hdrs.get("authorization"), "Bearer " + KEY)

    def test_file_binary_saves_raw_bytes_and_notes_path(self):
        # Binary body -> the exact raw bytes are written to a temp file AND the
        # NOTE names that path + the content-type + size (a real download, not
        # just an exit). Cleans up the temp file it created.
        raw = b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff"  # 12 bytes, not utf-8 decodable
        class _Resp:
            def __init__(self, body, ctype):
                self._b = body
                self.headers = {"Content-Type": ctype}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self._b
        def _urlopen(req, timeout=None):
            return _Resp(raw, "image/png")
        urlopen = mock.Mock(side_effect=_urlopen)
        ns = mock.Mock(id="f9")
        buf = io.StringIO()
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(owui.urllib.request, "urlopen", new=urlopen))
        with stack, contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            owui.cmd_file(BASE, KEY, ns)
        note = cm.exception.code
        self.assertIn("image/png", note)
        self.assertIn("%d bytes" % len(raw), note)
        path = note.split("Saved to: ", 1)[1].split()[0]
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), raw)  # exact raw bytes saved
        os.unlink(path)

    # --- projects-memory surface (owui._whoami / _kb_files / _kb_status / ...) ---

    def _proj_tree(self):
        root = tempfile.mkdtemp(prefix="kb-ut-")
        enc = "-tmp-testproj"
        mem = os.path.join(root, enc, "memory")
        os.makedirs(mem)
        with open(os.path.join(mem, "x.md"), "w") as f:
            f.write("# memory\nfact one\n")
        return root, enc

    def test_index_projects_creates(self):
        root, _enc = self._proj_tree()
        ns = mock.Mock(host="testhost", root=root, project=None,
                       dry_run=False, wait=False, no_cleanup=False)
        upload = mock.Mock(return_value=({"id": "fid"}, None))
        patches = [
            (owui, "_whoami", {"email": "a@b"}),
            (owui, "jget", {"items": []}),                # no existing KB
            (owui, "call", (200, '{"id": "newid"}')),      # create KB
            (owui, "_kb_files", []),
            (owui, "_upload_memory_file", upload),
            (owui, "_delete_file", (True, None)),
        ]
        out = _run(patches, owui.cmd_index_projects, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["total"]["added"], 1)
        self.assertEqual(d["projects"][0]["created"], "created")
        self.assertEqual(d["projects"][0]["kb_id"], "newid")
        self.assertEqual(d["projects"][0]["errors"], [])
        self.assertEqual(d["waited"], [])

    def test_index_projects_dry_run(self):
        root, _enc = self._proj_tree()
        ns = mock.Mock(host="testhost", root=root, project=None,
                       dry_run=True, wait=False, no_cleanup=False)
        patches = [
            (owui, "_whoami", {"email": "a@b"}),
            (owui, "jget", {"items": []}),
            (owui, "call", (200, '{"id": "newid"}')),
            (owui, "_kb_files", []),
        ]
        out = _run(patches, owui.cmd_index_projects, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["projects"][0]["created"], "would-create")
        self.assertEqual(d["projects"][0]["added"], 1)

    def test_index_projects_create_failure_recorded(self):
        # A failed KB create must append a project entry (not silently drop it).
        root, _enc = self._proj_tree()
        ns = mock.Mock(host="testhost", root=root, project=None,
                       dry_run=False, wait=False, no_cleanup=False)
        patches = [
            (owui, "_whoami", {"email": "a@b"}),
            (owui, "jget", {"items": []}),
            (owui, "call", (500, '{"error": "boom"}')),   # create fails
            (owui, "_kb_files", []),
        ]
        out = _run(patches, owui.cmd_index_projects, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["total"]["failed"], 1)
        self.assertEqual(d["projects"][0]["created"], "failed")
        self.assertTrue(d["projects"][0]["errors"])  # error captured, not printed

    def test_index_projects_no_projects_empty_json(self):
        root = tempfile.mkdtemp(prefix="kb-ut-")
        ns = mock.Mock(host="testhost", root=root, project=None,
                       dry_run=False, wait=False, no_cleanup=False)
        out = _run([(owui, "_whoami", {"email": "a@b"})],
                   owui.cmd_index_projects, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d, {"projects": [],
                             "total": {"added": 0, "modified": 0, "reused": 0,
                                       "deleted": 0, "failed": 0},
                             "waited": []})

    def test_index_projects_wait(self):
        root, _enc = self._proj_tree()
        ns = mock.Mock(host="testhost", root=root, project=None,
                       dry_run=False, wait=True, no_cleanup=False)
        drained = {"completed": 1, "pending": 0, "processing": 0,
                   "failed": 0, "failed_files": []}
        patches = [
            (owui, "_whoami", {"email": "a@b"}),
            (owui, "jget", {"items": []}),
            (owui, "call", (200, '{"id": "newid"}')),
            (owui, "_kb_files", []),
            (owui, "_upload_memory_file", ({"id": "fid"}, None)),
            (owui, "_delete_file", (True, None)),
            (owui, "_kb_status", drained),
        ]
        with mock.patch.object(owui.time, "sleep"):  # no real sleeping
            out = _run(patches, owui.cmd_index_projects, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(len(d["waited"]), 1)
        self.assertEqual(d["waited"][0]["completed"], 1)

    def test_retrieve_projects(self):
        ns = mock.Mock(query="q", host=None, project=None, account=None,
                       kb_glob=None, k=4, no_hybrid=False)
        hits = [{"id": "h1", "distance": 0.1, "file": "f", "text": "t"}]
        items = [{"id": "k1", "name": "testhost--p", "user": {"email": "a@b"},
                  "description": "repo=r"}]
        patches = [
            (owui, "_whoami", {"email": "a@b"}),
            (owui, "jget", {"items": items}),
            (owui, "_search_one_kb", (hits, None)),
        ]
        out = _run(patches, owui.cmd_retrieve_projects, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["kbs"], 1)
        self.assertEqual(len(d["hits"]), 1)
        self.assertEqual(d["hits"][0]["kb_name"], "testhost--p")
        self.assertEqual(d["hits"][0]["repo"], "r")

    def test_retrieve_projects_empty(self):
        ns = mock.Mock(query="q", host=None, project=None, account=None,
                       kb_glob=None, k=4, no_hybrid=False)
        patches = [(owui, "_whoami", {"email": "a@b"}),
                   (owui, "jget", {"items": []})]
        out = _run(patches, owui.cmd_retrieve_projects, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d, {"kbs": 0, "hits": [], "errors": []})

    def test_status_projects_success(self):
        ns = mock.Mock(project="p", host=None, wait=False)
        drained = {"completed": 2, "pending": 0, "processing": 0,
                   "failed": 0, "failed_files": []}
        items = [{"id": "k1", "name": "testhost--p", "user": {"email": "a@b"}}]
        patches = [(owui, "_whoami", {"email": "a@b"}),
                   (owui, "jget", {"items": items}),
                   (owui, "_kb_status", drained)]
        out = _run(patches, owui.cmd_status_projects, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["kb_id"], "k1")
        self.assertEqual(d["completed"], 2)
        self.assertEqual(d["failed_files"], [])

    def test_status_projects_not_found_exits(self):
        ns = mock.Mock(project="nope", host=None, wait=False)
        patches = [(owui, "_whoami", {"email": "a@b"}),
                   (owui, "jget", {"items": []})]
        buf = io.StringIO()
        stack = contextlib.ExitStack()
        for mod, name, val in patches:
            stack.enter_context(mock.patch.object(mod, name, return_value=val))
        with stack, contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            owui.cmd_status_projects(BASE, KEY, ns)
        d = json.loads(buf.getvalue())  # the error object is valid JSON
        self.assertIn("error", d)


if __name__ == "__main__":
    unittest.main()