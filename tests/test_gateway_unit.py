#!/usr/bin/env python3
"""Unit tests for the api-gateway source-side functions (no stack needed).

Covers the gdrive mtime capture added to gateway/app.py (_entry_for,
walk_source) and its propagation into the OWUI upload multipart metadata
(gateway/owui.py upload_file). The HTTP layer is monkeypatched (urllib), so no
running stack is required. Run:  python3 tests/test_gateway_unit.py -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

GATEWAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docker", "gateway")
SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, os.path.abspath(GATEWAY))
# app.py imports kb_ignore (scripts/kb_ignore.py) -- put scripts/ on sys.path so
# that import resolves under pytest (which only had gateway/ here before).
sys.path.insert(0, os.path.abspath(SCRIPTS))
# No sys.modules["owui"] clash to evict: the skill's wrapper used to be named
# `owui` (skills/claude/scripts/owui.py) and collided with gateway/owui.py under
# pytest's single-process collection. The skill module is now `kb`, so
# gateway/owui.py binds unambiguously here.
import app  # noqa: E402  (gateway/app.py)
import owui  # noqa: E402  (gateway/owui.py)

_EPOCH = 1_700_000_000  # fixed, deterministic mtime source


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class EntryMtimeTests(unittest.TestCase):
    """_entry_for / walk_source capture the source file mtime as ISO-8601 UTC."""

    def test_entry_for_captures_mtime(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(b"pdf-bytes")
            path = tf.name
        try:
            os.utime(path, (_EPOCH, _EPOCH))
            e = app._entry_for(path, os.path.dirname(path), {"pdf"}, 100 << 20)
            self.assertIsNotNone(e)
            self.assertIn("mtime", e)
            self.assertEqual(e["mtime"], _iso(_EPOCH))
            # parses as ISO-8601 UTC (raises ValueError otherwise)
            time.strptime(e["mtime"], "%Y-%m-%dT%H:%M:%SZ")
        finally:
            os.unlink(path)

    def test_entry_for_skips_disallowed_ext(self):
        # A disallowed extension returns None (mtime capture never reached).
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as tf:
            tf.write(b"x")
            path = tf.name
        try:
            self.assertIsNone(app._entry_for(path, os.path.dirname(path), {"pdf"}, 100 << 20))
        finally:
            os.unlink(path)

    def test_walk_source_entries_carry_mtime(self):
        root = tempfile.mkdtemp()
        try:
            fpath = os.path.join(root, "a.pdf")
            with open(fpath, "wb") as f:
                f.write(b"x")
            os.utime(fpath, (_EPOCH, _EPOCH))
            entries = app.walk_source(root, {"pdf"}, 100 << 20)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["filename"], "a.pdf")
            self.assertEqual(entries[0]["mtime"], _iso(_EPOCH))
        finally:
            import shutil
            shutil.rmtree(root)

    def test_walk_source_skips_meta_sidecar(self):
        # gdrive-meta sidecars must NOT be indexed. `.meta` (YAML) is dropped by
        # the ext allowlist (meta ∉ DEFAULT_ALLOW); `.meta.json` (JSON, ext `json`
        # which IS allowed) is dropped by the name skip in _entry_for. Guards the
        # "exclude sidecars from indexing" claim (./root/.kb-ignore globals protect
        # the local sidecars from sync deletion; the walk excludes them from the
        # index).
        root = tempfile.mkdtemp()
        try:
            with open(os.path.join(root, "a.pdf"), "wb") as f:
                f.write(b"pdf-bytes")
            with open(os.path.join(root, "a.pdf.meta"), "wb") as f:
                f.write(b"id: x\nname: a.pdf\n")
            with open(os.path.join(root, "a.pdf.meta.json"), "wb") as f:
                f.write(b'{"id":"x","grounded":true}')
            with open(os.path.join(root, "notes.txt"), "wb") as f:
                f.write(b"notes")
            entries = app.walk_source(root, app.DEFAULT_ALLOW, 100 << 20)
            names = sorted(e["filename"] for e in entries)
            self.assertEqual(names, ["a.pdf", "notes.txt"])
            self.assertNotIn("a.pdf.meta", names)
            self.assertNotIn("a.pdf.meta.json", names)
        finally:
            import shutil
            shutil.rmtree(root)

    def test_gdrive_meta_for_reads_sidecar(self):
        # _gdrive_meta_for parses the <file>.meta.json sidecar into a dict; a
        # missing sidecar returns None (the upload proceeds without gdrive meta).
        root = tempfile.mkdtemp()
        try:
            src = os.path.join(root, "a.pdf")
            with open(src, "wb") as f:
                f.write(b"x")
            with open(src + ".meta.json", "w", encoding="utf-8") as f:
                f.write('{"grounded": true, "labels": ["grounded"]}')
            self.assertEqual(app._gdrive_meta_for(src),
                             {"grounded": True, "labels": ["grounded"]})
            os.unlink(src + ".meta.json")
            self.assertIsNone(app._gdrive_meta_for(src))
        finally:
            import shutil
            shutil.rmtree(root)

    def test_gdrive_meta_for_malformed_returns_none(self):
        # A malformed sidecar never blocks the upload: logged + None.
        root = tempfile.mkdtemp()
        try:
            src = os.path.join(root, "a.pdf")
            with open(src, "wb") as f:
                f.write(b"x")
            with open(src + ".meta.json", "w", encoding="utf-8") as f:
                f.write("{not valid json")
            self.assertIsNone(app._gdrive_meta_for(src))
        finally:
            import shutil
            shutil.rmtree(root)


class TestKbIgnore(unittest.TestCase):
    """apply_kb_ignores: the additive .kb-ignore ancestor-chain deny-list.
    Each test builds a temp KB_SOURCE_ROOT with per-directory .kb-ignore files
    and runs the filter on synthetic walk entries ({path, filename}); no stack.
    Semantics: gitignore-style -- no-slash matches a basename at any depth; a
    slash pattern is anchored at the .kb-ignore's own dir; `*` does not cross
    `/`, `**` does; `!` re-includes; ancestor .kb-ignore files accumulate
    (shallowest first, last match wins)."""

    def _ignore_at(self, root, rel, text):
        """Write a .kb-ignore at <root>/<rel>/.kb-ignore (rel='' = root)."""
        d = os.path.join(root, rel) if rel else root
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".kb-ignore"), "w", encoding="utf-8") as f:
            f.write(text)

    def _e(self, path, filename):
        return {"path": path, "filename": filename}

    def test_missing_ignore_is_noop(self):
        # No .kb-ignore anywhere -> files returned unchanged (allowed() returns
        # True for every path; the _entry_for sidecar skip is independent).
        root = tempfile.mkdtemp()
        try:
            files = [self._e("", "a.pdf"), self._e("sub", "b.md")]
            self.assertEqual(app.apply_kb_ignores(files, "gdrive", root), files)
        finally:
            import shutil; shutil.rmtree(root)

    def test_globals_apply_to_every_kb(self):
        # root/.kb-ignore (globals) applies to EVERY KB dir, not just gdrive.
        root = tempfile.mkdtemp()
        try:
            self._ignore_at(root, "", "*.json\n")
            files = [self._e("", "a.json"), self._e("", "b.pdf")]
            out = app.apply_kb_ignores(files, "mydocs", root)
            self.assertEqual([f["filename"] for f in out], ["b.pdf"])
        finally:
            import shutil; shutil.rmtree(root)

    def test_per_drive_scoped_not_global(self):
        # root/gdrive/.kb-ignore drops gdrive/secret.txt but NOT mydocs/secret.txt
        # (a per-dir .kb-ignore is relative to its own location, not a global).
        root = tempfile.mkdtemp()
        try:
            self._ignore_at(root, "gdrive", "secret.txt\n")
            files = [self._e("", "secret.txt")]
            self.assertEqual(app.apply_kb_ignores(files, "gdrive", root), [])
            self.assertEqual(app.apply_kb_ignores(files, "mydocs", root), files)
        finally:
            import shutil; shutil.rmtree(root)

    def test_ancestor_chain_accumulates(self):
        # root/.kb-ignore (*.pyc) + root/gdrive/Team Mtgs/.kb-ignore (/notes.md):
        # both apply to gdrive/Team Mtgs/notes.md; the global applies elsewhere too.
        root = tempfile.mkdtemp()
        try:
            self._ignore_at(root, "", "*.pyc\n")
            self._ignore_at(root, "gdrive/Team Mtgs", "/notes.md\n")
            files = [
                self._e("", "a.pyc"),              # global *.pyc -> drop
                self._e("Team Mtgs", "notes.md"),  # /notes.md anchored at Team Mtgs -> drop
                self._e("", "keep.pdf"),           # nothing matches -> keep
                self._e("Team Mtgs", "keep.pdf"),  # nothing matches -> keep
            ]
            out = app.apply_kb_ignores(files, "gdrive", root)
            self.assertEqual(sorted(f["filename"] for f in out), ["keep.pdf", "keep.pdf"])
        finally:
            import shutil; shutil.rmtree(root)

    def test_negation_reinclude(self):
        # root/.kb-ignore: *.pdf then !keep.pdf -> keep.pdf re-included; a deeper
        # drop.pdf is still denied (last match wins).
        root = tempfile.mkdtemp()
        try:
            self._ignore_at(root, "", "*.pdf\n!keep.pdf\n")
            files = [self._e("", "drop.pdf"), self._e("", "keep.pdf"),
                     self._e("sub", "drop2.pdf")]
            out = app.apply_kb_ignores(files, "gdrive", root)
            self.assertEqual([f["filename"] for f in out], ["keep.pdf"])
        finally:
            import shutil; shutil.rmtree(root)

    def test_star_allowlist(self):
        # root/gdrive/.kb-ignore: * then !subtree/** -> only subtree/** kept (the
        # ! pattern is relative to gdrive/, where the .kb-ignore lives).
        root = tempfile.mkdtemp()
        try:
            self._ignore_at(root, "gdrive", "*\n!subtree/**\n")
            files = [
                self._e("", "a.pdf"),                 # * -> drop
                self._e("subtree", "keep.md"),        # !subtree/** -> keep
                self._e("subtree/deep", "keep2.md"),  # !subtree/** -> keep
            ]
            out = app.apply_kb_ignores(files, "gdrive", root)
            self.assertEqual(sorted(f["filename"] for f in out), ["keep.md", "keep2.md"])
        finally:
            import shutil; shutil.rmtree(root)

    def test_glob_star_does_not_cross_slash(self):
        # "sub/*.pdf" (slash pattern, anchored at the .kb-ignore's dir): drops
        # sub/a.pdf but NOT sub/deep/b.pdf (* does not cross /) and NOT foo/sub/a2.pdf
        # (anchored at gdrive/, foo/ comes first). "**" crosses "/".
        root = tempfile.mkdtemp()
        try:
            self._ignore_at(root, "gdrive", "sub/*.pdf\nsub2/**/*.pdf\n")
            files = [
                self._e("sub", "a.pdf"),           # sub/*.pdf -> drop
                self._e("sub/deep", "b.pdf"),      # sub/*.pdf no (crosses /) -> keep
                self._e("foo/sub", "a2.pdf"),      # sub/*.pdf no (anchored, foo/ first) -> keep
                self._e("sub2/deep", "c.pdf"),     # sub2/**/*.pdf -> drop
            ]
            out = app.apply_kb_ignores(files, "gdrive", root)
            self.assertEqual(sorted(f["filename"] for f in out), ["a2.pdf", "b.pdf"])
        finally:
            import shutil; shutil.rmtree(root)

    def test_anchored_at_dir_root(self):
        # root/gdrive/sub/.kb-ignore: /a.pdf anchored at sub -> drops sub/a.pdf
        # but NOT sub/deep/a.pdf (the anchor is sub, not deeper).
        root = tempfile.mkdtemp()
        try:
            self._ignore_at(root, "gdrive/sub", "/a.pdf\n")
            files = [
                self._e("sub", "a.pdf"),           # /a.pdf anchored at sub -> drop
                self._e("sub/deep", "a.pdf"),      # rel is deep/a.pdf -> keep
            ]
            out = app.apply_kb_ignores(files, "gdrive", root)
            self.assertEqual([f["filename"] for f in out], ["a.pdf"])
        finally:
            import shutil; shutil.rmtree(root)

    def test_kb_ignore_file_itself_not_indexed(self):
        # .kb-ignore is a dot-name (walk_source drops dot-names in a normal walk);
        # _entry_for hardcodes the skip for the sidecar/.kb-ignore names. Assert
        # it returns None.
        root = tempfile.mkdtemp()
        try:
            self._ignore_at(root, "gdrive", "*.pdf\n")
            p = os.path.join(root, "gdrive", ".kb-ignore")
            self.assertIsNone(app._entry_for(p, os.path.dirname(p), {"pdf"}, 100 << 20))
        finally:
            import shutil; shutil.rmtree(root)


class _Resp:
    """Fake urllib urlopen context manager returning a fixed JSON body."""

    def __init__(self, body_bytes, status=200):
        self._b = body_bytes
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def _multipart_fields(body: bytes):
    """Parse upload_file's multipart body -> {field_name: raw_value_bytes}."""
    fields = {}
    # Split on the first boundary (the line starting with --).
    parts = body.split(b"\r\n--")
    for part in parts:
        m = b'name="'
        i = part.find(m)
        if i < 0:
            continue
        j = part.find(b'"', i + len(m))
        name = part[i + len(m):j].decode()
        # value is after the blank line (\r\n\r\n) to the end of this part
        k = part.find(b"\r\n\r\n")
        if k < 0:
            continue
        fields[name] = part[k + 4:].rstrip(b"\r\n--")
    return fields


class UploadFileMtimeTests(unittest.TestCase):
    """upload_file puts mtime into the multipart metadata field (or omits it)."""

    def _capture_upload(self, mtime):
        captured = {}

        def _urlopen(req, timeout=None):
            captured["data"] = req.data  # the multipart body bytes
            return _Resp(b'{"id":"fid-new"}')

        with mock.patch.object(owui.urllib.request, "urlopen", _urlopen):
            owui.upload_file("admin-key", "kb1", "sha-abc", "dir-1",
                             "doc.pdf", b"file-bytes", mtime=mtime)
        return captured["data"]

    def test_includes_mtime_when_provided(self):
        body = self._capture_upload("2025-10-30T16:50:57Z")
        fields = _multipart_fields(body)
        meta = json.loads(fields["metadata"].decode())
        self.assertEqual(meta["knowledge_id"], "kb1")
        self.assertEqual(meta["file_hash"], "sha-abc")
        self.assertEqual(meta["directory_id"], "dir-1")
        self.assertEqual(meta["mtime"], "2025-10-30T16:50:57Z")
        # the file field carries the raw bytes
        self.assertEqual(fields["file"], b"file-bytes")

    def test_omits_mtime_when_none(self):
        # Backward-compatible: no mtime -> no mtime key in metadata.
        body = self._capture_upload(None)
        meta = json.loads(_multipart_fields(body)["metadata"].decode())
        self.assertNotIn("mtime", meta)
        self.assertEqual(set(meta), {"knowledge_id", "file_hash", "directory_id"})

    def test_includes_gdrive_meta_when_provided(self):
        # gdrive_meta (the parsed .meta.json sidecar) is stored under metadata.gdrive
        # so OWUI persists it into File.meta.data.gdrive.
        captured = {}

        def _urlopen(req, timeout=None):
            captured["data"] = req.data
            return _Resp(b'{"id":"fid-new"}')

        gmeta = {"grounded": True, "labels": ["grounded"],
                 "description": "[grounded]"}
        with mock.patch.object(owui.urllib.request, "urlopen", _urlopen):
            owui.upload_file("admin-key", "kb1", "sha-abc", "dir-1", "doc.pdf",
                             b"file-bytes", mtime="2025-10-30T16:50:57Z",
                             gdrive_meta=gmeta)
        meta = json.loads(_multipart_fields(captured["data"])["metadata"].decode())
        self.assertEqual(meta["gdrive"], gmeta)
        self.assertEqual(meta["mtime"], "2025-10-30T16:50:57Z")

    def test_omits_gdrive_meta_when_none(self):
        # Backward-compatible: no gdrive_meta -> no gdrive key in metadata.
        body = self._capture_upload(None)
        meta = json.loads(_multipart_fields(body)["metadata"].decode())
        self.assertNotIn("gdrive", meta)


class _FakeHeaders:
    """Minimal Header-get for Handler tests (only Authorization is read)."""

    def __init__(self, auth="Bearer caller-key"):
        self._auth = auth

    def get(self, key, default=""):
        return self._auth if key == "Authorization" else default


class _FakeHandler:
    """A stand-in for app.Handler so _retrieve_kb can run without an HTTP
    socket. Captures the _ok result; GatewayError propagates to the caller
    (the test catches it and asserts status/message)."""

    def __init__(self, body, auth="Bearer caller-key"):
        self.headers = _FakeHeaders(auth)
        self._body = body
        self.sent = None  # ("ok", obj)

    def _read_body(self):
        return self._body

    def _ok(self, obj):
        self.sent = ("ok", obj)


# A synthetic OWUI /query/collection raw response (one collection, 2 chunks).
_RAW = {
    "documents": [["reg text", "more text"]],
    "distances": [[0.12, 0.40]],
    "metadatas": [[
        {"file_id": "fid-1", "file_name": "regs.pdf", "page": 3,
         "start_index": 100, "source": "regs.pdf", "mtime": "2026-01-02T00:00:00Z"},
        {},
    ]],
}


class TestRetrieveRoute(unittest.TestCase):
    """POST /retrieve: validation, mode->forwarded-args mapping, error map,
    _flatten_hits, score_order. Mocks owui.query_collection (handler) and
    owui.urllib.request.urlopen (forwarder body). No stack needed."""

    KB = "550e8400-e29b-41d4-a716-446655440000"

    def _run(self, body, qc_return=_RAW, qc_raises=None):
        """Invoke Handler._retrieve_kb on a _FakeHandler; mock owui.query_collection
        to return qc_return or raise qc_raises. Returns ("ok", obj) or
        ("err", status, message)."""
        h = _FakeHandler(body)

        def _qc(*a, **kw):
            if qc_raises is not None:
                raise qc_raises
            return qc_return

        with mock.patch.object(owui, "query_collection", side_effect=_qc):
            try:
                app.Handler._retrieve_kb(h, None, body)
            except app.GatewayError as e:
                return ("err", e.status, e.message)
        return h.sent

    # -- mode -> forwarded args (handler -> owui.query_collection) --

    def test_mode_maps_to_hybrid_and_weight(self):
        cases = {  # mode -> (hybrid, bm25_weight)
            "hybrid": (True, None), "lexical": (True, 1.0), "vector": (False, None),
        }
        for mode, (hy, bw) in cases.items():
            captured = {}

            def _qc(api_key, kb_id, query, hybrid, hybrid_bm25_weight, k):
                captured.update(api_key=api_key, kb_id=kb_id, query=query,
                                hybrid=hybrid, hybrid_bm25_weight=hybrid_bm25_weight, k=k)
                return _RAW

            with mock.patch.object(owui, "query_collection", side_effect=_qc):
                h = _FakeHandler({"kb_id": self.KB, "query": "CAP_ENGAGE",
                                  "mode": mode, "k": 7})
                body = {"kb_id": self.KB, "query": "CAP_ENGAGE", "mode": mode, "k": 7}
                app.Handler._retrieve_kb(h, None, body)
            self.assertEqual(captured["hybrid"], hy, mode)
            self.assertEqual(captured["hybrid_bm25_weight"], bw, mode)
            self.assertEqual(captured["kb_id"], self.KB, mode)
            self.assertEqual(captured["k"], 7, mode)
            self.assertEqual(captured["api_key"], "caller-key", mode)
            self.assertIs(captured["query"], "CAP_ENGAGE", mode)

    def test_mode_omitted_defaults_hybrid(self):
        captured = {}

        def _qc(api_key, kb_id, query, hybrid, hybrid_bm25_weight, k):
            captured["hybrid"] = hybrid
            captured["bm25"] = hybrid_bm25_weight
            return _RAW

        with mock.patch.object(owui, "query_collection", side_effect=_qc):
            app.Handler._retrieve_kb(_FakeHandler({"kb_id": self.KB, "query": "x"}),
                                     None,
                                     {"kb_id": self.KB, "query": "x"})
        self.assertTrue(captured["hybrid"])
        self.assertIsNone(captured["bm25"])

    def test_lexical_dsl_prefixes_sentinel(self):
        # mode=lexical-dsl: the handler prefixes LEXICAL_DSL_PREFIX to the query
        # (the sentinel contract with the kb-openwebui image), hybrid=True,
        # bm25_weight=1.0 (vector arm skipped). The agent never types the sentinel.
        captured = {}

        def _qc(api_key, kb_id, query, hybrid, hybrid_bm25_weight, k):
            captured.update(query=query, hybrid=hybrid,
                            hybrid_bm25_weight=hybrid_bm25_weight)
            return _RAW

        with mock.patch.object(owui, "query_collection", side_effect=_qc):
            h = _FakeHandler({"kb_id": self.KB, "query": "CAP_ENGAGE",
                              "mode": "lexical-dsl", "k": 5})
            app.Handler._retrieve_kb(h, None,
                                     {"kb_id": self.KB, "query": "CAP_ENGAGE",
                                      "mode": "lexical-dsl", "k": 5})
        self.assertEqual(captured["hybrid"], True)
        self.assertEqual(captured["hybrid_bm25_weight"], 1.0)
        self.assertEqual(captured["query"],
                         app.LEXICAL_DSL_PREFIX + "CAP_ENGAGE")
        # the sentinel must equal the OWUI-image constant (apply_lexical_dsl.SENTINEL)
        self.assertEqual(app.LEXICAL_DSL_PREFIX, "KB_LEXICAL_DSL_V1::")
        # the response echoes the mode (not the prefixed query)
        self.assertEqual(h.sent[1]["mode"], "lexical-dsl")

    # -- forwarder body (owui.query_collection -> OWUI HTTP) --

    def _capture_forwarded_body(self, mode):
        captured = {}

        def _urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            captured["auth"] = req.headers.get("Authorization")
            captured["url"] = req.full_url
            return _Resp(b'{"documents":[["t"]],"distances":[[0.1]],"metadatas":[[{}]]}')

        with mock.patch.object(owui.urllib.request, "urlopen", _urlopen):
            owui.query_collection("caller-key", self.KB, "q", *app.RETRIEVE_MODES[mode], 5)
        return captured

    def test_forwarder_body_hybrid_omits_weight(self):
        c = self._capture_forwarded_body("hybrid")
        self.assertEqual(c["body"]["collection_names"], [self.KB])
        self.assertEqual(c["body"]["hybrid"], True)
        self.assertEqual(c["body"]["k"], 5)
        self.assertEqual(c["body"]["query"], "q")
        self.assertNotIn("hybrid_bm25_weight", c["body"])
        self.assertEqual(c["auth"], "Bearer caller-key")

    def test_forwarder_body_lexical_sends_weight_one(self):
        c = self._capture_forwarded_body("lexical")
        self.assertEqual(c["body"]["hybrid"], True)
        self.assertEqual(c["body"]["hybrid_bm25_weight"], 1.0)

    def test_forwarder_body_lexical_dsl_sends_weight_one(self):
        # the forwarder (owui.query_collection -> OWUI HTTP) sends the query
        # verbatim -- the sentinel prefix is the HANDLER's job (tested above);
        # the forwarder just carries weight=1.0 like lexical. The tuple stays
        # 2-element, so the *app.RETRIEVE_MODES[mode] splat still works (M3).
        c = self._capture_forwarded_body("lexical-dsl")
        self.assertEqual(c["body"]["hybrid"], True)
        self.assertEqual(c["body"]["hybrid_bm25_weight"], 1.0)

    def test_forwarder_body_vector_hybrid_false(self):
        c = self._capture_forwarded_body("vector")
        self.assertEqual(c["body"]["hybrid"], False)
        self.assertNotIn("hybrid_bm25_weight", c["body"])

    def test_forwarder_raises_owuierror_on_non200(self):
        def _urlopen(req, timeout=None):
            raise owui.urllib.error.HTTPError(req.full_url, 403, "forbidden",
                                               {}, io.BytesIO(b'{"detail":"no"}'))
        with mock.patch.object(owui.urllib.request, "urlopen", _urlopen):
            with self.assertRaises(owui.OwuiError) as cm:
                owui.query_collection("k", self.KB, "q", True, None, 5)
        self.assertEqual(cm.exception.code, 403)

    # -- validation matrix --

    def test_missing_kb_id_400(self):
        self.assertEqual(self._run({"query": "x"})[0], "err")
        self.assertEqual(self._run({"query": "x"})[1], 400)

    def test_non_uuid_kb_id_400(self):
        r = self._run({"kb_id": "not-a-uuid", "query": "x"})
        self.assertEqual(r[0], "err")
        self.assertEqual(r[1], 400)

    def test_empty_query_400(self):
        for q in ["", "   "]:
            r = self._run({"kb_id": self.KB, "query": q})
            self.assertEqual(r[1], 400, q)

    def test_missing_query_400(self):
        self.assertEqual(self._run({"kb_id": self.KB})[1], 400)

    def test_bad_mode_400(self):
        r = self._run({"kb_id": self.KB, "query": "x", "mode": "fuzzy"})
        self.assertEqual(r[1], 400)

    def test_k_rejects_bad_values(self):
        for bad in [0, 999, True, "5", 1.5, None]:
            r = self._run({"kb_id": self.KB, "query": "x", "k": bad})
            self.assertEqual(r[1], 400, bad)

    def test_k_omitted_uses_default(self):
        captured = {}

        def _qc(*a, **kw):
            captured["k"] = a[-1]
            return _RAW

        with mock.patch.object(owui, "query_collection", side_effect=_qc):
            app.Handler._retrieve_kb(_FakeHandler({"kb_id": self.KB, "query": "x"}),
                                     None,
                                     {"kb_id": self.KB, "query": "x"})
        self.assertEqual(captured["k"], app.RETRIEVE_K_DEFAULT)

    def test_query_too_long_400(self):
        r = self._run({"kb_id": self.KB, "query": "x" * (app.MAX_QUERY + 1)})
        self.assertEqual(r[1], 400)

    # -- error map (OwuiError.code -> gateway status) --

    def test_error_map_403_echoed(self):
        r = self._run({"kb_id": self.KB, "query": "x"},
                      qc_raises=owui.OwuiError("denied", code=403))
        self.assertEqual(r[0], "err")
        self.assertEqual(r[1], 403)

    def test_error_map_401_echoed(self):
        r = self._run({"kb_id": self.KB, "query": "x"},
                      qc_raises=owui.OwuiError("badkey", code=401))
        self.assertEqual(r[1], 401)

    def test_error_map_500_to_502(self):
        r = self._run({"kb_id": self.KB, "query": "x"},
                      qc_raises=owui.OwuiError("boom", code=500))
        self.assertEqual(r[1], 502)

    def test_error_map_transport_to_503(self):
        r = self._run({"kb_id": self.KB, "query": "x"},
                      qc_raises=owui.OwuiError("unreachable"))  # code=None
        self.assertEqual(r[1], 503)

    # -- success response shape + score_order + flatten --

    def test_success_response_keys(self):
        r = self._run({"kb_id": self.KB, "query": "CAP_ENGAGE", "mode": "hybrid", "k": 3})
        self.assertEqual(r[0], "ok")
        obj = r[1]
        self.assertEqual(obj["kb_id"], self.KB)
        self.assertEqual(obj["mode"], "hybrid")
        self.assertEqual(obj["k"], 3)
        self.assertEqual(obj["score_order"], "desc")
        self.assertEqual(len(obj["hits"]), 2)
        # first hit carries the metadata; second hit (empty meta) -> defaults
        self.assertEqual(obj["hits"][0]["file"], "regs.pdf")
        self.assertEqual(obj["hits"][0]["file_id"], "fid-1")
        self.assertEqual(obj["hits"][1]["file"], "")
        self.assertIsNone(obj["hits"][1]["page"])

    def test_score_order_per_mode(self):
        for mode, order in [("hybrid", "desc"), ("lexical", "desc"),
                            ("lexical-dsl", "desc"), ("vector", "asc")]:
            r = self._run({"kb_id": self.KB, "query": "x", "mode": mode})
            self.assertEqual(r[1]["score_order"], order, mode)
            self.assertEqual(r[1]["mode"], mode, mode)

    def test_flatten_hits_eight_keys(self):
        hits = app._flatten_hits(_RAW)
        self.assertEqual(len(hits), 2)
        for h in hits:
            self.assertEqual(set(h),
                             {"distance", "file", "file_id", "page", "start_index",
                              "source", "mtime", "text"})
        self.assertEqual(hits[0]["distance"], 0.12)
        self.assertEqual(hits[1]["distance"], 0.40)
        # second chunk has empty metadata -> defaults (file "", page None)
        self.assertEqual(hits[1]["file"], "")
        self.assertIsNone(hits[1]["page"])
        self.assertEqual(hits[1]["text"], "more text")


class _DrainFake:
    """Stand-in with only what _send/_drain_body/_read_body touch, so the
    keep-alive body-drain logic in app.Handler can be unit-tested without an
    HTTP socket. rfile.read is a Mock so the test asserts it was/wasn't called.
    _drain_body/_read_body are the real app.Handler methods (aliased below) so
    the flag + read behavior exercised is the production code, not a copy."""

    def __init__(self, content_length=0, body=b"{}"):
        self.headers = {"Content-Length": str(content_length)} if content_length else {}
        self.rfile = mock.Mock()
        self.rfile.read.return_value = body
        self.wfile = mock.Mock()
        self._body_consumed = False
        self.sent_code = None

    def send_response(self, code):
        self.sent_code = code

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass


# Bind the real app.Handler body methods onto the fake so _send's
# self._drain_body()/self._read_body() resolve to the production code.
_DrainFake._drain_body = app.Handler._drain_body
_DrainFake._read_body = app.Handler._read_body


class TestKeepAliveBodyDrain(unittest.TestCase):
    """A POST route that does _auth() before _read_body() leaves the body
    unread on a 401; with HTTP/1.1 keep-alive, Caddy reuses the upstream
    connection and the next request parses the stale body as its request
    line -> 501 'Unsupported method <body>POST'. _send() must drain an
    unconsumed body before responding. No stack needed."""

    def test_send_drains_unread_body_on_error(self):
        # 401 path: _auth() raised before _read_body -> _body_consumed False.
        f = _DrainFake(50, b'{"kb_id":"not-a-uuid","query":"x","mode":"hybrid"}')
        app.Handler._send(f, 401, {"error": "auth required"})
        f.rfile.read.assert_called_once()
        self.assertEqual(f.rfile.read.call_args[0][0], min(50, app.MAX_BODY))
        self.assertEqual(f.sent_code, 401)
        self.assertFalse(f._body_consumed)  # reset for the next keep-alive request

    def test_send_skips_drain_when_body_consumed(self):
        # success path: the handler called _read_body -> _body_consumed True.
        f = _DrainFake(50)
        f._body_consumed = True
        app.Handler._send(f, 200, {"ok": True})
        f.rfile.read.assert_not_called()
        self.assertFalse(f._body_consumed)  # reset after the response

    def test_send_no_body_is_noop(self):
        # GET /health: no Content-Length -> drain is a no-op, rfile untouched.
        f = _DrainFake(0)
        app.Handler._send(f, 200, {"status": "ok"})
        f.rfile.read.assert_not_called()

    def test_read_body_marks_consumed_even_on_bad_json(self):
        # _read_body reads the bytes THEN parses; an invalid-JSON 400 must NOT
        # cause _send to drain again (which would read the NEXT request). The
        # flag is set right after rfile.read, before the parse.
        f = _DrainFake(20, b"not json")
        with self.assertRaises(app.GatewayError) as cm:
            f._read_body()
        self.assertEqual(cm.exception.status, 400)
        self.assertTrue(f._body_consumed)  # set despite the parse failure


class TestValidateDir(unittest.TestCase):
    """_validate_dir: the KB name is one top-level subdir under KB_SOURCE_ROOT.
    Reject empty, `.`, `..`, multi-segment, wildcard, non-existent; accept a
    single segment incl. a dot-prefixed name (`.tests`). Real temp source root
    so isdir + realpath checks exercise. No stack needed."""

    def setUp(self):
        self._root = tempfile.mkdtemp()
        for d in ("gdrive", ".tests"):
            os.mkdir(os.path.join(self._root, d))
        self._env = mock.patch.dict(os.environ, {"KB_SOURCE_ROOT": self._root})
        self._env.start()
        self.addCleanup(self._env.stop)
        import shutil

        def _rm():
            shutil.rmtree(self._root, ignore_errors=True)
        self.addCleanup(_rm)

    def _qs(self, dir):
        return "dir=%s" % dir if dir is not None else ""

    def _call(self, dir):
        # _validate_dir reads `dir` from the parsed qs dict (app._qs parses the
        # querystring). Pass a real querystring through _qs via a tiny shim:
        # build a qs string and let urllib.parse.parse_qs read it.
        import urllib.parse as up
        qs = "" if dir is None else ("dir=%s" % up.quote(dir or "", safe=""))
        return app._validate_dir(qs, self._root)

    def _err(self, dir):
        with self.assertRaises(app.GatewayError) as cm:
            self._call(dir)
        return cm.exception.status

    def test_single_segment_accepted(self):
        self.assertEqual(self._call("gdrive"), "gdrive")

    def test_dot_prefixed_segment_accepted(self):
        self.assertEqual(self._call(".tests"), ".tests")

    def test_empty_rejected(self):
        self.assertEqual(self._err(""), 400)
        self.assertEqual(self._err(None), 400)

    def test_dot_and_dotdot_rejected(self):
        self.assertEqual(self._err("."), 400)
        self.assertEqual(self._err(".."), 400)

    def test_multi_segment_rejected(self):
        for d in ("a/b", "gdrive/sub", "a\\b", "/gdrive"):
            self.assertEqual(self._err(d), 400, d)

    def test_wildcard_rejected(self):
        for d in ("gdrive*", "g?drive", "g[drive]", "x*"):
            self.assertEqual(self._err(d), 400, d)

    def test_nonexistent_rejected(self):
        self.assertEqual(self._err("no-such-dir"), 400)

    def test_dotdot_escape_rejected(self):
        # a single `..` is rejected explicitly; a constructed escape via a
        # symlink would also fail realpath containment, but `..` alone covers
        # the in-process gate.
        self.assertEqual(self._err(".."), 400)


class TestStatusRoute(unittest.TestCase):
    """GET /status?json=1: per-file size + top-level drain runtime/started_at.
    Mocks owui.list_file_status + walk_source + owui._admin_key + app.time.time.
    Uses a real temp KB_SOURCE_ROOT/gdrive so _validate_dir's isdir + realpath
    checks pass (walk_source itself is mocked). No stack needed."""

    KB = "550e8400-e29b-41d4-a716-446655440000"
    T0 = 1788069380  # a fixed created_at baseline (unix seconds)

    def setUp(self):
        self._src_root = tempfile.mkdtemp()
        os.mkdir(os.path.join(self._src_root, "gdrive"))
        self._env = mock.patch.dict(os.environ, {"KB_SOURCE_ROOT": self._src_root})
        self._env.start()
        self.addCleanup(self._env.stop)
        import shutil

        def _rm():
            shutil.rmtree(self._src_root, ignore_errors=True)
        self.addCleanup(_rm)

    def _run(self, file_status, now=None):
        """Invoke Handler._status(json=1) with mocked dependencies. Returns the
        summary dict captured by _FakeHandler._ok."""
        if now is None:
            now = self.T0 + 100
        h = _FakeHandler({}, auth="Bearer caller-key")
        qs = "kb_id=%s&dir=gdrive&json=1" % self.KB
        patches = [
            mock.patch.object(owui, "_admin_key", return_value="admin-key"),
            mock.patch.object(app, "walk_source", return_value=[{"path": "x"}]),
            mock.patch.object(owui, "list_file_status", return_value=file_status),
            mock.patch.object(app.time, "time", return_value=now),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        app.Handler._status(h, None, qs)
        self.assertEqual(h.sent[0], "ok")
        return h.sent[1]

    def test_size_and_runtime_fields(self):
        fs = [
            {"filename": "a.md", "status": "completed", "size": 100,
             "created_at": self.T0, "error": None},
            {"filename": "b.pdf", "status": "pending", "size": 200,
             "created_at": self.T0 + 10, "error": None},
            {"filename": "c.pdf", "status": "failed", "size": 300,
             "created_at": self.T0 + 20, "error": "boom"},
        ]
        d = self._run(fs, now=self.T0 + 100)
        # top-level drain runtime = now - min(created_at); started_at = that min.
        self.assertEqual(d["runtime"], 100)
        self.assertEqual(d["started_at"], self.T0)
        self.assertEqual(d["indexed_count"], 1)
        self.assertEqual(d["pending"], 1)
        self.assertEqual(d["failed"], 1)
        # per-file size carried through to each list
        self.assertEqual(d["indexed_files"][0]["size"], 100)
        self.assertEqual(d["pending_files"][0]["size"], 200)
        self.assertEqual(d["failed_files"][0]["size"], 300)
        self.assertEqual(d["failed_files"][0]["error"], "boom")

    def test_empty_kb_runtime_none(self):
        # no files -> started_at + runtime are None (no drain to time).
        d = self._run([], now=self.T0 + 100)
        self.assertIsNone(d["started_at"])
        self.assertIsNone(d["runtime"])
        self.assertEqual(d["indexed_count"], 0)

    def test_missing_created_at_excluded_from_min(self):
        # a file with created_at=None does not poison min(); it is skipped.
        fs = [
            {"filename": "a.md", "status": "completed", "size": 100,
             "created_at": self.T0, "error": None},
            {"filename": "b.md", "status": "completed", "size": 50,
             "created_at": None, "error": None},
        ]
        d = self._run(fs, now=self.T0 + 50)
        self.assertEqual(d["started_at"], self.T0)
        self.assertEqual(d["runtime"], 50)

    def test_human_size_helper(self):
        self.assertEqual(app._human_size(None), "-")
        self.assertEqual(app._human_size(500), "500 B")
        self.assertEqual(app._human_size(1268354), "1.2 MB")
        self.assertEqual(app._human_size(1073741824), "1.0 GB")

    def test_fmt_dur_helper(self):
        self.assertEqual(app._fmt_dur(None), "-")
        self.assertEqual(app._fmt_dur(100), "01m 40s")
        self.assertEqual(app._fmt_dur(3723), "1h 02m 03s")


if __name__ == "__main__":
    unittest.main()