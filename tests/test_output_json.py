#!/usr/bin/env python3
"""Unit tests for the agentic-first JSON output of the /kb skill scripts.

Covers every report/status/retrieve subcommand of skills/claude/scripts/kb.py
(the OWUI KB+projects verbs and the `memory` facts verbs): asserts the success
path prints valid JSON with the expected
top-level schema, and (for the agent-facing scripts) that the JSON is COMPACT
(single line, no indent — whitespace costs an agent tokens). `file` defaults
to the EXTRACTED text (GET /files/{id}/data/content) and
has a `--raw` escape hatch (GET /files/{id}/content, original bytes). No stack
required: the HTTP layer is monkeypatched (unittest.mock), and cmd_file's
kb.call + direct urllib.request.urlopen calls are patched too.

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
# The skill wrapper is ONE module, kb.py (a merge of the former kb.py +
# kb.py). It is no longer named `owui`, so there is no clash with
# gateway/kb.py and no sys.modules eviction is needed.
import kb  # noqa: E402

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


class JgetContractTests(_Assertions):
    # The merged kb.jget carries a `strict` flag that reconciles the two
    # original contracts (OWUI strict vs gateway lenient). These mock kb.call
    # and exercise the REAL jget logic (the verb tests above patch jget out).
    # Guards the codex-reviewed blocker: a context-free jget cannot preserve
    # both contracts; the strict flag must.

    def _jget(self, code, txt, strict):
        with mock.patch.object(kb, "call", return_value=(code, txt)):
            return kb.jget(BASE, KEY, "GET", "/x", strict=strict)

    def _exit_msg(self, code, txt, strict):
        with mock.patch.object(kb, "call", return_value=(code, txt)):
            with self.assertRaises(SystemExit) as cm:
                kb.jget(BASE, KEY, "GET", "/x", strict=strict)
        return cm.exception.args[0] if cm.exception.args else ""

    # --- strict=True (OWUI KB verbs) ---
    def test_strict_200_json_returns_parsed(self):
        self.assertEqual(self._jget(200, '{"a": 1}', True), {"a": 1})

    def test_strict_200_empty_exits(self):
        msg = self._exit_msg(200, "", True)
        self.assertIn("non-JSON", msg)

    def test_strict_200_non_json_exits(self):
        msg = self._exit_msg(200, "not-json", True)
        self.assertIn("non-JSON", msg)
        self.assertIn("not-json", msg)

    def test_strict_200_null_returns_none(self):
        # JSON null parses (valid JSON); strict only rejects non-JSON.
        self.assertIsNone(self._jget(200, "null", True))

    def test_strict_non200_raw_body(self):
        # strict prints the RAW body, NOT data["error"].
        msg = self._exit_msg(403, '{"error": "denied", "detail": "x"}', True)
        self.assertIn("403", msg)
        self.assertIn('"error"', msg)   # raw JSON body, not the extracted word
        self.assertIn("denied", msg)

    # --- strict=False (facts `memory` verbs) ---
    def test_lenient_200_json_returns_parsed(self):
        self.assertEqual(self._jget(200, '{"a": 1}', False), {"a": 1})

    def test_lenient_200_empty_returns_none(self):
        self.assertIsNone(self._jget(200, "", False))

    def test_lenient_200_non_json_returns_none(self):
        self.assertIsNone(self._jget(200, "not-json", False))

    def test_lenient_200_null_returns_none(self):
        self.assertIsNone(self._jget(200, "null", False))

    def test_lenient_non200_extracts_error(self):
        # lenient extracts data["error"] when present, else the raw body.
        msg = self._exit_msg(403, '{"error": "denied", "detail": "x"}', False)
        self.assertIn("403", msg)
        self.assertIn("denied", msg)
        self.assertNotIn("detail", msg)   # .error extracted, not the raw body

    def test_lenient_non200_empty_body(self):
        msg = self._exit_msg(500, "", False)
        self.assertIn("500", msg)


class MemoryTests(_Assertions):
    # Every memory cmd_mem_* calls jget(); patch kb.jget with canned JSON.

    def test_whoami(self):
        ns = mock.Mock(spec=[])
        out = _run([(kb, "jget", {"email": "a@b", "role": "user", "id": "u1"})],
                   kb.cmd_mem_whoami, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(set(d), {"email", "role", "id"})

    def test_groups(self):
        ns = mock.Mock(spec=[])
        out = _run([(kb, "jget", {"groups": ["g1", "g2"]})],
                   kb.cmd_mem_groups, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d, {"groups": ["g1", "g2"]})

    def test_add(self):
        ns = mock.Mock(text="t", name="n", group=None, source_description=None)
        out = _run([(kb, "jget", {"group": "user:a@b", "ok": True})],
                   kb.cmd_mem_add, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["group"], "user:a@b")

    def test_retrieve(self):
        ns = mock.Mock(query="q", k=5)
        out = _run([(kb, "jget", {"facts": [{"uuid": "f1"}]})],
                   kb.cmd_mem_retrieve, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d, {"facts": [{"uuid": "f1"}]})

    def test_episodes(self):
        ns = mock.Mock(max=10)
        out = _run([(kb, "jget", {"episodes": [{"uuid": "e1"}]})],
                   kb.cmd_mem_episodes, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d, {"episodes": [{"uuid": "e1"}]})

    def test_status(self):
        ns = mock.Mock(spec=[])
        out = _run([(kb, "jget", {"status": {"neo4j": "healthy"}})],
                   kb.cmd_mem_status, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d, {"status": {"neo4j": "healthy"}})

    def test_forget(self):
        ns = mock.Mock(group="user:a@b")
        out = _run([(kb, "jget", {"group": "user:a@b"})],
                   kb.cmd_mem_forget, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["group"], "user:a@b")

    def test_delete_edge(self):
        ns = mock.Mock(uuid="u1")
        out = _run([(kb, "jget", {"uuid": "u1", "group": "g"})],
                   kb.cmd_mem_delete_edge, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["uuid"], "u1")

    def test_delete_episode(self):
        ns = mock.Mock(uuid="e1")
        out = _run([(kb, "jget", {"uuid": "e1", "group": "g"})],
                   kb.cmd_mem_delete_episode, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["uuid"], "e1")


class OwuiTests(_Assertions):
    def test_whoami(self):
        ns = mock.Mock(spec=[])
        out = _run([(kb, "jget", {"email": "a@b", "role": "user"})],
                   kb.cmd_whoami, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(set(d), {"email", "role"})

    def test_kbs(self):
        items = [{"id": "k1", "name": "KB1", "file_count": 3, "write_access": True,
                  "user": {"email": "o@x"},
                  "description": "Indexed from local root/gdrive/ via api-gateway | source=root | host=testhost | path=gdrive"}]
        ns = mock.Mock(spec=[])
        out = _run([(kb, "jget", {"items": items})], kb.cmd_kbs, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["kbs"], [{"id": "k1", "name": "KB1", "file_count": 3,
                                     "write_access": True, "owner": "o@x",
                                     "description": "Indexed from local root/gdrive/ via api-gateway | source=root | host=testhost | path=gdrive",
                                     "source": "root", "host": "testhost", "path": "gdrive",
                                     "project": None, "repo": None}])

    def test_kbs_legacy_and_unknown(self):
        # Legacy root prose (no kv) -> prefix fallback source=root, path=<top dir>.
        items = [{"id": "k1", "name": "xgen", "file_count": 0, "write_access": False,
                  "user": {"email": "o@x"},
                  "description": "Indexed from local root/xgen/ via api-gateway"},
                 {"id": "k2", "name": "weird", "file_count": 0, "write_access": False,
                  "user": None,
                  "description": "some random description"}]
        ns = mock.Mock(spec=[])
        out = _run([(kb, "jget", {"items": items})], kb.cmd_kbs, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["kbs"][0]["source"], "root")
        self.assertEqual(d["kbs"][0]["path"], "xgen")
        self.assertEqual(d["kbs"][1]["source"], "unknown")

    def test_kbs_empty(self):
        ns = mock.Mock(spec=[])
        out = _run([(kb, "jget", {"items": []})], kb.cmd_kbs, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d, {"kbs": []})  # JSON empty form, not prose

    def test_kb(self):
        ns = mock.Mock(id="k1")
        # cmd_kb calls jget twice when user is None: detail, then list to fill user.
        jget = mock.Mock(side_effect=[
            {"id": "k1", "name": "KB1", "user": None},
            {"items": [{"id": "k1", "user": {"email": "o@x"}}]},
        ])
        out = _run([(kb, "jget", jget)], kb.cmd_kb, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["id"], "k1")
        self.assertEqual(d["user"]["email"], "o@x")

    def test_search_kbs(self):
        items = [{"id": "k1", "name": "KB1"}, {"id": "k2", "name": "KB2"}]
        ns = mock.Mock(query="kb")
        out = _run([(kb, "jget", {"items": items})], kb.cmd_search_kbs, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d, {"kbs": [{"id": "k1", "name": "KB1"},
                                     {"id": "k2", "name": "KB2"}]})

    def test_retrieve(self):
        # cmd_retrieve POSTs the gateway-mediated /retrieve {kb_id, query, k,
        # mode}; the gateway flattens the OWUI response into 8-key hits and
        # returns {hits, score_order}. The wrapper keeps the gdrive join
        # (File.meta.data.gdrive, file-level, one GET per file_id) and echoes
        # the resolved kb_id + kb_name + mode + score_order. ns MUST set
        # mode=None: _mode(a) reads a.mode, and an unset Mock attr is truthy
        # (would forward a Mock, not "hybrid").
        ns = mock.Mock(kb="k1", query="q", k=4, mode=None, no_hybrid=False)
        hits = [{"distance": 0.1, "file": "f1", "file_id": "fid1", "page": 3,
                 "start_index": 0, "source": "upload",
                 "mtime": "2025-10-30T16:50:57Z", "text": "t1"},
                {"distance": 0.2, "file": "f2", "file_id": "", "page": None,
                 "start_index": None, "source": "", "mtime": None, "text": "t2"}]
        resp = {"hits": hits, "score_order": "desc"}
        # Canned File.meta.data.gdrive record for fid1: the gdrive-join in
        # cmd_retrieve (one GET per unique file_id) runs without network.
        # hits[1] has file_id="" -> not fetched -> gdrive stays None.
        gdrive = {"grounded": True, "labels": ["spec"],
                  "approval": {"status": "approved",
                               "complete_time": "2026-01-01T00:00:00Z"},
                  "comments": ["c1"], "description": "desc",
                  "modified_time": "2026-01-02T00:00:00Z"}
        # _resolve_kb supplies provenance (kb_id, kb_name); jget serves the
        # /retrieve response; _file_gdrive serves the per-file gdrive meta.
        jget = mock.Mock(return_value=resp)
        out = _run([(kb, "_resolve_kb", ("k1", "KB1")),
                   (kb, "jget", jget),
                   (kb, "_file_gdrive", gdrive)], kb.cmd_retrieve, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(set(d), {"kb_id", "kb_name", "mode", "score_order", "hits"})
        self.assertEqual(d["kb_id"], "k1")
        self.assertEqual(d["kb_name"], "KB1")
        self.assertEqual(d["mode"], "hybrid")
        self.assertEqual(d["score_order"], "desc")
        self.assertEqual(len(d["hits"]), 2)
        # jget called ONCE for POST /retrieve (resolve is patched, not jget).
        self.assertEqual(jget.call_count, 1)
        args, _ = jget.call_args
        self.assertEqual(args[:4], (BASE, KEY, "POST", "/retrieve"))
        self.assertEqual(args[4], {"kb_id": "k1", "query": "q", "k": 4,
                                    "mode": "hybrid"})
        # the 8 hit keys come from the gateway flatten; gdrive joined per file_id
        self.assertEqual(set(d["hits"][0]),
                         {"distance", "file", "file_id", "page",
                          "start_index", "source", "mtime", "text", "gdrive"})
        self.assertEqual(d["hits"][0]["file"], "f1")
        self.assertEqual(d["hits"][0]["file_id"], "fid1")
        self.assertEqual(d["hits"][0]["page"], 3)
        self.assertEqual(d["hits"][0]["mtime"], "2025-10-30T16:50:57Z")
        # gdrive-join: fid1 -> curated view (grounded carries through); hits[1]
        # has file_id="" -> not fetched -> gdrive None.
        self.assertEqual(d["hits"][0]["gdrive"]["grounded"], True)
        self.assertEqual(d["hits"][0]["gdrive"]["approval_status"], "approved")
        self.assertIsNone(d["hits"][1]["page"])
        self.assertIsNone(d["hits"][1]["mtime"])
        self.assertIsNone(d["hits"][1]["gdrive"])
        self.assertEqual(d["hits"][1]["file_id"], "")

    def test_retrieve_mode_aliases(self):
        # cmd_retrieve forwards the resolved mode in both the printed object
        # and the POST body. --mode lexical / --mode vector pass through; bare
        # --no-hybrid resolves to vector (deprecated alias); --no-hybrid +
        # --mode vector is redundant but ACCEPTED (consistent, not a conflict).
        cases = [("lexical", False, "lexical"),
                 ("vector", False, "vector"),
                 (None, True, "vector"),        # --no-hybrid alias
                 ("vector", True, "vector")]     # --no-hybrid --mode vector
        for mode_arg, no_hybrid, expect in cases:
            ns = mock.Mock(kb="k1", query="q", k=4, mode=mode_arg,
                           no_hybrid=no_hybrid)
            jget = mock.Mock(return_value={"hits": [], "score_order": "desc"})
            out = _run([(kb, "_resolve_kb", ("k1", "KB1")),
                       (kb, "jget", jget)], kb.cmd_retrieve, ns)
            d = self.assert_json(out)
            self.assertEqual(d["mode"], expect, (mode_arg, no_hybrid))
            body = jget.call_args.args[4]
            self.assertEqual(body["mode"], expect, (mode_arg, no_hybrid))

    def test_mode_resolution(self):
        # _mode resolves --mode + the deprecated --no-hybrid alias. --no-hybrid
        # is an alias for --mode vector; it conflicts with an explicit
        # --mode hybrid/lexical (exit) but is redundant-but-accepted with
        # --mode vector. A bare --no-hybrid emits a deprecation line to stderr.
        def _m(mode, no_hybrid):
            return kb._mode(mock.Mock(mode=mode, no_hybrid=no_hybrid))

        self.assertEqual(_m(None, False), "hybrid")   # bare default
        self.assertEqual(_m("hybrid", False), "hybrid")
        self.assertEqual(_m("lexical", False), "lexical")
        self.assertEqual(_m("vector", False), "vector")
        with self.assertRaises(SystemExit):
            _m("hybrid", True)   # --no-hybrid conflicts with --mode hybrid
        with self.assertRaises(SystemExit):
            _m("lexical", True)  # --no-hybrid conflicts with --mode lexical
        self.assertEqual(_m("vector", True), "vector")  # redundant, accepted
        # bare --no-hybrid -> vector + stderr deprecation
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.assertEqual(kb._mode(mock.Mock(mode=None, no_hybrid=True)),
                             "vector")
        self.assertIn("deprecation", buf.getvalue())

    def test_resolve_kb(self):
        # Resolution order: exact id; valid-but-unknown UUID FAILS (no
        # fallthrough to name matching); exact name; else fail. Guards the
        # structural fix: a wrong hand-copied id cannot silently query the
        # wrong KB.
        items = [{"id": "k1", "name": "KB1"}, {"id": "k2", "name": "KB2"}]
        def _r(arg):
            with mock.patch.object(kb, "jget", return_value={"items": items}):
                return kb._resolve_kb(BASE, KEY, arg)
        self.assertEqual(_r("k1"), ("k1", "KB1"))   # exact id
        self.assertEqual(_r("KB2"), ("k2", "KB2"))  # exact name
        with mock.patch.object(kb, "jget", return_value={"items": items}):
            with self.assertRaises(SystemExit):    # valid UUID, unknown -> fail
                kb._resolve_kb(BASE, KEY, "00000000-0000-0000-0000-000000000000")
            with self.assertRaises(SystemExit):    # no match -> fail
                kb._resolve_kb(BASE, KEY, "no-such-kb")

    def test_file_default_returns_extracted_text(self):
        # Default: GET /files/{id}/data/content -> the EXTRACTED text OWUI stored
        # at index time (file.data['content']). Bypasses urlopen entirely; one
        # kb.call, no raw-bytes fetch. Asserts the URL + a trailing newline.
        ns = mock.Mock(id="f1", raw=False)
        call = mock.Mock(return_value=(200, json.dumps({"content": "hello world text"})))
        urlopen = mock.Mock(
            side_effect=AssertionError("urlopen must not be called without --raw"))
        out = _run([(kb, "call", call), (kb.urllib.request, "urlopen", urlopen)],
                   kb.cmd_file, ns)
        self.assertEqual(out, "hello world text\n")  # trailing newline added
        self.assertEqual(call.call_args.args[:4],
                         (BASE, KEY, "GET", "/api/v1/files/f1/data/content"))

    def test_file_raw_flag_uses_content_endpoint(self):
        # --raw skips the extracted-text endpoint and fetches the ORIGINAL bytes
        # via GET /files/{id}/content (urlopen, bearer auth). Text bytes decode
        # and print verbatim. Proves the "download a raw file" capability + auth.
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
        call = mock.Mock(side_effect=AssertionError("call() must not be called with --raw"))
        ns = mock.Mock(id="f1", raw=True)
        out = _run([(kb, "call", call), (kb.urllib.request, "urlopen", urlopen)],
                   kb.cmd_file, ns)
        self.assertEqual(out, "plain text body\n")
        req = captured["req"]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.full_url, BASE + "/api/v1/files/f1/content")
        # Bearer auth (case-insensitive on the header name — urllib's casing
        # varies by Python version).
        hdrs = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(hdrs.get("authorization"), "Bearer " + KEY)

    def test_file_binary_fallback_saves_and_exits_zero(self):
        # No extracted text (empty /data/content) + undecodable raw bytes -> save
        # to a temp file + NOTE on stderr, and the command RETURNS (exit 0): not a
        # hard failure (the agent can open the saved file or `retrieve` chunks).
        class _Resp:
            def __init__(self, body, ctype):
                self._b = body
                self.headers = {"Content-Type": ctype}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self._b
        urlopen = mock.Mock(return_value=_Resp(b"\xff\xfe\x00\x01", "application/pdf"))
        call = mock.Mock(return_value=(200, json.dumps({"content": ""})))
        ns = mock.Mock(id="f1", raw=False)
        buf, err = io.StringIO(), io.StringIO()
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(kb, "call", new=call))
        stack.enter_context(mock.patch.object(kb.urllib.request, "urlopen", new=urlopen))
        with stack, contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            ret = kb.cmd_file(BASE, KEY, ns)
        self.assertIsNone(ret)                 # no SystemExit (exit 0)
        self.assertEqual(buf.getvalue(), "")  # nothing on stdout
        note = err.getvalue()
        self.assertIn("application/pdf", note)
        os.unlink(note.split("Saved to: ", 1)[1].split()[0])  # clean temp file

    def test_file_binary_saves_raw_bytes_and_notes_path(self):
        # No extracted text + binary body -> the exact raw bytes are written to a
        # temp file AND the NOTE (on stderr) names that path + content-type + size
        # (a real download, not just a message). Cleans up the temp file.
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
        call = mock.Mock(return_value=(200, json.dumps({"content": ""})))
        ns = mock.Mock(id="f9", raw=False)
        buf, err = io.StringIO(), io.StringIO()
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(kb, "call", new=call))
        stack.enter_context(mock.patch.object(kb.urllib.request, "urlopen", new=urlopen))
        with stack, contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            ret = kb.cmd_file(BASE, KEY, ns)
        self.assertIsNone(ret)  # exit 0, not a SystemExit
        note = err.getvalue()
        self.assertIn("image/png", note)
        self.assertIn("%d bytes" % len(raw), note)
        path = note.split("Saved to: ", 1)[1].split()[0]
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), raw)  # exact raw bytes saved
        os.unlink(path)

    # --- projects-memory surface (kb._whoami / _kb_files / _kb_status / ...) ---

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
            (kb, "_whoami", {"email": "a@b"}),
            (kb, "jget", {"items": []}),                # no existing KB
            (kb, "call", (200, '{"id": "newid"}')),      # create KB
            (kb, "_kb_files", []),
            (kb, "_upload_memory_file", upload),
            (kb, "_delete_file", (True, None)),
        ]
        out = _run(patches, kb.cmd_index_projects, ns)
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
            (kb, "_whoami", {"email": "a@b"}),
            (kb, "jget", {"items": []}),
            (kb, "call", (200, '{"id": "newid"}')),
            (kb, "_kb_files", []),
        ]
        out = _run(patches, kb.cmd_index_projects, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["projects"][0]["created"], "would-create")
        self.assertEqual(d["projects"][0]["added"], 1)

    def test_index_projects_create_failure_recorded(self):
        # A failed KB create must append a project entry (not silently drop it).
        root, _enc = self._proj_tree()
        ns = mock.Mock(host="testhost", root=root, project=None,
                       dry_run=False, wait=False, no_cleanup=False)
        patches = [
            (kb, "_whoami", {"email": "a@b"}),
            (kb, "jget", {"items": []}),
            (kb, "call", (500, '{"error": "boom"}')),   # create fails
            (kb, "_kb_files", []),
        ]
        out = _run(patches, kb.cmd_index_projects, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["total"]["failed"], 1)
        self.assertEqual(d["projects"][0]["created"], "failed")
        self.assertTrue(d["projects"][0]["errors"])  # error captured, not printed

    def test_index_projects_no_projects_empty_json(self):
        root = tempfile.mkdtemp(prefix="kb-ut-")
        ns = mock.Mock(host="testhost", root=root, project=None,
                       dry_run=False, wait=False, no_cleanup=False)
        out = _run([(kb, "_whoami", {"email": "a@b"})],
                   kb.cmd_index_projects, ns)
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
            (kb, "_whoami", {"email": "a@b"}),
            (kb, "jget", {"items": []}),
            (kb, "call", (200, '{"id": "newid"}')),
            (kb, "_kb_files", []),
            (kb, "_upload_memory_file", ({"id": "fid"}, None)),
            (kb, "_delete_file", (True, None)),
            (kb, "_kb_status", drained),
        ]
        with mock.patch.object(kb.time, "sleep"):  # no real sleeping
            out = _run(patches, kb.cmd_index_projects, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(len(d["waited"]), 1)
        self.assertEqual(d["waited"][0]["completed"], 1)

    def test_retrieve_projects(self):
        ns = mock.Mock(query="q", host=None, project=None, account=None,
                       kb_glob=None, k=4, mode=None, no_hybrid=False)
        hits = [{"distance": 0.1, "file": "f", "text": "t"}]
        items = [{"id": "k1", "name": "testhost--p", "user": {"email": "a@b"},
                  "description": "repo=r"}]
        patches = [
            (kb, "_whoami", {"email": "a@b"}),
            (kb, "jget", {"items": items}),
            (kb, "_search_one_kb", (hits, "desc", None)),
        ]
        out = _run(patches, kb.cmd_retrieve_projects, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["kbs"], 1)
        self.assertEqual(d["score_order"], "desc")
        self.assertEqual(len(d["hits"]), 1)
        self.assertEqual(d["hits"][0]["kb_name"], "testhost--p")
        self.assertEqual(d["hits"][0]["repo"], "r")

    def test_retrieve_projects_empty(self):
        ns = mock.Mock(query="q", host=None, project=None, account=None,
                       kb_glob=None, k=4, mode=None, no_hybrid=False)
        patches = [(kb, "_whoami", {"email": "a@b"}),
                   (kb, "jget", {"items": []})]
        out = _run(patches, kb.cmd_retrieve_projects, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d, {"kbs": 0, "score_order": "asc",
                             "hits": [], "errors": []})

    def test_retrieve_projects_sort_order(self):
        # score_order drives the merge sort: hybrid/lexical return an RRF score
        # (higher=better, desc); vector returns a cosine distance (lower=better,
        # asc). Sorting ascending unconditionally reverses hybrid ranking — this
        # guards that fix. Two KBs each return 3 hits at fixed distances.
        items = [{"id": "k1", "name": "h--p1", "user": {"email": "a@b"},
                  "description": "repo=r1"},
                 {"id": "k2", "name": "h--p2", "user": {"email": "a@b"},
                  "description": "repo=r2"}]
        kb1 = [{"distance": 0.9, "text": "a"}, {"distance": 0.1, "text": "b"},
               {"distance": 0.5, "text": "c"}]
        kb2 = [{"distance": 0.8, "text": "d"}, {"distance": 0.2, "text": "e"},
               {"distance": 0.6, "text": "f"}]

        def _search(base, key, kb_id, query, k, mode):
            return (list(kb1 if kb_id == "k1" else kb2), self._order, None)

        def _run_proj(order):
            self._order = order
            ns = mock.Mock(query="q", host=None, project=None, account=None,
                           kb_glob=None, k=10, mode=None, no_hybrid=False)
            patches = [(kb, "_whoami", {"email": "a@b"}),
                       (kb, "jget", {"items": items}),
                       (kb, "_search_one_kb", mock.Mock(side_effect=_search))]
            return _run(patches, kb.cmd_retrieve_projects, ns)

        # desc: highest distance first (RRF score, higher=better)
        d = self.assert_json(_run_proj("desc"))
        self.assertEqual(d["score_order"], "desc")
        self.assertEqual([h["distance"] for h in d["hits"]],
                         [0.9, 0.8, 0.6, 0.5, 0.2, 0.1])
        # asc: lowest distance first (cosine distance, lower=better)
        d = self.assert_json(_run_proj("asc"))
        self.assertEqual(d["score_order"], "asc")
        self.assertEqual([h["distance"] for h in d["hits"]],
                         [0.1, 0.2, 0.5, 0.6, 0.8, 0.9])

    def test_status_projects_success(self):
        ns = mock.Mock(project="p", host=None, wait=False)
        drained = {"completed": 2, "pending": 0, "processing": 0,
                   "failed": 0, "failed_files": []}
        items = [{"id": "k1", "name": "testhost--p", "user": {"email": "a@b"}}]
        patches = [(kb, "_whoami", {"email": "a@b"}),
                   (kb, "jget", {"items": items}),
                   (kb, "_kb_status", drained)]
        out = _run(patches, kb.cmd_status_projects, ns)
        d = self.assert_json(out); self.assert_compact(out)
        self.assertEqual(d["kb_id"], "k1")
        self.assertEqual(d["completed"], 2)
        self.assertEqual(d["failed_files"], [])

    def test_status_projects_not_found_exits(self):
        ns = mock.Mock(project="nope", host=None, wait=False)
        patches = [(kb, "_whoami", {"email": "a@b"}),
                   (kb, "jget", {"items": []})]
        buf = io.StringIO()
        stack = contextlib.ExitStack()
        for mod, name, val in patches:
            stack.enter_context(mock.patch.object(mod, name, return_value=val))
        with stack, contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            kb.cmd_status_projects(BASE, KEY, ns)
        d = json.loads(buf.getvalue())  # the error object is valid JSON
        self.assertIn("error", d)


if __name__ == "__main__":
    unittest.main()