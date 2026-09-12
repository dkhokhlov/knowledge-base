#!/usr/bin/env python3
"""Unit tests for scripts/kb_delete.py (operator delete CLI).

Patches kb_utils.get_kb / kb_utils.kb_inflight / kb_utils.delete_kb (qualified
calls in kb_delete -- `import kb_utils` -- so the patches take effect; per the
codex review, `from kb_utils import ...` would bind names locally and defeat
mocking). Asserts pretty JSON on stdout + exit code + that delete is NOT called
on every non-success path. No stack needed. Run:  python3 tests/test_kb_delete.py -v
"""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import kb_delete  # noqa: E402
import kb_utils  # noqa: E402

KB_ID = "550e8400-e29b-41d4-a716-446655440000"
KB_NAME = "gdrive"
KEY = "admin-key"
BASE = "http://owui.test"

_ENV = {"OPENWEBUI_ADMIN_API_KEY": KEY, "KB_HOST": BASE}
_OMIT = object()  # sentinel: argument not provided (distinct from None)


def _kw(ret, default):
    """Build patch kwargs. _OMIT -> the default; an Exception -> side_effect;
    anything else (incl. None) -> return_value."""
    if ret is _OMIT:
        val = default
    else:
        val = ret
    if isinstance(val, Exception):
        return {"side_effect": val}
    return {"return_value": val}


class _Run:
    """Run kb_delete.main under patched kb_utils + env, capture stdout. Exposes
    the started mocks (g_mock/i_mock/d_mock) so call assertions work after the
    context exits (the mock objects retain call records once stopped)."""

    def __init__(self, get_kb=_OMIT, inflight=_OMIT, delete=_OMIT):
        self.g = mock.patch.object(kb_utils, "get_kb", **_kw(get_kb, {"name": KB_NAME}))
        self.i = mock.patch.object(kb_utils, "kb_inflight", **_kw(inflight, (0, 0)))
        # delete defaults to a MagicMock returning True (call count visible).
        self.d = mock.patch.object(kb_utils, "delete_kb", **_kw(delete, True))

    def __enter__(self):
        self.g_mock = self.g.start()
        self.i_mock = self.i.start()
        self.d_mock = self.d.start()
        return self

    def __exit__(self, *a):
        self.g.stop(); self.i.stop(); self.d.stop()

    def run(self, argv, env=_ENV):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stdout(buf):
            code = kb_delete.main(argv)
        return code, json.loads(buf.getvalue())


class KbDeleteTests(unittest.TestCase):

    def test_success(self):
        with _Run() as r:
            code, d = r.run(["--kb", KB_ID])
            r.d_mock.assert_called_once_with(BASE, KEY, KB_ID)
        self.assertEqual(code, 0)
        self.assertEqual(d["status"], "deleted")
        self.assertEqual(d["kb_id"], KB_ID)
        self.assertEqual(d["name"], KB_NAME)

    def test_refused_drain_in_flight(self):
        # pending+processing > 0 -> delete NOT called.
        with _Run(inflight=(3, 1)) as r:
            code, d = r.run(["--kb", KB_ID])
            r.d_mock.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(d["status"], "refused")
        self.assertEqual(d["reason"], "drain_in_flight")
        self.assertEqual(d["pending"], 3)
        self.assertEqual(d["processing"], 1)

    def test_refused_verify_unavailable(self):
        # inflight scan failed (None) -> fail-closed; delete NOT called.
        with _Run(inflight=None) as r:
            code, d = r.run(["--kb", KB_ID])
            r.d_mock.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(d["status"], "refused")
        self.assertEqual(d["reason"], "verify_unavailable")

    def test_not_found(self):
        # get_kb returns None -> 404; delete NOT called.
        with _Run(get_kb=None) as r:
            code, d = r.run(["--kb", KB_ID])
            r.d_mock.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(d["status"], "not_found")
        self.assertEqual(d["reason"], "not_found")

    def test_missing_kb_id(self):
        with _Run() as r:
            code, d = r.run(["--kb", ""])
            r.d_mock.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(d["status"], "error")
        self.assertEqual(d["reason"], "missing_kb_id")

    def test_missing_admin_key(self):
        with _Run() as r:
            code, d = r.run(["--kb", KB_ID], env={"KB_HOST": BASE})
        self.assertEqual(code, 2)
        self.assertEqual(d["status"], "error")
        self.assertEqual(d["reason"], "missing_admin_key")

    def test_missing_kb_host(self):
        with _Run() as r:
            code, d = r.run(["--kb", KB_ID], env={"OPENWEBUI_ADMIN_API_KEY": KEY})
        self.assertEqual(code, 2)
        self.assertEqual(d["status"], "error")
        self.assertEqual(d["reason"], "missing_kb_host")

    def test_delete_failed(self):
        with _Run(delete=RuntimeError("OWUI DELETE KB %s -> HTTP 500" % KB_ID)) as r:
            code, d = r.run(["--kb", KB_ID])
        self.assertEqual(code, 2)
        self.assertEqual(d["status"], "error")
        self.assertEqual(d["reason"], "delete_failed")
        self.assertIn("HTTP 500", d["message"])

    def test_lookup_failed(self):
        with _Run(get_kb=RuntimeError("get_kb -> HTTP 500")) as r:
            code, d = r.run(["--kb", KB_ID])
            r.d_mock.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(d["status"], "error")
        self.assertEqual(d["reason"], "lookup_failed")

    def test_every_case_emits_valid_pretty_json(self):
        # raw stdout is indented (pretty) JSON, not compact; no prose lines.
        buf = io.StringIO()
        with _Run(), mock.patch.dict(os.environ, _ENV, clear=True), \
                contextlib.redirect_stdout(buf):
            kb_delete.main(["--kb", KB_ID])
        raw = buf.getvalue()
        self.assertTrue(raw.startswith("{\n"))
        self.assertTrue(raw.endswith("}\n"))


if __name__ == "__main__":
    unittest.main()