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

GATEWAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway")
sys.path.insert(0, os.path.abspath(GATEWAY))
# Native pytest collection imports every test module in ONE process. Another
# test module (test_output_json.py) imports a DIFFERENT module also named `owui`
# (skills/claude/scripts/owui.py). If that one was imported first,
# sys.modules["owui"] holds it, and our `import app` (gateway/app.py, which
# itself does `import owui`) would bind the wrong module. Evict any cached
# `owui` so app + this module re-resolve to gateway/owui.py. The other module's
# already-bound references are unaffected (module globals bind once, at import).
sys.modules.pop("owui", None)
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
        # "exclude sidecars from indexing" claim (gdrive-exclude.conf [*] protects
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
        for mode, order in [("hybrid", "desc"), ("lexical", "desc"), ("vector", "asc")]:
            r = self._run({"kb_id": self.KB, "query": "x", "mode": mode})
            self.assertEqual(r[1]["score_order"], order, mode)

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


if __name__ == "__main__":
    unittest.main()