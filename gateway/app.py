#!/usr/bin/env python3
"""kb-gateway: stack-side authorization + Graphiti MCP bridge + admin user
provisioning. Zero-dependency (Python 3 stdlib only).

Listens on :8010 (container-internal). Caddy (:8000, edge) is the public face.
All endpoints require `Authorization: Bearer <KB_API_KEY>` except /health.
Identity + role are derived from the key via Open WebUI (tamper-proof); the
caller cannot influence them. Writes are to the caller's own personal group;
destructive ops require owning the target group or admin; reads see all
groups (discovered from Neo4j).
"""
import json
import os
import secrets
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import authorize
import mcp
import neo4j
import owui

MAX_BODY = int(os.environ.get("KB_MAX_BODY", str(256 * 1024)))
MAX_CONCURRENCY = int(os.environ.get("KB_MAX_CONCURRENCY", "16"))

# Whether this Open WebUI image supports the admin user-provisioning flow
# (probed from /openapi.json at startup; mutable image tag).
PROVISIONING_OK = False
PROVISIONING_MISSING = []

_concurrency = threading.Semaphore(MAX_CONCURRENCY)


class GatewayError(Exception):
    """Carries an HTTP status + message out of a handler."""
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --- helpers -----------------------------------------------------------------

def tool_text(result):
    """Extract a MCP tools/call result into a Python value. Graphiti returns
    facts/episodes as JSON inside a text content block. A tool-level error
    (isError=true) is raised as a 502."""
    if isinstance(result, dict):
        if result.get("isError"):
            content = result.get("content") or []
            txt = "".join(c.get("text", "") for c in content
                          if isinstance(c, dict) and c.get("type") == "text")
            raise GatewayError(502, "tool error: %s" % txt[:300])
        content = result.get("content")
        if isinstance(content, list):
            txt = "".join(c.get("text", "") for c in content
                          if isinstance(c, dict) and c.get("type") == "text")
            if not txt:
                return None
            try:
                return json.loads(txt)
            except Exception:
                return txt
    return result


# --- request handler ---------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "kb-gateway/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # quiet; the stack logs elsewhere

    # -- response helpers --
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, obj):
        self._send(200, obj)

    def _err(self, code, message):
        self._send(code, {"error": message})

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise GatewayError(413, "request body too large")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode())
        except Exception:
            raise GatewayError(400, "request body is not valid JSON")
        if not isinstance(data, dict):
            raise GatewayError(400, "request body must be a JSON object")
        return data

    def _auth(self):
        """Resolve the caller's identity from the Bearer key. Raises
        GatewayError(401) for a bad/missing key, GatewayError(503) if OWUI is
        unreachable. The caller cannot spoof identity — it comes from OWUI."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise GatewayError(401, "Authorization: Bearer <KB_API_KEY> required")
        key = auth[len("Bearer "):].strip()
        if not key:
            raise GatewayError(401, "missing API key")
        try:
            identity = owui.whoami(key)
        except owui.OwuiError as e:
            raise GatewayError(503, "identity service unavailable: %s" % e)
        if identity is None:
            raise GatewayError(401, "invalid or unauthorized API key")
        return identity

    # -- dispatch --
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        # Caddy may proxy DELETE for future use; not exposed in MVP.
        self._send(405, {"error": "method not allowed"})

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0]
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        # /health is ungated and cheap; no concurrency gate.
        if path == "/health" and method == "GET":
            return self._health()
        try:
            with _concurrency:
                self._route(method, path, qs)
        except GatewayError as e:
            self._err(e.status, e.message)
        except owui.OwuiError as e:
            self._err(503, "identity service unavailable: %s" % e)
        except owui.OwuiConflict as e:
            self._err(409, str(e))
        except mcp.McpToolError as e:
            self._err(502, str(e))
        except mcp.McpError as e:
            self._err(502, "graphiti unavailable: %s" % e)
        except neo4j.Neo4jError as e:
            self._err(502, "neo4j unavailable: %s" % e)
        except BrokenPipeError:
            pass
        except Exception as e:  # never leak a stack trace to the client
            self._err(500, "internal error: %s" % e)

    def _route(self, method, path, qs):
        identity = None
        # Routes that need auth (everything except /health).
        if path == "/mem/whoami" and method == "GET":
            identity = self._auth()
            return self._ok({"email": identity["email"], "id": identity["id"],
                             "role": identity["role"]})
        if path == "/mem/groups" and method == "GET":
            self._auth()
            return self._ok({"groups": neo4j.discover_groups()})
        if path == "/mem/status" and method == "GET":
            self._auth()
            return self._ok({"status": tool_text(mcp.call_tool("get_status", {}))})
        if path == "/mem/episodes" and method == "GET":
            self._auth()
            max_eps = _qs_int(qs, "max", 10)
            return self._ok({"episodes": tool_text(
                mcp.call_tool("get_episodes", {"group_ids": neo4j.discover_groups(),
                                                 "max_episodes": max_eps}))})
        if path == "/mem/add" and method == "POST":
            identity = self._auth()
            return self._add(identity, self._read_body())
        if path == "/mem/search" and method == "POST":
            self._auth()
            body = self._read_body()
            query = _req(body, "query")
            return self._search(query, body)
        if path == "/mem/forget" and method == "POST":
            identity = self._auth()
            return self._forget(identity, self._read_body())
        if path == "/mem/delete-edge" and method == "POST":
            identity = self._auth()
            return self._delete_uuid(identity, self._read_body(), "edge")
        if path == "/mem/delete-episode" and method == "POST":
            identity = self._auth()
            return self._delete_uuid(identity, self._read_body(), "episode")
        if path == "/admin/users" and method == "POST":
            identity = self._auth()
            return self._create_user(identity, self._read_body())
        raise GatewayError(404, "not found: %s %s" % (method, path))

    # -- memory operations --
    def _health(self):
        # Process-up + identity dependency (OWUI) reachable. Caddy proxies
        # /health here, so `make health` catches an auth-broken stack (codex #8)
        # rather than reporting healthy while identity resolution is down.
        owui_ok = _probe(os.environ.get("OWUI_URL", "http://openwebui:8080").rstrip("/") + "/health")
        if not owui_ok:
            return self._send(503, {"status": "degraded", "owui": "down"})
        self._ok({"status": "ok", "owui": "up"})

    def _add(self, identity, body):
        text = _req(body, "text")
        name = (body.get("name") or "").strip() or "kb-memory"
        group, err = authorize.resolve_add_group(identity, body.get("group"))
        if err:
            raise GatewayError(403, err)
        args = {"name": name, "episode_body": text, "group_id": group,
                "source": "text", "source_description": body.get("source_description") or ""}
        mcp.call_tool("add_memory", args)
        self._ok({"ok": True, "group": group})

    def _search(self, query, body):
        max_facts = int(body.get("k") or 10)
        groups = neo4j.discover_groups()
        result = tool_text(mcp.call_tool("search_memory_facts",
                                          {"query": query, "group_ids": groups,
                                           "max_facts": max_facts}))
        self._ok({"facts": result, "groups": groups})

    def _forget(self, identity, body):
        group = (_req(body, "group") or "").strip()
        if not authorize.can_destruct(identity, group):
            raise GatewayError(403, "not permitted to clear group %r" % group)
        mcp.call_tool("clear_graph", {"group_ids": [group]})
        self._ok({"ok": True, "group": group})

    def _delete_uuid(self, identity, body, kind):
        uuid = _req(body, "uuid")
        if kind == "edge":
            target_group = neo4j.lookup_edge_group(uuid)
            tool, args = "delete_entity_edge", {"uuid": uuid}
        else:
            target_group = neo4j.lookup_node_group(uuid)
            tool, args = "delete_episode", {"uuid": uuid}
        ok, err = authorize.check_uuid_target_group(identity, target_group)
        if not ok:
            raise GatewayError(403, err)
        mcp.call_tool(tool, args)
        self._ok({"ok": True, "uuid": uuid, "group": target_group})

    # -- admin: agent-driven user provisioning --
    def _create_user(self, identity, body):
        if not authorize.is_admin(identity):
            raise GatewayError(403, "admin role required")
        if not PROVISIONING_OK:
            raise GatewayError(501, "user provisioning unsupported by this Open "
                                    "WebUI image; missing: %s" % ", ".join(PROVISIONING_MISSING))
        email = (_req(body, "email") or "").strip().lower()
        name = (_req(body, "name") or "").strip()
        role = (body.get("role") or "user").strip() or "user"
        if role not in ("admin", "user"):
            raise GatewayError(400, "role must be 'admin' or 'user', got %r" % role)
        if "@" not in email:
            raise GatewayError(400, "invalid email")
        if not name:
            raise GatewayError(400, "name required")
        admin_key = self.headers.get("Authorization", "")[len("Bearer "):].strip()
        password = secrets.token_urlsafe(24)

        # 1. create the user (admin key authorizes). Raises OwuiConflict(409)
        #    on duplicate email — no partial state to clean up. A transport
        #    failure (OwuiError) here propagates as 503 with no user created.
        created = owui.add_user(admin_key, name, email, password, role)
        user_id = created.get("id")
        # Test-only hook (codex #3 rollback coverage): when set, force a failure
        # right after a successful create so the rollback path (delete the
        # partial user) is exercised. NEVER set in production; not in compose.yml.
        if os.environ.get("KB_TEST_PROVISION_FAIL_AFTER_CREATE", "").lower() in ("1", "true", "yes"):
            self._rollback_user(admin_key, user_id)
            raise GatewayError(502, "forced test failure after user creation "
                                    "(rollback exercised)")
        # 2-4. sign in as the new user, generate that user's API key, verify it.
        #    ANY failure after a successful create (transport error on signin,
        #    bad creds, key generation failure, unexpected identity) rolls back
        #    the partial user so we never leave a half-provisioned account. The
        #    broad `except Exception` is intentional: the rollback guarantee
        #    outranks a precise status code here; the message includes the cause.
        jwt = None
        try:
            jwt = owui.signin(email, password)
            if not jwt:
                raise owui.OwuiError("signin returned no token")
            api_key = owui.gen_api_key(jwt)
            who = owui.whoami(api_key)
            if not who or who.get("email", "").lower() != email or who.get("role") != role:
                raise owui.OwuiError("key resolved to an unexpected identity")
        except Exception as e:
            self._rollback_user(admin_key, user_id, jwt)
            raise GatewayError(502, "created user but provisioning failed: %s "
                                    "(rolled back)" % e)
        # 5. return everything to the admin; never persist (stateless).
        self._ok({"email": email, "temp_password": password,
                  "kb_api_key": api_key, "role": who.get("role"), "id": user_id})

    def _rollback_user(self, admin_key, user_id, jwt=None):
        """Best-effort cleanup of a partially-provisioned user. Never raises.
        Revokes the new user's API key (if a JWT was obtained) then deletes the
        user (OWUI cascades the user's keys on delete). Failures are logged to
        stdout so a leaked partial user is visible, not silently ignored."""
        if not user_id:
            return
        if jwt:
            try:
                owui.revoke_api_key(jwt)
            except Exception as e:
                print("rollback: key revoke failed for user %s: %s" % (user_id, e), flush=True)
        try:
            owui.delete_user(admin_key, user_id)
        except Exception as e:
            print("rollback: user delete failed for user %s: %s" % (user_id, e), flush=True)


# --- utils ---

def _req(body, key):
    val = body.get(key)
    if val is None or (isinstance(val, str) and not val.strip()):
        raise GatewayError(400, "missing required field: %s" % key)
    return val


def _probe(url, timeout=3):
    """True only if a GET returns 2xx (the host is reachable AND healthy). Used
    by /health to confirm the identity dependency is up; a 4xx/5xx means
    reachable but unhealthy, so /health reports degraded rather than masking
    a broken Open WebUI as "up"."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        return 200 <= e.code < 300
    except Exception:
        return False


def _qs_int(qs, key, default):
    if not qs:
        return default
    for pair in qs.split("&"):
        k, _, v = pair.partition("=")
        if k == key:
            try:
                return int(v)
            except Exception:
                return default
    return default


def main():
    global PROVISIONING_OK, PROVISIONING_MISSING
    # Probe the deployed OWUI image's provisioning endpoints.
    try:
        PROVISIONING_OK, PROVISIONING_MISSING, perr = owui.provisioning_capabilities()
    except Exception as e:  # never crash startup over the probe
        PROVISIONING_OK, PROVISIONING_MISSING, perr = False, [], str(e)
    prov = "ok" if PROVISIONING_OK else ("missing %s" % (PROVISIONING_MISSING or [perr]))
    port = int(os.environ.get("KB_GATEWAY_PORT", "8010"))
    print("kb-gateway on :%s (provisioning=%s)" % (port, prov), flush=True)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()