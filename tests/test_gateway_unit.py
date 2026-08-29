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

    def __init__(self, body_bytes):
        self._b = body_bytes

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


if __name__ == "__main__":
    unittest.main()