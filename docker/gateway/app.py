#!/usr/bin/env python3
"""api-gateway: stack-side authorization + Graphiti REST bridge + admin user
provisioning. Zero-dependency (Python 3 stdlib only).

Listens on :8010 (container-internal). Caddy (:3000, edge) is the public face.
All endpoints require `Authorization: Bearer <KB_API_KEY>` except /health.
Identity + role are derived from the key via Open WebUI (tamper-proof); the
caller cannot influence them. Writes are to the caller's own personal group;
destructive ops require owning the target group or admin; reads see all
groups (discovered from Neo4j).
"""
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import authorize
import graphiti
import kb_ignore
import neo4j
import owui

MAX_BODY = int(os.environ.get("KB_MAX_BODY", str(256 * 1024)))
MAX_CONCURRENCY = int(os.environ.get("KB_MAX_CONCURRENCY", "16"))

# /retrieve: gateway-mediated retrieval over an OWUI KB. `mode` lives only at
# the gateway boundary; OWUI's QueryCollectionsForm already has hybrid +
# hybrid_bm25_weight + k, so the gateway maps mode -> those and forwards a
# single collection_name. RETRIEVE_MODES: mode -> (hybrid, hybrid_bm25_weight);
# `hybrid` weight is None (omitted -> OWUI applies RAG_HYBRID_BM25_WEIGHT, the
# single source in .env), `lexical`/`lexical-dsl` is literal 1.0 (mode
# definition, not a tunable), `vector` is hybrid=False. `lexical-dsl` is an
# opt-in Tantivy DSL (phrase / +AND / +x -y); the gateway signals it to OWUI by
# prefixing the query with LEXICAL_DSL_PREFIX (a contract with the kb-openwebui
# image's pgvector.py + retrieval/utils.py, which strip it and run the
# id @@@ parse_with_field DSL predicate). RETRIEVE_ORDER: hybrid/lexical/
# lexical-dsl return an RRF score (higher=better, desc); vector returns a
# cosine distance (lower=better, asc) — the wrapper sorts by this so it does
# not reverse the hybrid ranking.
RETRIEVE_K_DEFAULT = int(os.environ.get("KB_RETRIEVE_K_DEFAULT", "5"))
RETRIEVE_K_MAX = int(os.environ.get("KB_RETRIEVE_K_MAX", "50"))
# Sentinel prefix the gateway prepends to the query for --mode lexical-dsl. The
# kb-openwebui image recognizes + strips it (pgvector.py branch + utils.py
# re-raise gate). A kb_check sentinel-agreement probe catches cross-image drift.
LEXICAL_DSL_PREFIX = "KB_LEXICAL_DSL_V1::"
RETRIEVE_MODES = {"hybrid": (True, None), "lexical": (True, 1.0),
                  "lexical-dsl": (True, 1.0), "vector": (False, None)}
RETRIEVE_ORDER = {"hybrid": "desc", "lexical": "desc",
                  "lexical-dsl": "desc", "vector": "asc"}
MAX_QUERY = int(os.environ.get("KB_MAX_QUERY", "4096"))

# Whether this Open WebUI image supports the admin user-provisioning flow
# (probed from /openapi.json at startup; mutable image tag).
PROVISIONING_OK = False
PROVISIONING_MISSING = []

_concurrency = threading.Semaphore(MAX_CONCURRENCY)


class _UtcISOFormatter(logging.Formatter):
    """ISO-8601 UTC timestamp on every log line, matching the rest of the stack
    (oikb JSON ts, loguru, neo4j). Standard logging -> level support + filtering."""
    converter = time.gmtime

    def formatTime(self, record, datefmt=None):
        return time.strftime("%Y-%m-%dT%H:%M:%S", self.converter(record.created)) + ".%03dZ" % (record.msecs)


def _configure_logging():
    """One stderr handler with the ISO-UTC formatter on the root logger
    (deterministic: replaces any pre-existing handler). Level from LOG_LEVEL
    (default INFO)."""
    h = logging.StreamHandler()  # stderr
    h.setFormatter(_UtcISOFormatter("%(asctime)s %(levelname)s %(name)s - %(message)s"))
    root = logging.getLogger()
    root.handlers = [h]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())


log = logging.getLogger("kb-gateway")


class GatewayError(Exception):
    """Carries an HTTP status + message out of a handler."""
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --- request handler ---------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "kb-gateway/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # access log: the edge Caddy has no access_log, so log the auth boundary here
        logging.getLogger("kb-gateway.access").info("%s - %s", self.address_string(), fmt % args)

    # -- response helpers --
    def _send(self, code, obj):
        # Keep the keep-alive connection aligned on EVERY path: if the handler
        # did not consume the request body (e.g. _auth() raised 401 before
        # _read_body()), drain it now so the unread bytes do not get parsed as
        # the next request's request line on a Caddy-reused upstream connection
        # (which surfaced as a 501 "Unsupported method '<body>POST'"). _read_body
        # and _drain_body set _body_consumed=True when they read; here we drain
        # only what they left, then reset for the next keep-alive request.
        if not getattr(self, "_body_consumed", False):
            self._drain_body()
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self._body_consumed = False

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
        self._body_consumed = True  # read from the socket; do not drain again
        try:
            data = json.loads(raw.decode())
        except Exception:
            raise GatewayError(400, "request body is not valid JSON")
        if not isinstance(data, dict):
            raise GatewayError(400, "request body must be a JSON object")
        return data

    def _drain_body(self):
        """Read and discard the request body. With HTTP/1.1 keep-alive an
        unconsumed body leaves bytes in the socket buffer; the next request on
        the same (Caddy-reused) upstream connection then parses those bytes as
        its request line -> 'Unsupported method <body>POST'. _send() calls this
        whenever a handler did not consume the body (e.g. a 401 from _auth()
        before _read_body()). No parsing, no size limit beyond MAX_BODY."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._body_consumed = True
            return
        remaining = min(length, MAX_BODY)
        self.rfile.read(remaining)
        self._body_consumed = True
        # Anything beyond MAX_BODY: leave it. A trigger body is always tiny (`{}`);
        # an oversized body would desync the connection, but that is not a
        # contract any caller sends, so we do not engineer for it here.

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
        # /health + /openapi.json are ungated and cheap; no concurrency gate.
        if path == "/health" and method == "GET":
            return self._health()
        if path == "/openapi.json" and method == "GET":
            return self._openapi()
        try:
            with _concurrency:
                self._route(method, path, qs)
        except GatewayError as e:
            self._err(e.status, e.message)
        except owui.OwuiError as e:
            self._err(503, "identity service unavailable: %s" % e)
        except owui.OwuiConflict as e:
            self._err(409, str(e))
        except graphiti.GraphitiError as e:
            self._err(502, "graphiti unavailable: %s" % e)
        except neo4j.Neo4jError as e:
            self._err(502, "neo4j unavailable: %s" % e)
        except BrokenPipeError:
            pass
        except Exception as e:  # never leak a stack trace to the client
            log.exception("unhandled error on %s %s", method, path)
            self._err(500, "internal error: %s" % e)

    def _route(self, method, path, qs):
        identity = None
        # Routes that need auth (everything except /health).
        if path == "/memory/whoami" and method == "GET":
            identity = self._auth()
            return self._ok({"email": identity["email"], "id": identity["id"],
                             "role": identity["role"]})
        if path == "/memory/groups" and method == "GET":
            self._auth()
            return self._ok({"groups": neo4j.discover_groups()})
        if path == "/memory/status" and method == "GET":
            self._auth()
            return self._ok({"status": graphiti.status()})
        if path == "/memory/episodes" and method == "GET":
            self._auth()
            max_eps = _qs_int(qs, "max", 10)
            return self._ok({"episodes": graphiti.get_episodes(
                neo4j.discover_groups(), max_eps)})
        if path == "/memory/add" and method == "POST":
            identity = self._auth()
            return self._add(identity, self._read_body())
        if path == "/memory/retrieve" and method == "POST":
            self._auth()
            body = self._read_body()
            query = _req(body, "query")
            return self._retrieve(query, body)
        if path == "/memory/forget" and method == "POST":
            identity = self._auth()
            return self._forget(identity, self._read_body())
        if path == "/memory/delete-edge" and method == "POST":
            identity = self._auth()
            return self._delete_uuid(identity, self._read_body(), "edge")
        if path == "/memory/delete-episode" and method == "POST":
            identity = self._auth()
            return self._delete_uuid(identity, self._read_body(), "episode")
        if path == "/memory/rag" and method == "POST":
            identity = self._auth()
            return self._rag(identity, self._read_body())
        if path == "/retrieve" and method == "POST":
            identity = self._auth()
            return self._retrieve_kb(identity, self._read_body())
        if path == "/admin/users" and method == "POST":
            identity = self._auth()
            return self._create_user(identity, self._read_body())
        if path == "/index" and method == "POST":
            identity = self._auth()
            return self._index(identity, qs)
        if path == "/status" and method == "GET":
            identity = self._auth()
            return self._status(identity, qs)
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
        source_description = body.get("source_description") or ""
        graphiti.add_memory(group, name, text, source_description)
        self._ok({"ok": True, "group": group})

    def _retrieve(self, query, body):
        max_facts = int(body.get("k") or 10)
        groups = neo4j.discover_groups()
        facts = graphiti.search_facts(groups, query, max_facts)
        self._ok({"facts": facts, "groups": groups})

    def _forget(self, identity, body):
        # Charset-normalize at the boundary: client input may be `user:<email>`
        # or the stored `user-<sanitized>`; both map to the one id used for the
        # ownership check, the clear_group call, and the response.
        group = authorize.graphiti_group_id((_req(body, "group") or "").strip())
        if not authorize.can_destruct(identity, group):
            raise GatewayError(403, "not permitted to clear group %r" % group)
        graphiti.clear_group(group)
        self._ok({"ok": True, "group": group})

    def _delete_uuid(self, identity, body, kind):
        uuid = _req(body, "uuid")
        if kind == "edge":
            target_group = neo4j.lookup_edge_group(uuid)
        else:
            target_group = neo4j.lookup_node_group(uuid)
        ok, err = authorize.check_uuid_target_group(identity, target_group)
        if not ok:
            raise GatewayError(403, err)
        if kind == "edge":
            graphiti.delete_edge(uuid)
        else:
            graphiti.delete_episode(uuid)
        self._ok({"ok": True, "uuid": uuid, "group": target_group})

    # -- RAG: gateway-inserted chat model, OWUI-native KB authz --

    def _rag(self, identity, body):
        """POST /memory/rag: RAG chat grounded on an OWUI KB. The caller sends
        {messages, files}; the gateway inserts the chat model from OPENWEBUI_MODEL
        and proxies POST /api/chat/completions with the CALLER's key, so OWUI
        enforces KB read access natively (no admin-key escalation, no gateway-side
        authz replication). ident = _auth(); authz = delegated to OWUI. Errors map
        OwuiError.code -> 4xx (esp. 403 = KB read denied) / 5xx -> 502; transport
        (no code) / unset model -> 503."""
        messages = _req(body, "messages")
        files = body.get("files") or []
        key = self.headers.get("Authorization", "")[len("Bearer "):].strip()
        try:
            result = owui.rag(key, messages, files)
        except owui.OwuiError as e:
            if e.code is None:
                raise GatewayError(503, "RAG upstream unavailable: %s" % e)
            if 400 <= e.code < 500:
                raise GatewayError(e.code, "RAG upstream: %s" % e)
            raise GatewayError(502, "RAG upstream: %s" % e)
        self._ok(result)

    # -- retrieve: gateway-mediated KB retrieval (caller key enforces KB read) --

    def _retrieve_kb(self, identity, body):
        """POST /retrieve: retrieve raw chunks from an OWUI KB. Body
        {kb_id, query, mode, k}. mode in {hybrid,lexical,vector} (default
        hybrid) maps to {hybrid, hybrid_bm25_weight} forwarded to OWUI
        /api/v1/retrieval/query/collection with the CALLER's key, so OWUI
        enforces KB read access natively (403 on deny — same posture as _rag;
        no gateway-side authz). Response {kb_id, mode, k, score_order, hits}
        where hits are the 8-key flattened chunks (the wrapper joins
        File.meta.data.gdrive per file_id client-side, as today). score_order
        tells the consumer how to sort: hybrid/lexical return an RRF score
        (desc); vector returns a cosine distance (asc). Errors map
        OwuiError.code -> 4xx (echo, esp. 403 = KB read denied) / 5xx -> 502 /
        None -> 503."""
        kb_id = _req(body, "kb_id")
        if not _is_uuid(kb_id):
            raise GatewayError(400, "kb_id must be a UUID")
        query = _req(body, "query")
        if not isinstance(query, str):
            raise GatewayError(400, "query must be a string")
        query = query.strip()
        if not query:
            raise GatewayError(400, "query must not be empty")
        if len(query) > MAX_QUERY:
            raise GatewayError(400, "query too long (max %d chars)" % MAX_QUERY)
        mode = body.get("mode") or "hybrid"
        if mode not in RETRIEVE_MODES:
            raise GatewayError(400, "mode must be one of %s, got %r"
                               % (", ".join(RETRIEVE_MODES), mode))
        k = body.get("k", RETRIEVE_K_DEFAULT)
        # Reject bool (bool is a subclass of int) + str + non-int + out of range.
        if isinstance(k, bool) or not isinstance(k, int):
            raise GatewayError(400, "k must be an integer, got %r" % k)
        if k < 1 or k > RETRIEVE_K_MAX:
            raise GatewayError(400, "k must be 1..%d, got %s" % (RETRIEVE_K_MAX, k))
        hybrid, hybrid_bm25_weight = RETRIEVE_MODES[mode]
        if mode == "lexical-dsl":
            query = LEXICAL_DSL_PREFIX + query
        key = self.headers.get("Authorization", "")[len("Bearer "):].strip()
        try:
            raw = owui.query_collection(key, kb_id, query, hybrid, hybrid_bm25_weight, k)
        except owui.OwuiError as e:
            if e.code is None:
                raise GatewayError(503, "retrieve upstream unavailable: %s" % e)
            if 400 <= e.code < 500:
                raise GatewayError(e.code, "retrieve upstream: %s" % e)
            raise GatewayError(502, "retrieve upstream: %s" % e)
        hits = _flatten_hits(raw)
        self._ok({"kb_id": kb_id, "mode": mode, "k": k,
                  "score_order": RETRIEVE_ORDER[mode], "hits": hits})

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
                log.error("rollback: key revoke failed for user %s: %s", user_id, e)
        try:
            owui.delete_user(admin_key, user_id)
        except Exception as e:
            log.error("rollback: user delete failed for user %s: %s", user_id, e)

    # -- KB index: stateless sync source -> KB + status --

    def _index(self, identity, qs):
        """POST /index?kb_id=<id>&dir=<name>[&force=1][&dry_run=1]
        [&reindex_all=1][&retry_pending=1]. Admin-only. `dir` is the KB's
        top-level subdir under the source root (KB_SOURCE_ROOT): the walk root is
        KB_SOURCE_ROOT/dir, so manifest keys stay subdir-relative (the shape OWUI
        sync/diff keys on) and each KB's source is isolated. `dir` is a single
        segment (the KB name): no `/`, no backslash, no wildcard, and `.`/`..` are
        rejected. Drives OWUI's sync/diff protocol with the gateway's held admin
        key, returns per-file results. Stateless: the KB is the state (no
        manifest file). dry_run never mutates (returns the plan only). New source
        subdirs are created via dirs/create before their files are uploaded
        (sync/diff's directory_map only covers existing paths; without this,
        new-subdir files would land at KB root). The gateway does NOT link files
        itself — OWUI's per-upload background task is the sole linker (extract ->
        embed -> link) — and re-triggers failed files (plus stalled pending with
        retry_pending=1) by deleting + re-uploading them. The reconcile is a FULL
        KB-wide reconcile of the walk manifest (sync/diff `deleted` + `rmdir` flow
        through), so files removed from the source ARE removed from the KB.
        `dir` is required (empty rejected) so a bare call cannot reconcile every
        KB into one. `.kb-ignore` (per-directory, up the ancestor chain from the
        source root) is applied as an additive deny-list after the walk (see
        apply_kb_ignores)."""
        if not authorize.is_admin(identity):
            raise GatewayError(403, "admin role required for /index")
        admin_key = owui._admin_key()  # OwuiError -> 503 if unset
        kb_id = _qs(qs, "kb_id", "")
        if not kb_id:
            raise GatewayError(400, "kb_id required (query kb_id)")
        force = _qs_bool(qs, "force", False)
        dry_run = _qs_bool(qs, "dry_run", False)
        reindex_all = _qs_bool(qs, "reindex_all", False)
        retry_pending = _qs_bool(qs, "retry_pending", False)
        kb_source_root = os.environ.get("KB_SOURCE_ROOT", "/kb-source")
        dir = _validate_dir(qs, kb_source_root)
        root = os.path.join(kb_source_root, dir)  # per-KB walk root; keys stay subdir-relative
        max_size = _parse_size(os.environ.get("KB_MAX_SIZE", "100mb"))
        allow = _parse_allow(os.environ.get("KB_ALLOW", ",".join(sorted(DEFAULT_ALLOW))))

        # walk_source fails closed on any read/stat/hash error (a silently
        # dropped file would reappear as `deleted` in sync/diff -> cleanup
        # removes its KB entry: data loss on a transient I/O error).
        files = walk_source(root, allow, max_size)  # [{filename,path,checksum,size,abspath}]
        files = apply_kb_ignores(files, dir, kb_source_root)  # additive .kb-ignore deny-list
        if not files and not force:
            raise GatewayError(422, "source walk yielded 0 files - refusing "
                                    "(mount failure?). Use force=1 to proceed.")
        manifest = [{"filename": f["filename"], "path": f["path"],
                     "checksum": f["checksum"], "size": f["size"]} for f in files]
        by_key = {(f["path"], f["filename"]): f for f in files}

        # dry_run: return the plan with ZERO mutation (no drain, no dir create,
        # no upload, no cleanup). For reindex_all the plan reports every source
        # file as added + the existing count it would drain; otherwise it runs
        # the read-only sync/diff and reports that diff.
        if dry_run:
            if reindex_all:
                lst = owui.list_kb_files(admin_key, kb_id)
                would_drain = len([it for it in (lst.get("items") or []) if it.get("id")])
                mkdir = sorted({f["path"] for f in files if f["path"]})
                return self._ok({"dry_run": True, "reindex_all": True,
                                 "added": len(files), "modified": 0, "deleted": 0,
                                 "unmodified": 0, "would_drain": would_drain,
                                 "mkdir": mkdir})
            diff = owui.sync_diff(admin_key, kb_id, manifest)
            return self._ok({"dry_run": True, "added": len(diff.get("added") or []),
                             "modified": len(diff.get("modified") or []),
                             "deleted": len(diff.get("deleted") or []),
                             "unmodified": diff.get("unmodified_count") or 0,
                             "mkdir": diff.get("mkdir") or []})

        # reindex_all (non-dry_run): drain the KB first so the whole source is
        # re-uploaded (everything becomes `added` in the diff).
        if reindex_all:
            lst = owui.list_kb_files(admin_key, kb_id)
            drain_file_ids = [it.get("id") for it in (lst.get("items") or []) if it.get("id")]
            drain_dir_ids = [d.get("id") for d in (lst.get("directories") or []) if d.get("id")]
            if drain_file_ids or drain_dir_ids:
                owui.sync_cleanup(admin_key, kb_id, drain_file_ids, drain_dir_ids)

        diff = owui.sync_diff(admin_key, kb_id, manifest)
        # directory_map: EXISTING path -> dir_id (from sync/diff). Extended below
        # with newly created dirs so every source path resolves to a dir_id.
        directory_map = dict(diff.get("directory_map") or {})
        added = diff.get("added") or []
        modified = diff.get("modified") or []
        deleted = diff.get("deleted") or []
        rmdir = diff.get("rmdir") or []
        unmodified = diff.get("unmodified_count") or 0
        log.info("/index kb=%s added=%d modified=%d deleted=%d unmodified=%d",
                 kb_id, len(added), len(modified), len(deleted), unmodified)

        # errors is initialized BEFORE the mkdir loop so a create_directory
        # failure inside that loop records an error (an append before
        # initialization would NameError -> 500 on a dir-create fail).
        errors = []
        # Create the new directories (mkdir) shallowest-first so each segment's
        # parent_id is known. sync/diff returns mkdir sorted by depth; create each
        # missing segment via dirs/create and extend directory_map to cover it.
        for mkdir_path in (diff.get("mkdir") or []):
            segments = mkdir_path.split("/")
            for depth in range(1, len(segments) + 1):
                p = "/".join(segments[:depth])
                if p in directory_map:
                    continue
                parent_p = "/".join(segments[:depth - 1])
                parent_id = directory_map.get(parent_p)  # None -> top-level
                try:
                    d = owui.create_directory(admin_key, kb_id, segments[depth - 1], parent_id)
                except (owui.OwuiError, OSError) as e:
                    # socket.timeout (OSError) is not always wrapped as OwuiError.
                    # A failed dir leaves deeper segments without a parent -> stop
                    # this path; its files fall back to dir_id="" (KB root).
                    log.warning("/index mkdir failed kb=%s path=%s: %s", kb_id, p, e)
                    errors.append({"path": p, "status": "error",
                                   "error": "create_directory failed: %s" % e})
                    break
                directory_map[p] = d.get("id") or ""

        orphan_file_ids = []  # modified: stale_file_ids to clean after the new link
        for entry in added + modified:
            fn = entry.get("filename")
            path = entry.get("path", "")
            info = by_key.get((path, fn))
            if not info:
                errors.append({"filename": fn, "status": "error",
                               "error": "not found in source walk"})
                continue
            dir_id = directory_map.get(path) or ""
            try:
                data_bytes = open(info["abspath"], "rb").read()
            except Exception as e:
                log.warning("/index source read failed kb=%s file=%s: %s", kb_id, fn, e)
                errors.append({"filename": fn, "status": "error",
                               "error": "source read failed: %s" % e})
                continue
            try:
                gmeta = _gdrive_meta_for(info["abspath"])
                fm = owui.upload_file(admin_key, kb_id, info["checksum"],
                                      dir_id, fn, data_bytes, info.get("mtime"),
                                      gdrive_meta=gmeta)
            except (owui.OwuiError, OSError) as e:
                # socket.timeout (OSError) is not always wrapped as OwuiError.
                # One slow/timed-out upload is a per-file error, not a run abort.
                log.warning("/index upload failed kb=%s file=%s: %s", kb_id, fn, e)
                errors.append({"filename": fn, "status": "error", "error": str(e)})
                continue
            # modified: OWUI carries the prior file id as `stale_file_id`
            # (added entries never carry it). It is an orphan -> clean below
            # (sync_cleanup is tolerant of an id the idempotency patch already
            # reclaimed during the new upload). The background task links the
            # new file after extract+embed (sole linker).
            if entry.get("stale_file_id"):
                orphan_file_ids.append(entry["stale_file_id"])

        # The gateway does NOT link files. POST /files/ with
        # metadata.knowledge_id queues OWUI's per-upload background task that
        # runs the full pipeline — extract (markitdown-ocr) -> embed into the KB
        # collection (process_file(collection_name=knowledge_id)) -> link. That
        # task is the sole linker, so the link is a valid completion proxy
        # (vectors are written before the link). add_file_to_knowledge_by_id
        # has no exists-check, so a second link insert (a double-link race)
        # would IntegrityError -> "Failed to link file ..." -> data.status=
        # 'failed' on a successfully-extracted file; the gateway avoids this by
        # not inserting a link itself.

        cleanup_file_ids = orphan_file_ids + [d.get("file_id") for d in deleted if d.get("file_id")]
        cleanup_dir_ids = list(rmdir)
        if cleanup_file_ids or cleanup_dir_ids:
            log.info("/index cleanup kb=%s files=%d dirs=%d",
                     kb_id, len(cleanup_file_ids), len(cleanup_dir_ids))
            try:
                owui.sync_cleanup(admin_key, kb_id, cleanup_file_ids, cleanup_dir_ids)
            except (owui.OwuiError, OSError) as e:
                errors.append({"filename": "<sync_cleanup>", "status": "error", "error": str(e)})

        # Re-trigger: self-heal failed files (and, with retry_pending=1, stalled
        # pending) by deleting + re-uploading so a fresh background task is
        # queued. The upload-idempotency patch returns an existing same-hash file
        # WITHOUT re-queueing the task, so the delete first is required to retry.
        # Default retries only `failed` (a failed file is not actively
        # processing); retry_pending=1 also retries `pending` (operator-initiated
        # for stalled pending after the drain — not the default, to avoid
        # interrupting in-flight OCR). Each retry target is mapped back to its
        # source by the content hash (meta.data.file_hash == the source checksum
        # set at upload), then re-uploaded into its original directory.
        retry_statuses = {"failed"}
        if retry_pending:
            retry_statuses.add("pending")
        retried = 0
        orphans_removed = 0
        by_hash = {f["checksum"]: f for f in files}
        try:
            file_status = owui.list_file_status(admin_key, kb_id)
        except (owui.OwuiError, OSError) as e:
            file_status = None
            log.warning("/index list_file_status failed kb=%s: %s", kb_id, e)
            errors.append({"filename": "<re-trigger>", "status": "error",
                           "error": "list_file_status failed: %s" % e})
        if file_status is not None:
            for st in file_status:
                status = st.get("status")
                fn = st.get("filename") or st.get("file_id") or "?"
                src = by_hash.get(st.get("file_hash"))
                if not src and status != "completed":
                    # No-source orphan: the source is gone (deleted from Drive,
                    # rclone-excluded, or moved). Delete it on a KB-wide reconcile
                    # (by_hash covers the whole KB; list_file_status is KB-wide)
                    # when the file carries a gateway file_hash (a non-gateway file
                    # with no file_hash is indistinguishable and must not be
                    # deleted). This fires for failed AND pending/processing,
                    # independent of retry_pending: a no-source pending file has
                    # nothing to re-extract (its source is gone), so deleting it
                    # does NOT interrupt legit in-flight OCR the way
                    # retry_pending=1 would (which re-triggers ALL pending,
                    # including those with a live source). sync_diff did not catch
                    # it: failed/pending files are not linked. A no-source
                    # completed file is sync_diff's job (it is linked).
                    if not st.get("file_hash"):
                        errors.append({"filename": fn, "status": "error",
                                       "error": "re-trigger: no source for hash "
                                                "(no file_hash; not deleted)"})
                        continue
                    fid = st.get("file_id")
                    if not fid:
                        errors.append({"filename": fn, "status": "error",
                                       "error": "re-trigger orphan: no file_id"})
                        continue
                    try:
                        owui.delete_file(admin_key, fid)
                        orphans_removed += 1
                        log.info("/index orphan-delete kb=%s file=%s file_id=%s status=%s",
                                 kb_id, fn, fid, status)
                    except (owui.OwuiError, OSError) as e:
                        log.warning("/index orphan-delete failed kb=%s file=%s: %s",
                                    kb_id, fn, e)
                        errors.append({"filename": fn, "status": "error",
                                       "error": "re-trigger orphan delete failed: %s" % e})
                    continue
                if status not in retry_statuses:
                    continue
                log.info("/index retry kb=%s file=%s file_id=%s status=%s",
                         kb_id, fn, st.get("file_id"), st.get("status"))
                try:
                    owui.delete_file(admin_key, st["file_id"])
                except (owui.OwuiError, OSError) as e:
                    log.warning("/index retry delete failed kb=%s file=%s: %s", kb_id, fn, e)
                    errors.append({"filename": fn, "status": "error",
                                   "error": "re-trigger delete failed: %s" % e})
                    continue
                dir_id = directory_map.get(src["path"]) or ""
                try:
                    data_bytes = open(src["abspath"], "rb").read()
                    gmeta = _gdrive_meta_for(src["abspath"])
                    owui.upload_file(admin_key, kb_id, src["checksum"],
                                     dir_id, src["filename"], data_bytes, src.get("mtime"),
                                     gdrive_meta=gmeta)
                except (owui.OwuiError, OSError) as e:
                    log.warning("/index retry re-upload failed kb=%s file=%s: %s", kb_id, fn, e)
                    errors.append({"filename": fn, "status": "error",
                                   "error": "re-trigger re-upload failed: %s" % e})
                    continue
                retried += 1
        if retried:
            log.info("/index kb=%s retried=%d (retry_pending=%s)",
                     kb_id, retried, retry_pending)
        if orphans_removed:
            log.info("/index kb=%s orphans_removed=%d", kb_id, orphans_removed)

        self._ok({"added": len(added), "modified": len(modified),
                  "deleted": len(deleted), "unmodified": unmodified,
                  "retried": retried, "orphans_removed": orphans_removed,
                  "errors": errors, "ok": len(errors) == 0})

    def _status(self, identity, qs):
        """GET /status?kb_id=<id>&dir=<name>[&file=<relpath>][&json=1].
        Read-only. `dir` is the KB's top-level subdir under the source root (the
        walk root is KB_SOURCE_ROOT/dir). Reports real per-file progress from
        OWUI file.data.status
        (via GET /files/?content=false, paged). OWUI's status vocabulary:
          pending    = extraction phase (the slow GPU/OCR work) or queued —
                       extraction does not update status until it finishes, so
                       a file mid-OCR reads pending (the GPU-busy signal);
          processing = the KB embedding + link phase (brief), set in
                       _process_handler right before the second process_file;
          completed  = extracted + embedded in the KB collection + linked;
          failed     = error at any stage (error string in data.error).
        Re-derived live; no stored last-run state. Uses the gateway's held
        admin key for the file scan (GET /files/ is user-scoped — a read-scoped
        caller key sees only its own files, but the KB files were uploaded by
        the admin); the caller's KB_API_KEY is authorization only."""
        kb_id = _qs(qs, "kb_id", "")
        if not kb_id:
            raise GatewayError(400, "kb_id required (query kb_id)")
        as_json = _qs_bool(qs, "json", False)
        relpath = _qs(qs, "file", "")
        admin_key = owui._admin_key()  # OwuiError -> 503 if unset
        kb_source_root = os.environ.get("KB_SOURCE_ROOT", "/kb-source")
        dir = _validate_dir(qs, kb_source_root)
        root = os.path.join(kb_source_root, dir)
        allow = _parse_allow(os.environ.get("KB_ALLOW", ",".join(sorted(DEFAULT_ALLOW))))
        max_size = _parse_size(os.environ.get("KB_MAX_SIZE", "100mb"))
        files = walk_source(root, allow, max_size)
        files = apply_kb_ignores(files, dir, kb_source_root)  # additive .kb-ignore deny-list
        source_count = len(files)
        file_status = owui.list_file_status(admin_key, kb_id)
        completed = sum(1 for s in file_status if s.get("status") == "completed")
        pending = sum(1 for s in file_status if s.get("status") == "pending")
        processing = sum(1 for s in file_status if s.get("status") == "processing")
        failed = [s for s in file_status if s.get("status") == "failed"]
        log.info("/status kb=%s completed=%d pending=%d processing=%d failed=%d source=%d",
                 kb_id, completed, pending, processing, len(failed), source_count)
        in_flight = pending + processing
        per_file = [{"filename": s.get("filename"), "status": s.get("status"),
                     "size": s.get("size"), "error": s.get("error")}
                    for s in file_status
                    if s.get("status") == "completed"]
        if relpath:
            per_file = [p for p in per_file
                        if p.get("filename") == os.path.basename(relpath)]
        # Drain runtime = now - earliest file upload (created_at) in this KB.
        # On a fresh /index run (all files uploaded together) min(created_at) is
        # the run start; a re-index (delete + re-upload) resets created_at, so
        # this tracks the current run. None when the KB has no files yet.
        starts = [s.get("created_at") for s in file_status if s.get("created_at")]
        started_at = min(starts) if starts else None
        runtime = (int(time.time()) - started_at) if started_at else None
        summary = {"indexed_files": per_file,
                   "pending_files": [{"filename": s.get("filename"),
                                      "size": s.get("size"),
                                      "error": s.get("error")}
                                     for s in file_status
                                     if s.get("status") == "pending"],
                   "failed_files": [{"filename": f.get("filename"),
                                     "size": f.get("size"),
                                     "error": f.get("error")} for f in failed],
                   "dir": dir, "kb_id": kb_id,
                   "source_count": source_count,
                   "indexed_count": completed,
                   "pending": pending,
                   "processing": processing,
                   "failed": len(failed),
                   "started_at": started_at,
                   "runtime": runtime}
        if as_json:
            return self._ok(summary)
        # human-readable: glyphs (✓/✗/○), no emoji, no ETA (no daemon). pending
        # = GPU/OCR in flight (the busy signal); processing = embed + link.
        lines = ["%-18s: %d allowlisted files" % ("dir (%s)" % dir, source_count),
                 "indexed (OWUI KB) : %d completed (searchable)" % completed,
                 "pending (OWUI)    : %d in extraction (OCR/GPU)" % pending,
                 "processing (OWUI) : %d embedding + linking" % processing,
                 "failed (OWUI)     : %d" % len(failed),
                 "runtime           : %s" % _fmt_dur(runtime)]
        for f in failed[:20]:
            lines.append("  ✗ %s (%s) — %s" % (f.get("filename") or "?",
                                               _human_size(f.get("size")),
                                               (f.get("error") or "")[:80]))
        if in_flight == 0 and not failed:
            lines.append("status            : ✓ sync COMPLETE (drained, no failures)")
        elif in_flight == 0:
            lines.append("status            : ○ drain complete with %d failure(s) "
                         "(re-run /index to re-trigger failed)" % len(failed))
        else:
            lines.append("status            : ○ in-flight=%d (pending=%d processing=%d)"
                         % (in_flight, pending, processing))
        body = "\n".join(lines)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def _openapi(self):
        self._send(200, OPENAPI_SPEC)


# --- utils ---

# gdrive index: documents-only allowlist. Source code is handled by
# open-codebase-index, not the OWUI KB.
DEFAULT_ALLOW = {"docx", "pdf", "pptx", "xlsx", "txt", "md", "html", "json", "log", "tex"}

def _validate_dir(qs, kb_source_root):
    """Read and validate the `dir` query value: the KB name = one top-level
    subdir under the source root. Return the validated, stripped `dir`. Reject
    empty; `.` and `..`; any `/`, backslash, or wildcard char (`*?[`); a
    non-existent subdir; and a realpath that escapes the source root (defence in
    depth, since the single-segment rule already forbids `/`). A dot-prefixed
    single segment
    (e.g. `.tests`) is valid. KB identity is the top dir name only — no
    within-KB subpath scoping."""
    dir = (_qs(qs, "dir", "") or "").strip()
    if not dir:
        raise GatewayError(400, "dir required (the KB subdir under the source root)")
    if dir in (".", ".."):
        raise GatewayError(400, "dir must not be %r (a KB name is a single top dir)" % dir)
    if any(c in dir for c in ("/", "\\", "*", "?", "[")):
        raise GatewayError(400, "dir must be a single top-dir name (no slash or wildcard): %r" % dir)
    full = os.path.join(kb_source_root, dir)
    if not os.path.isdir(full):
        raise GatewayError(400, "dir not found under the source root: %r" % dir)
    root_real = os.path.realpath(kb_source_root)
    full_real = os.path.realpath(full)
    if not (full_real == root_real or full_real.startswith(root_real + os.sep)):
        raise GatewayError(400, "dir escapes the source root: %r" % dir)
    return dir


def _gdrive_meta_for(abspath):
    """Read the `<abspath>.meta.json` sidecar (scripts/gdrive-meta.py output) and
    return its parsed dict for OWUI File.meta.data, or None when no sidecar exists.
    The sidecar carries the Drive description, labels, grounded flag, approval,
    and comments. Read-only + defensive: a missing or malformed sidecar is logged
    and treated as None (the source file still indexes; its sidecar never blocks
    the upload). Stdlib json only (the gateway is zero-dependency)."""
    sidecar = abspath + ".meta.json"
    try:
        if not os.path.isfile(sidecar):
            return None
        with open(sidecar, "r", encoding="utf-8") as fh:
            m = json.load(fh)
    except (OSError, ValueError) as e:
        log.warning("/index .meta.json read failed file=%s: %s", abspath, e)
        return None
    return m if isinstance(m, dict) else None


def _entry_for(abspath, root, allow, max_size):
    """Build one walk_source entry for `abspath`, or return None when the file is
    skipped (symlink, non-regular, wrong extension, over size). `path` is the
    directory relpath from `root` (POSIX, "" at root) — the key OWUI sync/diff
    uses. Raise GatewayError(500) on a stat or hash OSError (fail closed)."""
    fn = os.path.basename(abspath)
    # gdrive-meta sidecars + `.kb-ignore` sit next to source files but are never
    # indexed. `.meta` (YAML) is already dropped by the ext allowlist below (meta
    # ∉ allow); `.meta.json` (JSON) has ext `json`, which IS allowed, so skip it
    # by name here. `.kb-ignore` is a dot-name (dropped by walk_source's dot-name
    # skip in a normal walk).
    if fn.endswith(".meta") or fn.endswith(".meta.json") or fn == ".kb-ignore":
        return None
    ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
    if ext not in allow:
        return None
    try:
        if os.path.islink(abspath) or not os.path.isfile(abspath):
            return None
        size = os.path.getsize(abspath)
        mtime = os.path.getmtime(abspath)
    except OSError as e:
        raise GatewayError(500, "stat failed for %s: %s" % (abspath, e))
    if size > max_size:
        return None
    try:
        h = hashlib.sha256()
        with open(abspath, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        checksum = h.hexdigest()
    except OSError as e:
        raise GatewayError(500, "hash failed for %s: %s" % (abspath, e))
    rel = os.path.relpath(abspath, root)
    d_rel = os.path.dirname(rel).replace(os.sep, "/")
    return {"filename": fn, "path": d_rel, "checksum": checksum,
            "size": size, "abspath": abspath,
            "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))}


def walk_source(root, allow, max_size):
    """Walk `root` and return one entry per allowlisted, in-size file:
    [{filename, path, checksum, size, abspath, mtime}]. `filename` is the
    basename; `path` is the directory relpath from `root` (POSIX, "" at root) —
    the shape OWUI sync/diff expects, and the key a full walk produces.
    `checksum` is the raw-file sha256. `mtime` is the source file mtime
    (rclone-preserved gdrive modifiedTime) as ISO-8601 UTC. Skip symlinks and
    dot-names (hidden dirs and files). The walk root itself may be a dot-dir
    (e.g. `.tests`): os.walk prunes dot-names only among children, never the
    root. Return an empty list when the root is missing or empty (the caller
    guards).

    Fails CLOSED on any OSError (stat, read, hash, or os.walk descent): a
    silently dropped file would reappear as `deleted` in sync/diff, and
    sync/cleanup would then remove its KB entry — data loss on a transient
    I/O/permission error. Raises GatewayError(500) so /index aborts instead."""
    out = []
    if not os.path.isdir(root):
        return out  # missing root -> empty (caller guards)

    def _walk_err(err):
        raise GatewayError(500, "source walk failed: %s" % err)

    for dirpath, dirs, filenames in os.walk(root, onerror=_walk_err):
        # Prune dot-dirs: hidden/auxiliary trees (.sync-reports, .sync.lock)
        # are excluded from a full walk. The walk root is never pruned here
        # (os.walk yields it before this mutation; a dot-dir KB like `.tests`
        # indexes because _validate_dir already confirmed it exists).
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            abspath = os.path.join(dirpath, fn)
            entry = _entry_for(abspath, root, allow, max_size)
            if entry:
                out.append(entry)
    return out


# --- .kb-ignore per-directory ignore (shared matcher, gitignore semantics) --
#
# One `.kb-ignore` file per directory under <source-root>, gitignore-style:
# rules are relative to the file's location and accumulate up the ancestor chain
# (shallowest first; the LAST matching rule wins; `!` re-includes). `*` does not
# cross `/`; `**` does; `?` = one non-`/` char; a leading `/` anchors at the
# file's dir; a trailing `/` = a directory and its contents; a no-slash name
# matches a file or dir of that name at any depth. The matcher body lives in
# `scripts/kb_ignore.py` (the gdrive-sync two-pass calls its `filter` CLI; kb.py
# clones it inline for its monolithic deploy); the gateway imports
# `kb_ignore.allowed`. Missing `.kb-ignore` everywhere -> no denies (only the
# extension allowlist applies). The deny-list is ADDITIVE (it only removes
# files; a deeper `!` re-includes). The hardcoded `.meta`/`.meta.json`/
# `.kb-ignore` sidecar skip in `_entry_for` stays (a gateway invariant);
# `.kb-ignore` cannot un-deny itself.
#
# Documented limitation (post-filter model): a `!` in a DEEPER `.kb-ignore` CAN
# re-include a file under a directory excluded by a SHALLOWER `.kb-ignore` (the
# gitignore parent-dir rule is NOT enforced). The `*` + `!subtree/**` allowlist
# (the primary use case) works.


def apply_kb_ignores(files, dir, kb_source_root):
    """Drop walk entries ignored by the `.kb-ignore` ancestor chain under
    `kb_source_root`. `dir` is the KB subdir (root-relative); each entry's
    root-relative path is `dir/<entry.path>/<filename>`. Additive only (a `!`
    in a deeper `.kb-ignore` re-includes). No-op when no `.kb-ignore` exists
    (`kb_ignore.allowed` returns True for every path -> `files` unchanged)."""
    out = []
    for f in files:
        fr = "/".join(p for p in (dir, f.get("path", ""), f.get("filename", "")) if p)
        if kb_ignore.allowed(kb_source_root, fr):
            out.append(f)
    return out


def _parse_size(s):
    """'100mb' / '100mb' -> bytes. Suffixes: b, k/kb, m/mb, g/gb (case-insensitive,
    optional 'b'). Default 100 MiB on bad input."""
    s = (s or "").strip().lower()
    if not s:
        return 100 * 1024 * 1024
    mult = 1
    for suf, m in (("gb", 1 << 30), ("g", 1 << 30), ("mb", 1 << 20),
                   ("m", 1 << 20), ("kb", 1 << 10), ("k", 1 << 10), ("b", 1)):
        if s.endswith(suf):
            s = s[:-len(suf)]
            mult = m
            break
    try:
        return int(s) * mult
    except ValueError:
        return 100 * 1024 * 1024


def _human_size(n):
    """bytes -> '1.2 MB' (1 decimal for >= 1 KB; bare bytes below). None -> '-'."""
    if n is None:
        return "-"
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "-"
    for unit, factor in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= factor:
            return "%.1f %s" % (n / factor, unit)
    return "%d B" % n


def _fmt_dur(sec):
    """seconds -> '1h 02m 03s' (leading zeros on m/s, stripped leading 0h/0m).
    None -> '-'."""
    if sec is None:
        return "-"
    sec = int(sec)
    h, sec = divmod(sec, 3600)
    m, sec = divmod(sec, 60)
    parts = []
    if h:
        parts.append("%dh" % h)
    if h or m:
        parts.append("%02dm" % m)
    parts.append("%02ds" % sec)
    return " ".join(parts)


def _parse_allow(s):
    """Comma-separated extensions -> set (lowercased, no leading dot)."""
    return {x.strip().lstrip(".").lower() for x in (s or "").split(",") if x.strip()}


def _req(body, key):
    val = body.get(key)
    if val is None or (isinstance(val, str) and not val.strip()):
        raise GatewayError(400, "missing required field: %s" % key)
    return val


def _is_uuid(s):
    """True only if `s` is a valid UUID string. Rejects a non-UUID kb_id before
    it becomes a collection_name forwarded to OWUI."""
    if not isinstance(s, str):
        return False
    try:
        uuid.UUID(s)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _flatten_hits(d):
    """OWUI /query/collection response: {documents:[[...]], distances:[[...]],
    metadatas:[[...]]} — one inner list per collection_name (the gateway sends a
    single collection, so one inner list). OWUI returns no `ids`; a chunk is
    identified by metadata file_id + start_index. Flatten to 8-key hit dicts:
    {distance, file, file_id, page, start_index, source, mtime, text}. Absent
    metadata -> None / "" (the wrapper joins gdrive per file_id client-side, as
    today; the gateway does not add gdrive here). The 8-key shape is the stable
    contract existing consumers depend on."""
    docs = d.get("documents", [[]])
    dists = d.get("distances", [[]])
    metas = d.get("metadatas", [[]])
    out = []
    for sub_docs, sub_d, sub_m in zip(docs, dists, metas):
        for j, t in enumerate(sub_docs or []):
            m = (sub_m[j] if j < len(sub_m or []) else {}) or {}
            out.append({
                "distance": sub_d[j] if j < len(sub_d or []) else None,
                "file": m.get("file_name") or m.get("name") or "",
                "file_id": m.get("file_id") or "",
                "page": m.get("page"),
                "start_index": m.get("start_index"),
                "source": m.get("source") or "",
                "mtime": m.get("mtime"),
                "text": t,
            })
    return out


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


def _qs(qs, key, default=""):
    """First query value for `key`, or `default`. URL-decoded."""
    if not qs:
        return default
    for pair in qs.split("&"):
        k, _, v = pair.partition("=")
        if k == key:
            return urllib.parse.unquote_plus(v) if v else default
    return default


def _qs_bool(qs, key, default=False):
    """Truthy: 1, true, yes, on (case-insensitive)."""
    v = _qs(qs, key, None)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# --- OpenAPI 3.1 spec (hand-written; served ungated at GET /openapi.json) ---
# Documents the gateway surface so callers (gdrive-sync, the kb skill, tests)
# can discover it. Stdlib only — a static dict. Validation lives in the handlers
# (_req, _qs, _qs_bool, _qs_int); this spec is descriptive.
OPENAPI_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "kb-gateway", "version": "1.0",
             "description": "Stack-side authorization + Graphiti bridge + admin "
                            "user provisioning + stateless KB index sync."},
    "paths": {
        "/health": {"get": {"summary": "Process + OWUI reachability", "security": []}},
        "/openapi.json": {"get": {"summary": "This document", "security": []}},
        "/index": {"post": {
            "summary": "Index (reconcile) a source into an OWUI KB (admin only)",
            "security": [{"bearerAuth": []}],
            "parameters": [
                {"name": "kb_id", "in": "query", "required": True, "schema": {"type": "string", "format": "uuid"}},
                {"name": "dir", "in": "query", "required": True, "schema": {"type": "string"},
                 "description": "KB top-level subdir under the source root (KB_SOURCE_ROOT); single segment, no slash or wildcard"},
                {"name": "force", "in": "query", "schema": {"type": "boolean", "default": False}},
                {"name": "dry_run", "in": "query", "schema": {"type": "boolean", "default": False}},
                {"name": "reindex_all", "in": "query", "schema": {"type": "boolean", "default": False}},
                {"name": "retry_pending", "in": "query", "schema": {"type": "boolean", "default": False}}],
            "responses": {"200": {"description": "per-file index result"},
                          "403": {"description": "admin role required"},
                          "422": {"description": "empty source (use force=1)"}}}},
        "/status": {"get": {
            "summary": "KB index status (read; read-scoped key works)",
            "security": [{"bearerAuth": []}],
            "parameters": [
                {"name": "kb_id", "in": "query", "required": True, "schema": {"type": "string", "format": "uuid"}},
                {"name": "dir", "in": "query", "required": True, "schema": {"type": "string"},
                 "description": "KB top-level subdir under the source root (KB_SOURCE_ROOT); single segment, no slash or wildcard"},
                {"name": "file", "in": "query", "required": False, "schema": {"type": "string"}},
                {"name": "json", "in": "query", "schema": {"type": "boolean", "default": False}}],
            "responses": {"200": {"description": "status (text or json)"}}}},
        "/admin/users": {"post": {"summary": "Admin user provisioning (admin only)", "security": [{"bearerAuth": []}]}},
        "/memory/whoami": {"get": {"summary": "Caller identity", "security": [{"bearerAuth": []}]}},
        "/memory/groups": {"get": {"summary": "List memory groups", "security": [{"bearerAuth": []}]}},
        "/memory/status": {"get": {"summary": "Graphiti status", "security": [{"bearerAuth": []}]}},
        "/memory/episodes": {"get": {"summary": "List episodes", "security": [{"bearerAuth": []}]}},
        "/memory/add": {"post": {"summary": "Add memory", "security": [{"bearerAuth": []}]}},
        "/memory/retrieve": {"post": {"summary": "Retrieve facts", "security": [{"bearerAuth": []}]}},
        "/memory/forget": {"post": {"summary": "Clear a Group", "security": [{"bearerAuth": []}]}},
        "/memory/delete-edge": {"post": {"summary": "Delete an edge", "security": [{"bearerAuth": []}]}},
        "/memory/delete-episode": {"post": {"summary": "Delete an episode", "security": [{"bearerAuth": []}]}},
        "/memory/rag": {"post": {"summary": "RAG chat grounded on an OWUI KB (gateway inserts the chat model from OPENWEBUI_MODEL; caller's key enforces KB read access)", "security": [{"bearerAuth": []}]}},
        "/retrieve": {"post": {
            "summary": "Retrieve raw chunks from an OWUI KB (gateway maps mode -> hybrid/hybrid_bm25_weight; caller's key enforces KB read access)",
            "security": [{"bearerAuth": []}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object",
                "required": ["kb_id", "query"],
                "properties": {
                    "kb_id": {"type": "string", "format": "uuid"},
                    "query": {"type": "string", "maxLength": 4096},
                    "mode": {"type": "string", "enum": ["hybrid", "lexical", "lexical-dsl", "vector"], "default": "hybrid"},
                    "k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5}
                }
            }}}},
            "responses": {
                "200": {"description": "{kb_id, mode, k, score_order, hits[]}"},
                "400": {"description": "bad kb_id / query / mode / k"},
                "403": {"description": "KB read denied (OWUI native authz)"},
                "502": {"description": "OWUI upstream 5xx"},
                "503": {"description": "OWUI upstream unreachable"}
            }
        }}},
    "components": {"securitySchemes": {"bearerAuth": {
        "type": "http", "scheme": "bearer", "description": "KB_API_KEY (OWUI API key)"}}},
}


def main():
    global PROVISIONING_OK, PROVISIONING_MISSING
    _configure_logging()
    # Probe the deployed OWUI image's provisioning endpoints.
    try:
        PROVISIONING_OK, PROVISIONING_MISSING, perr = owui.provisioning_capabilities()
    except Exception as e:  # never crash startup over the probe
        PROVISIONING_OK, PROVISIONING_MISSING, perr = False, [], str(e)
    prov = "ok" if PROVISIONING_OK else ("missing %s" % (PROVISIONING_MISSING or [perr]))
    port = int(os.environ.get("KB_GATEWAY_PORT", "8010"))
    log.info("kb-gateway on :%s (provisioning=%s)", port, prov)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()