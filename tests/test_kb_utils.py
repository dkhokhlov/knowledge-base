#!/usr/bin/env python3
"""Unit tests for scripts/kb_utils.py (shared OWUI REST primitives).

The HTTP layer is monkeypatched at urllib.request.urlopen. Crucially, the
non-2xx path is exercised with a REAL urllib.error.HTTPError (urlopen RAISES
on non-2xx; a fake response object with .status=404 would not exercise the
catch). No stack needed. Run:  python3 tests/test_kb_utils.py -v
"""
import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import kb_utils  # noqa: E402

KEY = "admin-key"
BASE = "http://owui.test"


class _Resp:
    """A stand-in for the urlopen context manager: .status + .read()."""

    def __init__(self, status, body=b""):
        self.status = status
        self._body = body if isinstance(body, bytes) else body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _httperr(code, body=b"err-body"):
    """A real urllib.error.HTTPError (the thing urlopen raises on non-2xx)."""
    return urllib.error.HTTPError(
        url=BASE + "/x", code=code, msg="err", hdrs=None,
        fp=io.BytesIO(body if isinstance(body, bytes) else body.encode()))


class HttpRequestsTests(unittest.TestCase):
    """http_request: (status, text) for any response; HTTPError caught before
    URLError; RuntimeError on no key / transport."""

    def test_2xx_returns_status_and_text(self):
        with mock.patch.object(urllib.request, "urlopen",
                               return_value=_Resp(200, b'{"ok":1}')):
            self.assertEqual(kb_utils.http_request(BASE, KEY, "GET", "/p"),
                             (200, '{"ok":1}'))

    def test_httperror_caught_returned_as_code_text(self):
        # urlopen RAISES HTTPError on non-2xx; http_request must catch it and
        # return (code, body), not propagate.
        with mock.patch.object(urllib.request, "urlopen", side_effect=_httperr(404, b"nope")):
            self.assertEqual(kb_utils.http_request(BASE, KEY, "GET", "/p"),
                             (404, "nope"))

    def test_urlerror_raises_runtimeerror(self):
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(RuntimeError):
                kb_utils.http_request(BASE, KEY, "GET", "/p")

    def test_no_key_raises(self):
        with self.assertRaises(RuntimeError):
            kb_utils.http_request(BASE, "", "GET", "/p")

    def test_authorization_header_set(self):
        captured = {}

        def side(req, *a, **k):
            captured["req"] = req
            return _Resp(200, b"{}")
        with mock.patch.object(urllib.request, "urlopen", side_effect=side):
            kb_utils.http_request(BASE, KEY, "GET", "/p")
        self.assertEqual(captured["req"].headers.get("Authorization"),
                         "Bearer " + KEY)

    def test_url_joins_base_and_path(self):
        captured = {}

        def side(req, *a, **k):
            captured["url"] = req.full_url
            return _Resp(200, b"{}")
        with mock.patch.object(urllib.request, "urlopen", side_effect=side):
            kb_utils.http_request(BASE + "/", KEY, "GET", "/p?q=1")
        self.assertEqual(captured["url"], "http://owui.test/p?q=1")


class GetKbTests(unittest.TestCase):
    def test_200_returns_dict(self):
        with mock.patch.object(urllib.request, "urlopen",
                               return_value=_Resp(200, b'{"id":"x","name":"k"}')):
            self.assertEqual(kb_utils.get_kb(BASE, KEY, "x"),
                             {"id": "x", "name": "k"})

    def test_404_returns_none(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=_httperr(404)):
            self.assertIsNone(kb_utils.get_kb(BASE, KEY, "x"))

    def test_other_non_200_raises(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=_httperr(500)):
            with self.assertRaises(RuntimeError):
                kb_utils.get_kb(BASE, KEY, "x")

    def test_no_key_raises(self):
        with self.assertRaises(RuntimeError):
            kb_utils.get_kb(BASE, "", "x")


class DeleteKbTests(unittest.TestCase):
    """Pins the /delete route + method + the body=='true' contract."""

    def _captured(self, resp):
        captured = {}

        def side(req, *a, **k):
            captured["method"] = req.method
            captured["url"] = req.full_url
            if isinstance(resp, Exception):
                raise resp
            return resp
        return mock.patch.object(urllib.request, "urlopen", side_effect=side), captured

    def test_200_true_returns_true_and_pins_route(self):
        patch, cap = self._captured(_Resp(200, b"true"))
        with patch:
            self.assertTrue(kb_utils.delete_kb(BASE, KEY, "kid-1"))
        self.assertEqual(cap["method"], "DELETE")
        self.assertTrue(cap["url"].endswith("/api/v1/knowledge/kid-1/delete"))

    def test_200_false_raises(self):
        patch, _ = self._captured(_Resp(200, b"false"))
        with patch:
            with self.assertRaises(RuntimeError):
                kb_utils.delete_kb(BASE, KEY, "kid-1")

    def test_non_200_raises(self):
        patch, _ = self._captured(_httperr(500, b"boom"))
        with patch:
            with self.assertRaises(RuntimeError):
                kb_utils.delete_kb(BASE, KEY, "kid-1")

    def test_no_key_raises(self):
        with self.assertRaises(RuntimeError):
            kb_utils.delete_kb(BASE, "", "kid-1")


class KbInflightTests(unittest.TestCase):
    """kb_inflight: (pending, processing) on 200; None on non-200 / transport /
    bad JSON / error-shape. Calls the folded /status?kb=<kb>&json=1."""

    def test_200_returns_pending_processing(self):
        body = json.dumps({"pending": 3, "processing": 1}).encode()
        with mock.patch.object(urllib.request, "urlopen", return_value=_Resp(200, body)):
            self.assertEqual(kb_utils.kb_inflight(BASE, KEY, "kid-1"), (3, 1))

    def test_200_zero_zero(self):
        body = json.dumps({"pending": 0, "processing": 0}).encode()
        with mock.patch.object(urllib.request, "urlopen", return_value=_Resp(200, body)):
            self.assertEqual(kb_utils.kb_inflight(BASE, KEY, "kid-1"), (0, 0))

    def test_404_returns_none(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=_httperr(404)):
            self.assertIsNone(kb_utils.kb_inflight(BASE, KEY, "kid-1"))

    def test_500_returns_none(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=_httperr(500)):
            self.assertIsNone(kb_utils.kb_inflight(BASE, KEY, "kid-1"))

    def test_transport_returns_none(self):
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")):
            self.assertIsNone(kb_utils.kb_inflight(BASE, KEY, "kid-1"))

    def test_error_shape_returns_none(self):
        # a gateway error body {"error": "..."} is valid JSON but not a status.
        body = json.dumps({"error": "KB not found"}).encode()
        with mock.patch.object(urllib.request, "urlopen", return_value=_Resp(200, body)):
            self.assertIsNone(kb_utils.kb_inflight(BASE, KEY, "kid-1"))

    def test_status_path_uses_kb_param(self):
        captured = {}

        def side(req, *a, **k):
            captured["url"] = req.full_url
            return _Resp(200, json.dumps({"pending": 0, "processing": 0}).encode())
        with mock.patch.object(urllib.request, "urlopen", side_effect=side):
            kb_utils.kb_inflight(BASE, KEY, "kid-1")
        self.assertIn("kb=kid-1", captured["url"])
        self.assertIn("/status", captured["url"])


if __name__ == "__main__":
    unittest.main()