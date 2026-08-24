#!/usr/bin/env python3
"""kb-gateway: stack-side authorization + Graphiti REST bridge + admin user
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
import secrets
import threading
import time
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import authorize
import graphiti
import neo4j
import owui

MAX_BODY = int(os.environ.get("KB_MAX_BODY", str(256 * 1024)))
MAX_CONCURRENCY = int(os.environ.get("KB_MAX_CONCURRENCY", "16"))

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

    def _drain_body(self):
        """Read and discard the request body. /index takes its arguments as
        query params and sends an unused `{}` body, but with HTTP/1.1 keep-alive
        an unconsumed body leaves bytes in the socket buffer; the next request
        on the same (Caddy-reused) upstream connection then parses those bytes
        as its request line -> 'Unsupported method'. Draining keeps the
        connection aligned. No parsing, no size limit beyond MAX_BODY."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return
        remaining = min(length, MAX_BODY)
        self.rfile.read(remaining)
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
        if path == "/memory/search" and method == "POST":
            self._auth()
            body = self._read_body()
            query = _req(body, "query")
            return self._search(query, body)
        if path == "/memory/forget" and method == "POST":
            identity = self._auth()
            return self._forget(identity, self._read_body())
        if path == "/memory/delete-edge" and method == "POST":
            identity = self._auth()
            return self._delete_uuid(identity, self._read_body(), "edge")
        if path == "/memory/delete-episode" and method == "POST":
            identity = self._auth()
            return self._delete_uuid(identity, self._read_body(), "episode")
        if path == "/admin/users" and method == "POST":
            identity = self._auth()
            return self._create_user(identity, self._read_body())
        if path == "/index" and method == "POST":
            self._drain_body()  # before auth: keep the keep-alive connection aligned on every path
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

    def _search(self, query, body):
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

    # -- gdrive index: stateless sync drive + status --

    def _index(self, identity, qs):
        """POST /index?source=gdrive&kb_id=<id>[&force=1][&dry_run=1][&reindex_all=1].
        Admin-only. Walks the source mount, drives OWUI's sync/diff protocol
        with the gateway's held admin key, returns per-file results. Stateless:
        the KB is the state (no manifest file). dry_run never mutates (returns
        the plan only). New source subdirs are created via dirs/create before
        their files are uploaded (sync/diff's directory_map only covers existing
        paths; without this, new-subdir files would land at KB root)."""
        if not authorize.is_admin(identity):
            raise GatewayError(403, "admin role required for /index")
        admin_key = owui._admin_key()  # OwuiError -> 503 if unset
        source = _qs(qs, "source", "gdrive")
        if source != "gdrive":
            raise GatewayError(400, "unknown source %r (Phase 1: 'gdrive' only)" % source)
        kb_id = _qs(qs, "kb_id", os.environ.get("GDRIVE_KB_ID", ""))
        if not kb_id:
            raise GatewayError(400, "kb_id required (query kb_id or GDRIVE_KB_ID env)")
        force = _qs_bool(qs, "force", False)
        dry_run = _qs_bool(qs, "dry_run", False)
        reindex_all = _qs_bool(qs, "reindex_all", False)
        root = os.environ.get("GDRIVE_ROOT", "/gdrive")
        max_size = _parse_size(os.environ.get("KB_MAX_SIZE", "100mb"))
        allow = _parse_allow(os.environ.get("KB_ALLOW", ",".join(sorted(DEFAULT_ALLOW))))

        # walk_source fails closed on any read/stat/hash error (a silently
        # dropped file would reappear as `deleted` in sync/diff -> cleanup
        # removes its KB entry: data loss on a transient I/O error).
        files = walk_source(root, allow, max_size)  # [{filename,path,checksum,size,abspath}]
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
                    errors.append({"path": p, "status": "error",
                                   "error": "create_directory failed: %s" % e})
                    break
                directory_map[p] = d.get("id") or ""

        errors = []
        add_items = []
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
                errors.append({"filename": fn, "status": "error",
                               "error": "source read failed: %s" % e})
                continue
            try:
                fm = owui.upload_file(admin_key, kb_id, info["checksum"],
                                      dir_id, fn, data_bytes)
            except (owui.OwuiError, OSError) as e:
                # socket.timeout (OSError) is not always wrapped as OwuiError.
                # One slow/timed-out upload is a per-file error, not a run abort.
                errors.append({"filename": fn, "status": "error", "error": str(e)})
                continue
            add_items.append({"file_id": fm["id"], "directory_id": dir_id})
            # modified: OWUI carries the OLD file id as `stale_file_id` (added
            # entries never carry it). It is now an orphan -> clean below
            # (sync_cleanup is tolerant of an id the idempotency patch already
            # reclaimed during the new upload).
            if entry.get("stale_file_id"):
                orphan_file_ids.append(entry["stale_file_id"])

        if add_items:
            try:
                owui.batch_add(admin_key, kb_id, add_items)
            except (owui.OwuiError, OSError) as e:
                errors.append({"filename": "<batch_add>", "status": "error", "error": str(e)})
        # No batch_process call here. OWUI's upload handler (POST /files/ with
        # metadata.knowledge_id) queues a per-file background task that runs the
        # full pipeline: extract (markitdown-ocr) -> embed into the KB collection
        # (process_file(collection_name=knowledge_id)) -> link. batch_process
        # (retrieval/process/files/batch) reads file.data.content directly and
        # does NOT extract, so calling it immediately after upload would run
        # BEFORE the background task populates content -> every file reports
        # "content is empty" and no vectors are written by this call. The
        # background task is the embedder; /status polls file.data.status until
        # the drain completes (pending -> 0).

        cleanup_file_ids = orphan_file_ids + [d.get("file_id") for d in deleted if d.get("file_id")]
        cleanup_dir_ids = list(rmdir)
        if cleanup_file_ids or cleanup_dir_ids:
            try:
                owui.sync_cleanup(admin_key, kb_id, cleanup_file_ids, cleanup_dir_ids)
            except (owui.OwuiError, OSError) as e:
                errors.append({"filename": "<sync_cleanup>", "status": "error", "error": str(e)})

        self._ok({"added": len(added), "modified": len(modified),
                  "deleted": len(deleted), "unmodified": unmodified,
                  "errors": errors, "ok": len(errors) == 0})

    def _status(self, identity, qs):
        """GET /status?source=gdrive&kb_id=<id>[&file=<relpath>][&json=1].
        Read-only. Returns source count, OWUI file_count, pending, and per-file
        state (re-derived live; no stored last-run state). Caller's KB_API_KEY
        must read the KB (read-scoped key works)."""
        source = _qs(qs, "source", "gdrive")
        if source != "gdrive":
            raise GatewayError(400, "unknown source %r (Phase 1: 'gdrive' only)" % source)
        kb_id = _qs(qs, "kb_id", os.environ.get("GDRIVE_KB_ID", ""))
        if not kb_id:
            raise GatewayError(400, "kb_id required (query kb_id or GDRIVE_KB_ID env)")
        as_json = _qs_bool(qs, "json", False)
        relpath = _qs(qs, "file", "")
        # status reads use the caller's own key (read-scoped works).
        read_key = self.headers.get("Authorization", "")[len("Bearer "):].strip()
        root = os.environ.get("GDRIVE_ROOT", "/gdrive")
        allow = _parse_allow(os.environ.get("KB_ALLOW", ",".join(sorted(DEFAULT_ALLOW))))
        max_size = _parse_size(os.environ.get("KB_MAX_SIZE", "100mb"))
        files = walk_source(root, allow, max_size)
        source_count = len(files)
        indexed = owui.kb_file_count(read_key, kb_id)
        pending = owui.pending_files(read_key, kb_id)
        lst = None
        try:
            lst = owui.list_kb_files(read_key, kb_id)
        except owui.OwuiError:
            lst = None
        items = (lst or {}).get("items") or []
        per_file = [{"filename": it.get("filename"), "hash": it.get("hash"),
                     "directory_id": ((it.get("meta") or {}).get("data") or {}).get("directory_id"),
                     "status": ((it.get("data") or {}).get("status")),
                     "error": ((it.get("data") or {}).get("error"))}
                    for it in items]
        if relpath:
            per_file = [p for p in per_file if p.get("filename") == os.path.basename(relpath)]
        summary = {"source": source, "kb_id": kb_id,
                   "source_count": source_count,
                   "indexed_count": indexed,
                   "pending": len(pending) if pending is not None else None,
                   "files": per_file}
        if as_json:
            return self._ok(summary)
        # human-readable: glyphs, no emoji, no ETA (no daemon)
        idx = indexed if indexed is not None else "?"
        pen_s = "%d" % len(pending) if isinstance(pending, list) else "<unavailable>"
        lines = ["source (gdrive)   : %d allowlisted files" % source_count,
                 "indexed (OWUI KB) : %s" % idx,
                 "pending (OWUI)    : %s files awaiting extraction" % pen_s]
        if isinstance(indexed, int) and source_count and indexed >= source_count:
            lines.append("status            : ✓ sync COMPLETE (indexed >= source)")
        elif isinstance(indexed, int):
            lines.append("status            : ○ remaining=%d" % (source_count - indexed))
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

_SKIP_NAMES = {".sync-reports", ".sync.lock"}


def walk_source(root, allow, max_size):
    """Walk `root` and return one entry per allowlisted, in-size file:
    [{filename, path, checksum, size, abspath}]. `filename` is the basename,
    `path` is the directory relpath from `root` (POSIX, "" at root) — the shape
    OWUI sync/diff expects. `checksum` is the raw-file sha256. Skips symlinks,
    .sync-reports, .sync.lock. Empty list if root missing/empty (caller guards).

    Fails CLOSED on any OSError (stat, read, hash, or os.walk descent): a
    silently dropped file would reappear as `deleted` in sync/diff, and
    sync/cleanup would then remove its KB entry — data loss on a transient
    I/O/permission error. Raises GatewayError(500) so /index aborts instead."""
    out = []
    if not os.path.isdir(root):
        return out

    def _walk_err(err):
        raise GatewayError(500, "source walk failed: %s" % err)

    for dirpath, dirs, filenames in os.walk(root, onerror=_walk_err):
        dirs[:] = [d for d in dirs if d not in _SKIP_NAMES]
        for fn in filenames:
            if fn in _SKIP_NAMES:
                continue
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            if ext not in allow:
                continue
            abspath = os.path.join(dirpath, fn)
            try:
                if os.path.islink(abspath) or not os.path.isfile(abspath):
                    continue
                size = os.path.getsize(abspath)
            except OSError as e:
                raise GatewayError(500, "stat failed for %s: %s" % (abspath, e))
            if size > max_size:
                continue
            rel = os.path.relpath(abspath, root)
            d_rel = os.path.dirname(rel).replace(os.sep, "/")
            try:
                h = hashlib.sha256()
                with open(abspath, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                checksum = h.hexdigest()
            except OSError as e:
                raise GatewayError(500, "hash failed for %s: %s" % (abspath, e))
            out.append({"filename": fn, "path": d_rel, "checksum": checksum,
                        "size": size, "abspath": abspath})
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


def _parse_allow(s):
    """Comma-separated extensions -> set (lowercased, no leading dot)."""
    return {x.strip().lstrip(".").lower() for x in (s or "").split(",") if x.strip()}


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
                            "user provisioning + stateless gdrive index sync."},
    "paths": {
        "/health": {"get": {"summary": "Process + OWUI reachability", "security": []}},
        "/openapi.json": {"get": {"summary": "This document", "security": []}},
        "/index": {"post": {
            "summary": "Index (reconcile) a source into an OWUI KB (admin only)",
            "security": [{"bearerAuth": []}],
            "parameters": [
                {"name": "source", "in": "query", "schema": {"type": "string", "default": "gdrive"}},
                {"name": "kb_id", "in": "query", "required": False, "schema": {"type": "string", "format": "uuid"}},
                {"name": "force", "in": "query", "schema": {"type": "boolean", "default": False}},
                {"name": "dry_run", "in": "query", "schema": {"type": "boolean", "default": False}},
                {"name": "reindex_all", "in": "query", "schema": {"type": "boolean", "default": False}}],
            "responses": {"200": {"description": "per-file index result"},
                          "403": {"description": "admin role required"},
                          "422": {"description": "empty source (use force=1)"}}}},
        "/status": {"get": {
            "summary": "KB index status (read; read-scoped key works)",
            "security": [{"bearerAuth": []}],
            "parameters": [
                {"name": "source", "in": "query", "schema": {"type": "string", "default": "gdrive"}},
                {"name": "kb_id", "in": "query", "required": False, "schema": {"type": "string", "format": "uuid"}},
                {"name": "file", "in": "query", "required": False, "schema": {"type": "string"}},
                {"name": "json", "in": "query", "schema": {"type": "boolean", "default": False}}],
            "responses": {"200": {"description": "status (text or json)"}}}},
        "/admin/users": {"post": {"summary": "Admin user provisioning (admin only)", "security": [{"bearerAuth": []}]}},
        "/memory/whoami": {"get": {"summary": "Caller identity", "security": [{"bearerAuth": []}]}},
        "/memory/groups": {"get": {"summary": "List memory groups", "security": [{"bearerAuth": []}]}},
        "/memory/status": {"get": {"summary": "Graphiti status", "security": [{"bearerAuth": []}]}},
        "/memory/episodes": {"get": {"summary": "List episodes", "security": [{"bearerAuth": []}]}},
        "/memory/add": {"post": {"summary": "Add memory", "security": [{"bearerAuth": []}]}},
        "/memory/search": {"post": {"summary": "Search facts", "security": [{"bearerAuth": []}]}},
        "/memory/forget": {"post": {"summary": "Clear a Group", "security": [{"bearerAuth": []}]}},
        "/memory/delete-edge": {"post": {"summary": "Delete an edge", "security": [{"bearerAuth": []}]}},
        "/memory/delete-episode": {"post": {"summary": "Delete an episode", "security": [{"bearerAuth": []}]}}},
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