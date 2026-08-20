"""Graphiti MCP client (Streamable HTTP, protocolVersion 2025-03-26).

One handshake per request (MVP): initialize -> capture Mcp-Session-Id ->
notifications/initialized -> tools/call -> close. Sessions are closed on exit
(best-effort). Pooled sessions are future work (see plan).

Responses may be JSON or SSE (text/event-stream). Both are parsed: SSE `data:`
lines are each a JSON-RPC message; a plain JSON body is the message directly.
"""
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse


class McpError(Exception):
    """Transport or protocol failure talking to graphiti-mcp (-> 502)."""


class McpToolError(Exception):
    """The tool call returned a JSON-RPC error result (-> 502 with detail)."""


def _base():
    return os.environ.get("GRAPHITI_MCP_URL", "http://graphiti-mcp:8000").rstrip("/")


def _timeout():
    return float(os.environ.get("MCP_TIMEOUT", "60"))


def _host_header():
    """graphiti-mcp's FastMCP host validation accepts `localhost`/`127.0.0.1`
    but rejects the container DNS name (HTTP 421 Misdirected Request). Caddy
    passed the client's `Host: localhost:8000` through; the gateway must do the
    same. urllib still connects to the real DNS name — only the header changes."""
    p = urlparse(_base())
    port = p.port or (443 if p.scheme == "https" else 80)
    return "localhost:%d" % port


def _parse_messages(body_bytes, content_type):
    """Extract JSON-RPC messages from a response body. Returns a list of dicts.
    SSE: parse `data: <json>` lines. JSON: parse the whole body as one message
    (or a list). Tolerant: bad lines are skipped."""
    ct = (content_type or "").lower()
    text = body_bytes.decode(errors="replace")
    msgs = []
    if "text/event-stream" in ct or "data:" in text:
        for ln in text.splitlines():
            ln = ln.strip()
            if ln.startswith("data:"):
                payload = ln[len("data:"):].strip()
                if not payload:
                    continue
                try:
                    msgs.append(json.loads(payload))
                except Exception:
                    continue
    if msgs:
        return msgs
    # Plain JSON (single message or a list).
    text = text.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        raise McpError("non-JSON MCP response: %s" % text[:200])
    if isinstance(parsed, list):
        return parsed
    return [parsed]


class McpSession:
    def __init__(self):
        self._sid = None
        self._id = 0

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _next_id(self):
        self._id += 1
        return self._id

    def _post(self, method, params=None, notification=False, msg_id=None):
        body = {"jsonrpc": "2.0", "method": method}
        if notification:
            body["method"] = method  # notifications carry no id
        else:
            body["id"] = msg_id if msg_id is not None else self._next_id()
        if params is not None:
            body["params"] = params
        data = json.dumps(body).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Host": _host_header(),
        }
        if self._sid:
            headers["Mcp-Session-Id"] = self._sid
        req = urllib.request.Request(_base() + "/mcp", data=data, headers=headers,
                                      method="POST")
        try:
            with urllib.request.urlopen(req, timeout=_timeout()) as r:
                ct = r.headers.get("Content-Type", "")
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self._sid = sid
                return r.status, r.read(), ct
        except urllib.error.HTTPError as e:
            raise McpError("MCP HTTP %s: %s" % (e.code, (e.read().decode() or "")[:200]))
        except urllib.error.URLError as e:
            raise McpError("MCP unreachable: %s" % e)

    def initialize(self):
        status, raw, ct = self._post(
            "initialize",
            {"protocolVersion": "2025-03-26", "capabilities": {},
             "clientInfo": {"name": "kb-gateway", "version": "1"}},
        )
        if status not in (200, 201):
            raise McpError("initialize -> HTTP %s" % status)
        if not self._sid:
            raise McpError("initialize response missing Mcp-Session-Id")
        # Notify initialized (no response expected; accept 200/202/204).
        status, _, _ = self._post("notifications/initialized", notification=True)
        if status not in (200, 202, 204):
            raise McpError("notifications/initialized -> HTTP %s" % status)

    def call(self, tool, arguments=None, msg_id=None):
        """Call an MCP tool. Returns the tool result content (parsed). Raises
        McpToolError on a JSON-RPC error, McpError on protocol/transport failure."""
        rid = msg_id if msg_id is not None else self._next_id()
        status, raw, ct = self._post("tools/call",
                                      {"name": tool, "arguments": arguments or {}},
                                      msg_id=rid)
        if status not in (200, 201):
            raise McpError("tools/call %s -> HTTP %s" % (tool, status))
        msgs = _parse_messages(raw, ct)
        # Find the JSON-RPC message matching our id with a result or error.
        for m in msgs:
            if not isinstance(m, dict):
                continue
            if m.get("id") != rid:
                continue
            if "error" in m:
                err = m["error"]
                raise McpToolError("tool %s error: %s" % (
                    tool, json.dumps(err)[:300]))
            if "result" in m:
                return m["result"]
        raise McpError("tools/call %s: no matching result in response" % tool)

    def close(self):
        """Best-effort session teardown. Graphiti has no standard delete-session
        in Streamable HTTP; the server expires idle sessions. Swallow errors."""
        if not self._sid:
            return
        try:
            req = urllib.request.Request(_base() + "/mcp", method="DELETE",
                                          headers={"Mcp-Session-Id": self._sid,
                                                   "Host": _host_header()})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
        self._sid = None


def call_tool(tool, arguments):
    """One-shot: handshake, call one tool, close. Returns the result content."""
    with McpSession() as s:
        return s.call(tool, arguments)