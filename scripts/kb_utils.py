"""Shared KB-stack REST primitives (operator-scoped, admin key).

Zero-dependency stdlib (urllib). `base` is base-agnostic: KB_HOST host-side
(Caddy routes /api/* -> Open WebUI and /status -> api-gateway) or
127.0.0.1:8080 in-container. NOT coupled to the /kb skill wrapper.

These helpers are operator/admin tooling: they use an admin-role
OPENWEBUI_ADMIN_API_KEY. Keep that key private; do not hand it to agents.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


def http_request(base, admin_key, method, path, timeout=60):
    """Send an authenticated request and return (status, text) for ANY HTTP
    response (2xx..5xx). `urlopen()` RAISES `HTTPError` on non-2xx, so the
    HTTPError is caught here (before URLError) and normalized to
    (e.code, e.read().decode()); a `resp.status >= 300` branch in the caller
    would be dead otherwise. `URLError` (transport: DNS, refused, timeout)
    propagates as RuntimeError. Raises RuntimeError if admin_key is unset."""
    if not admin_key:
        raise RuntimeError("OPENWEBUI_ADMIN_API_KEY unset (run: make api-keys)")
    url = base.rstrip("/") + "/" + path.lstrip("/")
    req = urllib.request.Request(url, method=method,
                                 headers={"Authorization": "Bearer " + admin_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise RuntimeError("%s %s -> URLError: %s" % (method, url, e)) from e


def get_kb(base, admin_key, kb_id):
    """GET /api/v1/knowledge/{id} -> the KB dict on 200; None on 404 (not
    found); raise RuntimeError on other non-200 or bad JSON. The response
    carries no file_count -- do not look for one."""
    code, txt = http_request(base, admin_key, "GET", "/api/v1/knowledge/%s" % kb_id)
    if code == 200:
        try:
            return json.loads(txt)
        except ValueError as e:
            raise RuntimeError("get_kb %s -> bad JSON: %s" % (kb_id, e))
    if code == 404:
        return None
    raise RuntimeError("get_kb %s -> HTTP %s: %s" % (kb_id, code, txt[:200]))


def delete_kb(base, admin_key, kb_id):
    """OWUI REST DELETE /api/v1/knowledge/{id}/delete (the /delete suffix is
    mandatory; the bare path 405s). The route returns the DB-delete result as a
    JSON bool: HTTP 200 + body `true` = the KB row was deleted. A plain status
    check is NOT enough -- require body == true. OWUI wraps its vector
    delete_collection in try/except: pass, so a vector-cleanup failure is
    silently swallowed; the KB-row delete (body `true`) is what we require, and
    residual vectors surface as class 5b on the next kb-check. Raises on
    non-200, body != true, or missing admin key."""
    code, txt = http_request(base, admin_key, "DELETE",
                             "/api/v1/knowledge/%s/delete" % kb_id)
    if code == 200 and txt.strip() == "true":
        return True
    if code == 200:
        raise RuntimeError("OWUI DELETE KB %s -> body %r (row not deleted)"
                           % (kb_id, txt[:64]))
    raise RuntimeError("OWUI DELETE KB %s -> HTTP %s: %s" % (kb_id, code, txt[:200]))


def kb_inflight(base, admin_key, kb):
    """In-flight drain counts for a KB via the folded gateway /status:
    GET /status?kb=<name|uuid>&json=1 (Bearer admin key). Returns
    (pending, processing) on 200; None on any non-200 / transport / bad JSON
    (fail-closed: the caller refuses rather than delete blind). `kb` is the KB
    id (UUID) or name; the guard passes the UUID. Reuses the gateway's
    already-tested OWUI /files/ scan (no host-side paging duplication); works
    for root AND project-memory KBs (kb_id-keyed, scope-free)."""
    try:
        code, txt = http_request(base, admin_key, "GET",
                                 "/status?kb=%s&json=1" % urllib.parse.quote(str(kb), safe=""))
    except RuntimeError as e:  # transport (URLError): OWUI/gateway down
        log.warning("kb_inflight kb=%s -> transport: %s", kb, e)
        return None
    if code != 200:
        log.warning("kb_inflight kb=%s -> HTTP %s", kb, code)
        return None
    try:
        d = json.loads(txt)
    except ValueError:
        log.warning("kb_inflight kb=%s -> bad JSON", kb)
        return None
    if "error" in d:
        log.warning("kb_inflight kb=%s -> error: %s", kb, d["error"])
        return None
    try:
        return int(d.get("pending", 0)), int(d.get("processing", 0))
    except (TypeError, ValueError):
        log.warning("kb_inflight kb=%s -> non-int counts", kb)
        return None