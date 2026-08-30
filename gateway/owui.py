"""Open WebUI HTTP client (stdlib urllib) for identity + user provisioning
+ the gdrive index sync protocol.

All calls go to OWUI over the container-internal `owui_net` network. For user
provisioning the gateway forwards the caller's KB_API_KEY (an OWUI key) to
authorize admin operations, and signs in as a newly created user with that
user's temp password to obtain that user's own JWT (never the admin's) for
per-user API-key generation (codex #3).

For the gdrive index path (/index, /status) the posture differs: the gateway
holds OPENWEBUI_ADMIN_API_KEY (compose env) and uses it for the OWUI sync
writes (sync/diff, upload, dirs/create, sync/cleanup, list files, delete
file). The caller's KB_API_KEY is authorization only (identity via whoami,
role via authorize.is_admin) — it is NOT forwarded for the write. This keeps
the admin key off the host for skill/agent callers; only the operator-run
gdrive-sync passes the admin KB_API_KEY. See _admin_key().

The gateway does NOT link files itself: POST /files/ with
metadata.knowledge_id queues OWUI's per-upload background task that extracts
(markitdown-ocr) -> embeds into the KB collection -> links. That background
task is the sole linker; /status reads file.data.status (via GET /files/) to
report real completed/pending/failed, and /index re-triggers failed (and, on
request, stalled pending) files by deleting + re-uploading them.

Raises OwuiError (mapped by app.py to 503 on transport failure, left as the
caller's concern for 4xx). Duplicate-email on add_user is surfaced as a
distinct OwuiConflict so the gateway can return 409.
"""
import json
import os
import urllib.error
import urllib.request


class OwuiError(Exception):
    """Failure talking to OWUI. `code` is the upstream HTTP status when this is
    an HTTP-level rejection (set by callers that read one), or None for a
    transport/I-O failure (-> 503). Additive: existing `OwuiError("...")` calls
    keep working (code defaults None)."""
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class OwuiConflict(Exception):
    """OWUI rejected a create because the email already exists (-> 409)."""


def _base():
    return os.environ.get("OWUI_URL", "http://openwebui:8080").rstrip("/")


def _timeout():
    return float(os.environ.get("OWUI_TIMEOUT", "15"))


def _index_timeout():
    """Timeout for the /index sync-protocol calls (sync/diff, dirs/create,
    sync/cleanup, list files, delete file). The 15s identity timeout is too
    short for a 159-file manifest diff, a paginated file-status scan, or a
    drained cleanup, so the index path uses this ceiling. Tunable via
    OWUI_INDEX_TIMEOUT (default 300s)."""
    return float(os.environ.get("OWUI_INDEX_TIMEOUT", "300"))


def _retrieve_timeout():
    """Timeout for a single /retrieve (OWUI query/collection forward). Query
    embedding can wait on a cold Ollama model load (~30s), so the 15s identity
    timeout is too short. Tunable via OWUI_RETRIEVE_TIMEOUT (default 60s)."""
    return float(os.environ.get("OWUI_RETRIEVE_TIMEOUT", "60"))


def _req(method, path, token=None, body=None, timeout=None):
    url = _base() + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or _timeout()) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode() or "")
    except urllib.error.URLError as e:
        raise OwuiError("OWUI unreachable: %s" % e)
    except OSError as e:
        # socket.timeout / TimeoutError are OSError, not URLError; without this
        # they escape _req and reach the gateway's generic 500. Wrap them so a
        # slow chat read becomes a 503, not an internal-error stack trace.
        raise OwuiError("OWUI I/O error: %s" % e)


def _j(method, path, token=None, body=None, timeout=None):
    code, txt = _req(method, path, token, body, timeout)
    try:
        data = json.loads(txt) if txt else None
    except Exception:
        data = None
    return code, data, txt


def whoami(api_key):
    """Resolve the caller's identity from their OWUI key. Returns
    {id,email,role} or raises OwuiError. code != 200 -> returns None (caller
    maps to 401)."""
    try:
        code, data, _ = _j("GET", "/api/v1/auths/", api_key)
    except OwuiError:
        raise
    if code != 200:
        return None
    if not isinstance(data, dict) or not data.get("email"):
        return None
    return {"id": data.get("id"), "email": data.get("email"),
            "role": data.get("role")}


def rag(api_key, messages, files):
    """RAG chat grounded on an OWUI KB, proxied with the CALLER's key (so OWUI
    enforces KB read access natively — no admin-key escalation, no gateway-side
    authz). The chat model is inserted server-side from OPENWEBUI_MODEL (the
    backend-configured model; the caller sends no model). Returns
    {"content": <answer>}. Raises OwuiError(code) on a non-200 chat (the gateway
    maps code -> 4xx/502/503) and OwuiError (no code, -> 503) on transport/timeout
    or when OPENWEBUI_MODEL is unset."""
    model = os.environ.get("OPENWEBUI_MODEL", "").strip()
    if not model:
        raise OwuiError("OPENWEBUI_MODEL not set in gateway env")
    body = {"model": model, "stream": False, "messages": messages}
    if files:
        body["files"] = files
    code, data, txt = _j("POST", "/api/chat/completions", api_key, body,
                         timeout=_index_timeout())
    if code != 200 or not isinstance(data, dict):
        raise OwuiError("rag -> HTTP %s: %s" % (code, (txt or "")[:200]), code=code)
    content = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    return {"content": content}


def query_collection(api_key, kb_id, query, hybrid, hybrid_bm25_weight, k):
    """POST /api/v1/retrieval/query/collection, forwarded with the CALLER's key
    so OWUI enforces KB read access natively (_validate_collection_access;
    403 on deny — no gateway-side authz replication, same posture as rag()). The
    gateway maps mode -> {hybrid, hybrid_bm25_weight} and forwards a SINGLE
    collection_name ([kb_id]) so every hit attributes to that KB. `hybrid` is
    always sent; `hybrid_bm25_weight` is sent only when not None (omitted ->
    OWUI applies the configured RAG_HYBRID_BM25_WEIGHT, the single source in
    .env; patch 7 honors the per-request value over the global). Returns the raw
    {documents, distances, metadatas} (the gateway flattens). Raises
    OwuiError(code) on a non-200 (the gateway maps code -> 4xx/502/503) and
    OwuiError (no code, -> 503) on transport/timeout."""
    body = {"collection_names": [kb_id], "query": query, "k": k, "hybrid": hybrid}
    if hybrid_bm25_weight is not None:
        body["hybrid_bm25_weight"] = hybrid_bm25_weight
    code, data, txt = _j("POST", "/api/v1/retrieval/query/collection", api_key,
                         body, timeout=_retrieve_timeout())
    if code != 200 or not isinstance(data, dict):
        raise OwuiError("query_collection -> HTTP %s: %s" % (code, (txt or "")[:200]), code=code)
    return data


def signin(email, password):
    """Sign in as a user; return that user's JWT, or None on bad creds."""
    code, data, _ = _j("POST", "/api/v1/auths/signin", None,
                       {"email": email, "password": password})
    if code == 200 and isinstance(data, dict):
        return data.get("token", "")
    return None


def add_user(admin_key, name, email, password, role):
    """Create a user via the admin add endpoint. role is passed explicitly
    (OWUI defaults to 'pending', which cannot sign in). Returns the new user
    dict {id, email, role, ...}. Raises OwuiConflict on duplicate email."""
    code, data, txt = _j("POST", "/api/v1/auths/add", admin_key,
                         {"name": name, "email": email, "password": password,
                          "role": role})
    if code == 200 and isinstance(data, dict) and data.get("id"):
        return data
    # OWUI signals a duplicate email as 400/409 with a mention of existing.
    low = (txt or "").lower()
    if code in (400, 409) or "already" in low or "exists" in low:
        raise OwuiConflict("email %s already exists" % email)
    raise OwuiError("add_user -> HTTP %s: %s" % (code, (txt or "")[:200]))


def gen_api_key(jwt):
    """Generate an API key for the account that owns `jwt`. Returns the key
    string, or raises OwuiError."""
    code, data, txt = _j("POST", "/api/v1/auths/api_key", jwt)
    if code == 200 and isinstance(data, dict) and data.get("api_key"):
        return data["api_key"]
    raise OwuiError("gen_api_key -> HTTP %s: %s" % (code, (txt or "")[:200]))


def delete_user(admin_key, user_id):
    """Admin delete of a user by id. Best-effort (used for rollback). Returns
    True on success, False otherwise."""
    code, _, _ = _j("DELETE", "/api/v1/users/%s" % user_id, admin_key)
    return code in (200, 204)


def revoke_api_key(jwt):
    """Best-effort revoke of the API key belonging to the JWT owner."""
    code, _, _ = _j("DELETE", "/api/v1/auths/api_key", jwt)
    return code in (200, 204)


# --- capability probe (codex #3) --------------------------------------------
# The provisioning flow depends on endpoints that vary by Open WebUI image
# tag (OPENWEBUI_IMAGE_TAG is mutable). Probe /openapi.json (no auth) at startup
# and report which required paths+methods are present. The gateway exposes
# /admin/users only if all are present; otherwise it returns 501.
PROVISIONING_PATHS = {
    "POST /api/v1/auths/add": ("POST", "/api/v1/auths/add"),
    "POST /api/v1/auths/signin": ("POST", "/api/v1/auths/signin"),
    "POST /api/v1/auths/api_key": ("POST", "/api/v1/auths/api_key"),
    "GET /api/v1/auths/": ("GET", "/api/v1/auths/"),
    "DELETE /api/v1/users/{user_id}": ("DELETE", "/api/v1/users/{user_id}"),
}


def provisioning_capabilities():
    """Return (ok: bool, missing: [str], error: str|None) by reading OWUI's
    OpenAPI schema. `ok` is True only if every required path+method is present."""
    try:
        code, data, _ = _j("GET", "/openapi.json")
    except OwuiError as e:
        return False, [], str(e)
    if code != 200 or not isinstance(data, dict):
        return False, [], "openapi.json unreadable (HTTP %s)" % code
    paths = data.get("paths") or {}
    missing = []
    for label, (method, path) in PROVISIONING_PATHS.items():
        spec = paths.get(path)
        if not isinstance(spec, dict) or method.lower() not in spec:
            missing.append(label)
    return (len(missing) == 0), missing, None


# --- gdrive index sync protocol (admin key) ----------------------------------
# Stateless reconciliation against the live KB: the client sends a source
# manifest, OWUI computes the diff against the KB's current files, the gateway
# acts on added/modified/deleted. The KB itself is the state (no client
# manifest). See gateway/app.py /index for the orchestration.

def _admin_key():
    """The gateway's held admin key for the /index sync writes (compose env).
    Posture change: the gateway holds OPENWEBUI_ADMIN_API_KEY for the index
    path; the caller's KB_API_KEY is authorization only. Raises OwuiError if
    unset (the gateway returns 500)."""
    k = os.environ.get("OPENWEBUI_ADMIN_API_KEY", "").strip()
    if not k:
        raise OwuiError("OPENWEBUI_ADMIN_API_KEY not set in gateway env")
    return k


def _upload_timeout():
    return float(os.environ.get("OWUI_UPLOAD_TIMEOUT", "180"))


def sync_diff(admin_key, kb_id, manifest):
    """POST /api/v1/knowledge/{id}/sync/diff. `manifest` is a list of
    {filename, path, checksum, size} (filename=basename, path=directory relpath
    only, checksum=raw-file sha256). Returns the diff dict
    {added, modified, deleted, mkdir, rmdir, unmodified_count, directory_map}.
    Raises OwuiError on transport failure or non-200."""
    code, data, txt = _j("POST", "/api/v1/knowledge/%s/sync/diff" % kb_id,
                         admin_key, {"manifest": manifest}, timeout=_index_timeout())
    if code != 200 or not isinstance(data, dict):
        raise OwuiError("sync_diff -> HTTP %s: %s" % (code, (txt or "")[:200]))
    return data


def sync_cleanup(admin_key, kb_id, file_ids, dir_ids):
    """POST /api/v1/knowledge/{id}/sync/cleanup. Removes orphan files + empty
    dirs. Returns True on success, raises OwuiError on non-200."""
    code, _, txt = _j("POST", "/api/v1/knowledge/%s/sync/cleanup" % kb_id,
                      admin_key, {"file_ids": file_ids or [], "dir_ids": dir_ids or []},
                      timeout=_index_timeout())
    if code != 200:
        raise OwuiError("sync_cleanup -> HTTP %s: %s" % (code, (txt or "")[:200]))
    return True


def create_directory(admin_key, kb_id, name, parent_id):
    """POST /api/v1/knowledge/{id}/dirs/create. Directories are first-class OWUI
    entities (sync/diff's directory_map only carries EXISTING paths); a new
    subdir must be created before files can be linked into it, and its id is
    needed as the upload/batch-add directory_id. `name` is the single segment;
    `parent_id` is the parent dir id or None for a top-level dir. Returns the
    directory model dict {id, name, parent_id, ...}. Raises OwuiError on
    transport failure or non-200."""
    code, data, txt = _j("POST", "/api/v1/knowledge/%s/dirs/create" % kb_id,
                         admin_key, {"name": name, "parent_id": parent_id},
                         timeout=_index_timeout())
    if code != 200 or not isinstance(data, dict) or not data.get("id"):
        raise OwuiError("create_directory %s/%s -> HTTP %s: %s" % (kb_id, name, code, (txt or "")[:200]))
    return data


def _cd_filename(name):
    """Escape a filename for a quoted Content-Disposition parameter: strip CR/LF
    (header injection) and backslash-escape double quotes + backslashes."""
    s = (name or "").replace("\r", "").replace("\n", "")
    return s.replace("\\", "\\\\").replace('"', '\\"')


def upload_file(admin_key, kb_id, file_hash, directory_id, filename, data_bytes,
                mtime=None, gdrive_meta=None):
    """POST /api/v1/files/ multipart: field 'file' (filename, raw bytes) + field
    'metadata' (JSON {knowledge_id, file_hash, directory_id, [mtime], [gdrive]}).
    `mtime` (optional, ISO-8601 UTC source mtime) is stored into File.meta.data so
    the OWUI chunk-metadata patch can propagate it into Chroma. `gdrive_meta`
    (optional, the parsed `<file>.meta.json` sidecar from scripts/gdrive-meta.py)
    is stored under File.meta.data.gdrive as the file-level Drive record
    (description, labels, grounded, approval, comments). OWUI persists the
    metadata dict verbatim into File.meta.data. The idempotency patch matches on
    (knowledge_id, directory_id, filename, file_hash) and returns the existing
    file_id without re-extracting when unchanged. Returns the FileModel dict
    {id, hash, filename, meta, ...}. Raises OwuiError on transport failure or
    non-200."""
    import uuid
    metadata = {"knowledge_id": kb_id, "file_hash": file_hash, "directory_id": directory_id}
    if mtime is not None:
        metadata["mtime"] = mtime
    if gdrive_meta is not None:
        metadata["gdrive"] = gdrive_meta
    boundary = uuid.uuid4().hex
    body = bytearray()
    body += ("--%s\r\n" % boundary).encode()
    body += ('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % _cd_filename(filename)).encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += data_bytes
    body += b"\r\n"
    body += ("--%s\r\n" % boundary).encode()
    body += b'Content-Disposition: form-data; name="metadata"\r\n'
    body += b"Content-Type: application/json\r\n\r\n"
    body += json.dumps(metadata).encode()
    body += b"\r\n"
    body += ("--%s--\r\n" % boundary).encode()
    url = _base() + "/api/v1/files/"
    req = urllib.request.Request(
        url, data=bytes(body),
        headers={"Authorization": "Bearer " + admin_key,
                 "Content-Type": "multipart/form-data; boundary=%s" % boundary},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_upload_timeout()) as r:
            txt = r.read().decode()
    except urllib.error.HTTPError as e:
        raise OwuiError("upload_file -> HTTP %s: %s" % (e.code, (e.read().decode() or "")[:200]))
    except urllib.error.URLError as e:
        raise OwuiError("OWUI unreachable: %s" % e)
    try:
        data = json.loads(txt)
    except Exception:
        raise OwuiError("upload_file -> non-JSON response: %s" % (txt or "")[:200])
    if not isinstance(data, dict) or not data.get("id"):
        raise OwuiError("upload_file -> no file_id: %s" % (txt or "")[:200])
    return data


def list_file_status(admin_key, kb_id):
    """GET /api/v1/files/?content=false&page=N, paged until `total` is covered
    (OWUI hardcodes PAGE_SIZE=50). Filters items by
    meta.data.knowledge_id == kb_id. Returns [{file_id, filename, file_hash,
    directory_id, size, created_at, status, error}]. size is File.meta.size
    (bytes, set at upload); created_at is the OWUI File unix timestamp (used by
    /status for drain runtime). This is the real progress signal: the
    /knowledge/{id}/files list defers File.data (status reads null there), but
    GET /files/ returns data.status + data.error (content=false strips the
    large content key but keeps status + error). Admin key sees every file
    (BYPASS_ADMIN_ACCESS_CONTROL); a read-scoped caller key would see only its
    own files, so /status uses the admin key. Raises OwuiError on transport
    failure or non-200."""
    out = []
    page = 1
    while True:
        code, data, txt = _j("GET", "/api/v1/files/?content=false&page=%d" % page,
                             admin_key, timeout=_index_timeout())
        if code != 200 or not isinstance(data, dict):
            raise OwuiError("list_file_status page %d -> HTTP %s: %s"
                            % (page, code, (txt or "")[:200]))
        items = data.get("items") or []
        for it in items:
            meta = it.get("meta") or {}
            mdata = (meta.get("data") or {})
            if mdata.get("knowledge_id") != kb_id:
                continue
            d = it.get("data") or {}
            out.append({"file_id": it.get("id"), "filename": it.get("filename"),
                        "file_hash": mdata.get("file_hash"),
                        "directory_id": mdata.get("directory_id"),
                        "size": meta.get("size"),
                        "created_at": it.get("created_at"),
                        "status": d.get("status"), "error": d.get("error")})
        # Last page (short) or covered the reported total. Safety bound below.
        if len(items) < 50 or page * 50 >= (data.get("total") or 0):
            break
        page += 1
        if page > 200:  # >10k files: stop paging rather than loop unbounded
            break
    return out


def delete_file(admin_key, file_id):
    """DELETE /api/v1/files/{id}. Admin can delete any file; OWUI cleans the KB
    associations + embeddings + the file-{id} vector collection first. Used by
    /index's re-trigger to remove a failed/stalled file before re-upload so the
    upload-idempotency reuse (same-hash -> return existing without re-queueing
    the background task) does not block a fresh extraction. Returns True on
    success, raises OwuiError on non-200/204."""
    code, _, txt = _j("DELETE", "/api/v1/files/%s" % file_id, admin_key,
                      timeout=_index_timeout())
    if code not in (200, 204):
        raise OwuiError("delete_file %s -> HTTP %s: %s" % (file_id, code, (txt or "")[:200]))
    return True


def list_kb_files(admin_key, kb_id):
    """GET /api/v1/knowledge/{id}/files?limit=1000. Admin-only `limit` override
    (PAGE_ITEM_COUNT default is 30) so reindex_all's drain + dry_run see EVERY
    linked file, not just page 1. Returns {items:[{id,hash,filename,data,meta,
    ...}], directories:[...], breadcrumbs, total}. Note: this list defers
    File.data (status reads null) — use list_file_status for status. Raises
    OwuiError on non-200."""
    code, data, txt = _j("GET", "/api/v1/knowledge/%s/files?limit=1000" % kb_id,
                         admin_key, timeout=_index_timeout())
    if code != 200 or not isinstance(data, dict):
        raise OwuiError("list_kb_files -> HTTP %s: %s" % (code, (txt or "")[:200]))
    return data