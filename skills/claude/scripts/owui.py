#!/usr/bin/env python3
"""CLI wrapper for a self-hosted Open WebUI knowledge base.

Two surfaces, one key (your non-admin user key, KB_API_KEY):

  * KB surface (read-scoped): list/search KBs, retrieve (semantic) from a KB, RAG chat
    grounded on a KB, read file content. The key is read-only here: it
    cannot upload, modify, or delete KBs/files it does not own.

  * Projects-memory surface (user-key writes to OWNED KBs): index
    ~/.claude/projects/<encoded>/memory/*.md into OWUI KBs (one KB per project),
    retrieve across those KBs, and check a repo's index status. The user key
    CREATES + OWNS each project KB (KB.user.email == caller) and uploads/deletes
    files in it. OWUI gates KB creation on the workspace.knowledge permission,
    which is off by default: run `make projects-bootstrap` once (admin) to enable
    it before the first `index-projects`.

The stack is fronted by Caddy at KB_HOST: OWUI REST is at the KB_HOST root
(/api/* via Caddy catch-all -> openwebui:8080). The api-gateway memory endpoints are
at /memory/* on the same KB_HOST. One URL, one key.

Zero dependencies (Python 3.10+ stdlib). Config: the wrapper is a thin client.
It reads ONLY two env vars from the shell environment — KB_HOST and KB_API_KEY.
It does not read .env / .env.local files (set both in your shell before invoking
it). RAG chat is proxied by the api-gateway (POST /memory/rag), which inserts the
chat model server-side from OPENWEBUI_MODEL; the wrapper carries no model. The
projects-memory --wait deadline is 600s (fixed).

Env vars: KB_HOST, KB_API_KEY.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import platform
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def base_url():
    if os.environ.get("KB_HOST"):
        return os.environ["KB_HOST"].rstrip("/")
    sys.exit("FAIL  no KB_HOST: set KB_HOST in your shell env "
             "(e.g. export KB_HOST=http://localhost:3000)")


def api_key():
    # KB_API_KEY is the unified key for the stack (an Open WebUI key).
    if os.environ.get("KB_API_KEY"):
        return os.environ["KB_API_KEY"]
    sys.exit("FAIL  no API key: set KB_API_KEY in your shell env")


def call(base, key, method, path, body=None):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + key}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            txt = r.read().decode()
            return r.status, txt
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def jget(base, key, method, path, body=None):
    code, txt = call(base, key, method, path, body)
    if code != 200:
        sys.exit("FAIL  %s %s -> HTTP %s: %s" % (method, path, code, txt[:300]))
    try:
        return json.loads(txt)
    except Exception:
        sys.exit("FAIL  non-JSON response from %s %s: %s" % (method, path, txt[:300]))


def _file_gdrive(base, key, file_id):
    """Read File.meta.data.gdrive for one file — the Drive record (description,
    labels, grounded flag, approval, comments) the gateway stored at upload from
    the <file>.meta.json sidecar. The chunk carries only file_id (gdrive is
    file-level, not in chunk metadata), so retrieve joins it here per file_id.
    Defensive: any non-200 / missing gdrive -> None (a chunk with no gdrive meta
    still returns, never aborts the retrieve). One GET per unique file_id."""
    if not file_id:
        return None
    code, txt = call(base, key, "GET", "/api/v1/files/%s?content=false" % file_id)
    if code != 200:
        return None
    try:
        d = json.loads(txt)
    except Exception:
        return None
    return ((d.get("meta") or {}).get("data") or {}).get("gdrive")


def _gdrive_view(g):
    """Curate the file-level gdrive record into the per-chunk fields a retrieval
    result (and a future grounding rerank) consumes: the strong signals (grounded,
    approval status + complete_time), the weak-signal text (description) +
    time-conditioning (modified_time), and label/comment counts. Returns None
    when the file has no gdrive meta."""
    if not g:
        return None
    ap = g.get("approval") or {}
    return {
        "grounded": g.get("grounded"),
        "labels": g.get("labels") or [],
        "approval_status": ap.get("status"),
        "approval_complete_time": ap.get("complete_time"),
        "comment_count": len(g.get("comments") or []),
        "description": g.get("description") or "",
        "modified_time": g.get("modified_time"),
    }


# --- subcommands -------------------------------------------------------------

def cmd_whoami(base, key, a):
    d = jget(base, key, "GET", "/api/v1/auths/")
    print(json.dumps({"email": d.get("email"), "role": d.get("role")}))


def cmd_kbs(base, key, a):
    d = jget(base, key, "GET", "/api/v1/knowledge/")
    items = d.get("items", []) if isinstance(d, dict) else d
    kbs = [{"id": k.get("id"), "name": k.get("name"),
            "file_count": k.get("file_count"), "write_access": k.get("write_access"),
            "owner": ((k.get("user") or {}).get("email") or "-")} for k in items]
    print(json.dumps({"kbs": kbs}))


def cmd_kb(base, key, a):
    d = jget(base, key, "GET", "/api/v1/knowledge/%s" % a.id)
    # The detail endpoint returns user=null (server limitation); only the list
    # endpoint populates user.email. Fill it in from the list when missing.
    if d.get("user") is None:
        lst = jget(base, key, "GET", "/api/v1/knowledge/")
        items = lst.get("items", []) if isinstance(lst, dict) else lst
        for k in items:
            if k.get("id") == a.id:
                d["user"] = k.get("user")
                break
    print(json.dumps(d))


def cmd_search_kbs(base, key, a):
    d = jget(base, key, "GET", "/api/v1/knowledge/search?query=%s" % urllib.parse.quote(a.query))
    items = d.get("items", []) if isinstance(d, dict) else d
    kbs = [{"id": k.get("id"), "name": k.get("name")} for k in items]
    print(json.dumps({"kbs": kbs}))


def _is_uuid(arg):
    try:
        uuid.UUID(arg)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _resolve_kb(base, key, arg):
    """Resolve a KB name-or-id to (kb_id, kb_name) against
    GET /api/v1/knowledge/. Resolution order: exact id; a VALID UUID that is
    not a real id FAILS (no fallthrough to name matching, so a wrong
    hand-copied id cannot silently query the wrong KB); exact name; else fail
    with the visible KB list as `name (id)` pairs. Exact match only — no
    substring/fragment step (a fragment unique today can turn ambiguous or
    point at a different KB later). ID takes precedence over name."""
    d = jget(base, key, "GET", "/api/v1/knowledge/")
    items = d.get("items", []) if isinstance(d, dict) else d
    cands = ["%s (%s)" % (k.get("name", ""), k.get("id", "")) for k in items]
    for k in items:                       # exact id
        if arg == k.get("id"):
            return k.get("id"), k.get("name", "")
    if _is_uuid(arg):                      # valid UUID, unknown -> fail, no fallthrough
        sys.exit("FAIL  unknown KB id %r; visible KBs: %s"
                 % (arg, "; ".join(cands)))
    for k in items:                       # exact name
        if arg == k.get("name"):
            return k.get("id"), k.get("name", "")
    sys.exit("FAIL  no KB matches name or id %r; visible KBs: %s"
             % (arg, "; ".join(cands)))


def _mode(a):
    """Resolve the retrieval mode from --mode + the deprecated --no-hybrid alias.
    --no-hybrid is a deprecated alias for --mode vector (pure vector, no hybrid
    search). --no-hybrid conflicts with an explicit --mode hybrid/lexical and
    exits non-zero; --no-hybrid + --mode vector is redundant but accepted. A bare
    --no-hybrid (no --mode) emits a deprecation line to stderr."""
    mode = a.mode or "hybrid"
    if getattr(a, "no_hybrid", False):
        if a.mode is None or a.mode == "vector":
            if a.mode is None:
                sys.stderr.write(
                    "deprecation: --no-hybrid is an alias for --mode vector; use --mode vector\n")
            return "vector"
        sys.exit("FAIL  --no-hybrid conflicts with --mode %r (use --mode vector)" % a.mode)
    return mode


def cmd_retrieve(base, key, a):
    mode = _mode(a)
    kb_id, kb_name = _resolve_kb(base, key, a.kb)
    # Gateway-mediated retrieve: POST /retrieve (Caddy -> api-gateway) maps mode
    # -> {hybrid, hybrid_bm25_weight} and forwards to OWUI with the caller's key
    # (OWUI enforces KB read access natively). The gateway flattens the OWUI
    # {documents,distances,metadatas} response into 8-key hits; the gdrive join
    # stays here (File.meta.data.gdrive is file-level, one GET per file_id).
    body = {"kb_id": kb_id, "query": a.query, "k": a.k, "mode": mode}
    d = jget(base, key, "POST", "/retrieve", body)
    hits = d.get("hits", [])
    score_order = d.get("score_order", "asc")
    gcache = {}
    for fid in {h.get("file_id") for h in hits if h.get("file_id")}:
        gcache[fid] = _gdrive_view(_file_gdrive(base, key, fid))
    for h in hits:
        h["gdrive"] = gcache.get(h.get("file_id"))
    print(json.dumps({"kb_id": kb_id, "kb_name": kb_name, "mode": mode,
                      "score_order": score_order, "hits": hits}))


def cmd_rag(base, key, a):
    # RAG is proxied by the api-gateway (POST /memory/rag), which inserts the
    # chat model server-side from OPENWEBUI_MODEL and forwards the caller's key
    # to OWUI so KB read access is enforced natively. The wrapper carries no
    # model (the model is backend-side config; everything is tested against it).
    body = {"messages": [{"role": "user", "content": a.question}]}
    if a.kb:
        # Ground the chat on KB(s). OWUI's /api/chat/completions (which the
        # gateway proxies to) reads KBs from the top-level `files` field as
        # collection items — NOT from a `knowledge` field (ignored) or
        # `metadata.knowledge` (metadata is discarded and replaced server-side).
        # type:collection -> whole-KB vector search; type:file would scope to
        # one file id.
        body["files"] = [{"type": "collection", "id": kid} for kid in a.kb]
    d = jget(base, key, "POST", "/memory/rag", body)
    print(d.get("content") or "(empty response)")


def _content_ext(ctype):
    """Map a Content-Type to a file extension for saving a binary /content
    response. Returns '' when there is no useful mapping (the caller saves the
    raw bytes with no extension)."""
    c = (ctype or "").split(";")[0].strip().lower()
    return {
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.ms-powerpoint": ".ppt",
        "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
        "image/bmp": ".bmp", "image/tiff": ".tiff", "image/webp": ".webp",
    }.get(c, "")


def cmd_file(base, key, a):
    # Print a file's TEXT content. Default: GET /files/{id}/data/content returns
    # the EXTRACTED text OWUI produced at index time (stored in
    # file.data['content']) — for PDF/Office/image files this is the full
    # extracted text, so the caller needs no client-side extractor. Access is
    # gated by file read (a KB read grant covers the files in that KB). With
    # --raw, skip the extracted text and fetch GET /files/{id}/content (the
    # ORIGINAL bytes) instead.
    if not getattr(a, "raw", False):
        code, txt = call(base, key, "GET", "/api/v1/files/%s/data/content" % a.id)
        if code == 200:
            try:
                content = (json.loads(txt) or {}).get("content", "")
            except Exception:
                content = ""
            if content:
                sys.stdout.write(content if content.endswith("\n") else content + "\n")
                return
    # Fallback: GET /files/{id}/content returns the ORIGINAL bytes. Text files
    # decode and print; binary files (no extracted text — e.g. extraction
    # pending or a raw-only upload) are saved to a temp file with a note. Fetch
    # directly (not via call()) to keep the Content-Type and handle a binary
    # body (call()'s r.read().decode() would raise UnicodeDecodeError on a PDF).
    # The extracted, searchable text is also available via `retrieve <kb> "<q>"`.
    url = base + "/api/v1/files/%s/content" % a.id
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key}, method="GET")
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        sys.exit("FAIL  GET /api/v1/files/%s/content -> HTTP %s: %s"
                 % (a.id, e.code, e.read().decode(errors="replace")[:300]))
    try:
        txt = raw.decode("utf-8")
    except UnicodeDecodeError:
        ext = _content_ext(ctype)
        fd, path = tempfile.mkstemp(prefix="owui-file-", suffix=ext)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        sys.stderr.write("NOTE  file %s has no extracted text (Content-Type: %s, %d bytes); the\n"
                         "  /content endpoint returns the RAW file. Saved to: %s\n"
                         "  Use `retrieve <kb> \"<query>\"` for the extracted text chunks, or open the saved file.\n"
                         % (a.id, ctype or "?", len(raw), path))
        if getattr(a, "raw", False):
            sys.stderr.write("  (fetched with --raw)\n")
        return  # not a failure: file saved + guidance emitted
    sys.stdout.write(txt if txt.endswith("\n") else txt + "\n")


# --- projects-memory surface (user-key writes to OWNED KBs) ------------------
# Source: ~/.claude/projects/<encoded-dir>/memory/*.md (Claude auto-memory). The
# encoded dir is the absolute path with '/' and '.' -> '-' (leading '/' -> '-');
# decoding is lossy, so the exact encoded dir is the authoritative project key.

def _short_host():
    # platform.node() is available in non-interactive shells (HOSTNAME is not).
    return (platform.node() or "unknown").split(".")[0]


def _whoami(base, key):
    code, txt = call(base, key, "GET", "/api/v1/auths/")
    if code != 200:
        sys.exit("FAIL  whoami -> HTTP %s: %s" % (code, (txt or "")[:300]))
    return json.loads(txt) if txt else {}


def _encode_path(path):
    """Claude project encoding: '/' and '.' -> '-' (leading '/' -> '-')."""
    return path.replace("/", "-").replace(".", "-")


def _decode_project_path(encoded):
    """Decode an encoded dir to a path (leading '-' -> '/', then '-' -> '/').
    Lossy ('/' and '.' both encode to '-'). If the decoded path is a real dir on
    disk, return its realpath (authoritative, symlink-safe); else the lossy str."""
    s = encoded.lstrip("-")
    decoded = ("/" + s.replace("-", "/")) if encoded.startswith("-") else s.replace("-", "/")
    return os.path.realpath(decoded) if os.path.isdir(decoded) else decoded


def _repo_name(encoded, project_path):
    """Git repo name = basename of the real path. Authoritative when project_path
    is a real dir (basename of it); else fall back to the encoded dir's last '-'
    segment (lossy)."""
    if project_path and os.path.isdir(project_path):
        return os.path.basename(project_path.rstrip("/")) or project_path
    tail = encoded.lstrip("-")
    return tail.rsplit("-", 1)[-1] if "-" in tail else tail


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _cd_filename(name):
    """Escape a filename for a quoted Content-Disposition parameter: strip CR/LF
    (header injection) and backslash-escape double quotes + backslashes."""
    s = (name or "").replace("\r", "").replace("\n", "")
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _list_all_files(base, key):
    """Paged GET /api/v1/files/?content=false&page=N (50/page) until the reported
    `total` is covered. User-scoped: a user key sees only its own files, admin
    sees all. Returns the raw item list (each {id,filename,meta,data,...})."""
    out = []
    page = 1
    while True:
        code, txt = call(base, key, "GET", "/api/v1/files/?content=false&page=%d" % page)
        if code != 200:
            sys.exit("FAIL  GET /api/v1/files/ page %d -> HTTP %s: %s"
                     % (page, code, (txt or "")[:300]))
        d = json.loads(txt) if txt else {}
        items = (d.get("items") or []) if isinstance(d, dict) else (d or [])
        out.extend(items)
        total = (d.get("total") or 0) if isinstance(d, dict) else 0
        if len(items) < 50 or page * 50 >= total or page > 200:  # last page / covered / safety
            break
        page += 1
    return out


def _kb_files(base, key, kb_id):
    """Files for kb_id from the unified GET /api/v1/files/?content=false source
    (the linked-only /knowledge/{id}/files MISSES pending/unlinked uploads).
    Returns [{id, filename, file_hash, status, error}] (file_hash = raw sha256)."""
    out = []
    for it in _list_all_files(base, key):
        m = it.get("meta") or {}
        md = m.get("data") or {}
        if md.get("knowledge_id") != kb_id:
            continue
        d = it.get("data") or {}
        out.append({"id": it.get("id"), "filename": it.get("filename"),
                     "file_hash": m.get("file_hash"),
                     "status": d.get("status"), "error": d.get("error")})
    return out


def _kb_status(base, key, kb_id):
    """Drain status for kb_id from the unified file source. Returns
    {completed, pending, processing, failed, failed_files, files}. Reused by
    index-projects --wait + status-projects."""
    files = _kb_files(base, key, kb_id)
    failed = [f for f in files if f["status"] == "failed"]
    return {
        "completed": sum(1 for f in files if f["status"] == "completed"),
        "pending": sum(1 for f in files if f["status"] == "pending"),
        "processing": sum(1 for f in files if f["status"] == "processing"),
        "failed": len(failed),
        "failed_files": [{"filename": f["filename"], "error": f["error"]} for f in failed],
        "files": [{"filename": f["filename"], "status": f["status"]} for f in files],
    }


def _upload_memory_file(base, key, kb_id, file_hash, filename, data_bytes, meta_extra):
    """POST /api/v1/files/ multipart: 'file' part (filename, raw bytes) +
    'metadata' part (JSON, lands FLAT in File.meta.data). The idempotency patch
    matches on (knowledge_id, directory_id, filename, file_hash): same hash reuses
    the existing File (no re-extract); a DIFFERENT hash reclaims the stale File
    WITHOUT cleaning KB vectors, so modified files are delete-then-upload by the
    caller (never rely on this upload's own reclaim). Returns (FileModel|None, err)."""
    import uuid
    metadata = {"knowledge_id": kb_id, "file_hash": file_hash, "directory_id": ""}
    metadata.update(meta_extra or {})
    boundary = uuid.uuid4().hex
    body = bytearray()
    body += ("--%s\r\n" % boundary).encode()
    body += ('Content-Disposition: form-data; name="file"; filename="%s"\r\n'
             % _cd_filename(filename)).encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += data_bytes
    body += b"\r\n"
    body += ("--%s\r\n" % boundary).encode()
    body += b'Content-Disposition: form-data; name="metadata"\r\n'
    body += b"Content-Type: application/json\r\n\r\n"
    body += json.dumps(metadata).encode()
    body += b"\r\n"
    body += ("--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(
        base + "/api/v1/files/", data=bytes(body),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "multipart/form-data; boundary=%s" % boundary},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            txt = r.read().decode()
    except urllib.error.HTTPError as e:
        return None, "HTTP %s: %s" % (e.code, (e.read().decode(errors="replace") or "")[:200])
    except Exception as e:
        return None, "transport: %s" % e
    try:
        data = json.loads(txt)
    except Exception:
        return None, "non-JSON: %s" % (txt or "")[:200]
    if not isinstance(data, dict) or not data.get("id"):
        return None, "no file_id: %s" % (txt or "")[:200]
    return data, None


def _delete_file(base, key, file_id):
    """DELETE /api/v1/files/{id} (router path: cleans the knowledge_file link AND
    the KB vectors). The caller owns its uploaded files, so the user key works.
    Returns (ok bool, err str)."""
    code, txt = call(base, key, "DELETE", "/api/v1/files/%s" % file_id)
    if code not in (200, 204):
        return False, "HTTP %s: %s" % (code, (txt or "")[:200])
    return True, None


def _search_one_kb(base, key, kb_id, query, k, mode):
    """POST /retrieve (gateway-mediated) for a SINGLE KB. The gateway forwards a
    single collection_name to OWUI, flattens the response, and returns
    {hits, score_order}. Returns (hits, score_order, err); score_order is 'asc'
    on error (cosine distance convention, the safer default)."""
    body = {"kb_id": kb_id, "query": query, "k": k, "mode": mode}
    code, txt = call(base, key, "POST", "/retrieve", body)
    if code != 200:
        return [], "asc", "HTTP %s: %s" % (code, (txt or "")[:200])
    d = json.loads(txt)
    hits = d.get("hits", [])
    for h in hits:
        h["kb_id"] = kb_id
    return hits, d.get("score_order", "asc"), None


def _parse_repo(desc):
    """Extract repo=<x> from a KB description (set by index-projects)."""
    for tok in (desc or "").split("|"):
        tok = tok.strip()
        if tok.startswith("repo="):
            return tok[len("repo="):].strip()
    return None


def _wait_deadline():
    return time.time() + 600


def cmd_index_projects(base, key, a):
    me = _whoami(base, key)
    account = me.get("email", "?")
    host = a.host or _short_host()
    root = os.path.expanduser(a.root)
    if not os.path.isdir(root):
        sys.exit("FAIL  projects root not found: %s" % root)
    projects = []
    for encoded in sorted(os.listdir(root)):
        mem = os.path.join(root, encoded, "memory")
        if encoded in ("-", "") or not os.path.isdir(mem):
            continue
        if a.project and a.project not in encoded:
            continue
        projects.append((encoded, mem))
    result = {"projects": [],
              "total": {"added": 0, "modified": 0, "reused": 0,
                        "deleted": 0, "failed": 0},
              "waited": []}
    if not projects:
        print(json.dumps(result))
        return
    d = jget(base, key, "GET", "/api/v1/knowledge/")
    items = d.get("items", []) if isinstance(d, dict) else d
    # Match only KBs the caller can WRITE (owner or write grant). Public-read
    # KBs (user:*) are visible to everyone; without this filter a same-name KB
    # owned by another user would be selected and uploads to it would 403. A
    # non-writable same-name KB is ignored and a new one is created instead
    # (KB names are not unique).
    kb_by_name = {k.get("name", ""): k for k in items if k.get("write_access")}
    touched = []  # (kb_id, kb_name) with uploads, for --wait
    for encoded, mem in projects:
        project_path = _decode_project_path(encoded)
        repo = _repo_name(encoded, project_path)
        kb_name = "%s--%s" % (host, encoded.lstrip("-"))
        kb = kb_by_name.get(kb_name)
        kb_id = kb["id"] if kb else None
        created = "exists" if kb else ("would-create" if a.dry_run else "created")
        pentry = {"kb_name": kb_name, "kb_id": kb_id, "repo": repo,
                  "created": created, "added": 0, "modified": 0, "reused": 0,
                  "deleted": 0, "failed": 0, "errors": []}
        if not a.dry_run and not kb:
            desc = ("Claude projects memory | repo=%s | host=%s | project=%s | path=%s"
                    % (repo, host, encoded, project_path))
            code, txt = call(base, key, "POST", "/api/v1/knowledge/create",
                             {"name": kb_name, "description": desc})
            if code != 200:
                pentry["created"] = "failed"
                pentry["failed"] = 1
                pentry["errors"].append("create -> HTTP %s: %s"
                                        % (code, (txt or "")[:200]))
                result["projects"].append(pentry)
                result["total"]["failed"] += 1
                continue
            kb_id = json.loads(txt).get("id")
            pentry["kb_id"] = kb_id
            # Grant public read (user:*) so every authenticated user can
            # retrieve this project KB. The caller owns it (their user key
            # created it), so access/update is permitted once
            # sharing.public_knowledge is enabled (make projects-bootstrap or
            # make kb-public-read). access/update REPLACES the grant set; a
            # fresh KB has no grants, so the public-read grant alone is correct
            # (owner access is implicit). On failure, log and continue — the
            # owner still gets their KB, and `make kb-public-read` backfills
            # visibility.
            gcode, gtxt = call(base, key, "POST",
                "/api/v1/knowledge/%s/access/update" % kb_id,
                {"access_grants": [{"principal_type": "user",
                                    "principal_id": "*", "permission": "read"}]})
            if gcode != 200:
                pentry["errors"].append("public-read grant -> HTTP %s: %s"
                                        % (gcode, (gtxt or "")[:200]))
        src = {}
        for fn in sorted(os.listdir(mem)):
            p = os.path.join(mem, fn)
            if os.path.isfile(p) and fn.endswith(".md"):
                src[fn] = (p, _sha256_file(p))
        existing = {}
        if kb_id:
            for f in _kb_files(base, key, kb_id):
                existing[f["filename"]] = f
        if a.dry_run:
            for fn, (p, sha) in src.items():
                ex = existing.get(fn)
                if not ex:
                    pentry["added"] += 1
                elif ex.get("file_hash") == sha:
                    pentry["reused"] += 1
                else:
                    pentry["modified"] += 1
            if not a.no_cleanup:
                pentry["deleted"] = sum(1 for fn in existing if fn not in src)
        else:
            for fn, (p, sha) in src.items():
                meta = {"host": host, "project": encoded, "project_path": project_path,
                        "repo": repo, "account": account,
                        "source_relpath": "memory/%s" % fn}
                with open(p, "rb") as fh:
                    data = fh.read()
                ex = existing.get(fn)
                if ex and ex.get("file_hash") == sha:
                    _, err = _upload_memory_file(base, key, kb_id, sha, fn, data, meta)
                    if err:
                        pentry["failed"] += 1
                        pentry["errors"].append("%s: %s" % (fn, err))
                    else:
                        pentry["reused"] += 1
                elif ex:
                    ok, derr = _delete_file(base, key, ex["id"])
                    if not ok:
                        pentry["failed"] += 1
                        pentry["errors"].append("%s: delete old -> %s" % (fn, derr))
                        continue
                    _, err = _upload_memory_file(base, key, kb_id, sha, fn, data, meta)
                    if err:
                        pentry["failed"] += 1
                        pentry["errors"].append("%s: %s" % (fn, err))
                    else:
                        pentry["modified"] += 1
                else:
                    _, err = _upload_memory_file(base, key, kb_id, sha, fn, data, meta)
                    if err:
                        pentry["failed"] += 1
                        pentry["errors"].append("%s: %s" % (fn, err))
                    else:
                        pentry["added"] += 1
            if not a.no_cleanup:
                for fn, f in existing.items():
                    if fn in src:
                        continue
                    ok, derr = _delete_file(base, key, f["id"])
                    if ok:
                        pentry["deleted"] += 1
                    else:
                        pentry["failed"] += 1
                        pentry["errors"].append("%s: cleanup -> %s" % (fn, derr))
        for k in result["total"]:
            result["total"][k] += pentry[k]
        result["projects"].append(pentry)
        if a.wait and kb_id and (pentry["added"] or pentry["modified"]):
            touched.append((kb_id, kb_name))
    if a.wait and touched:
        deadline = _wait_deadline()
        while time.time() < deadline:
            if all(not (_kb_status(base, key, kid)["pending"]
                       or _kb_status(base, key, kid)["processing"])
                   for kid, _ in touched):
                break
            time.sleep(10)
        for kid, kname in touched:
            st = _kb_status(base, key, kid)
            result["waited"].append({"kb_name": kname, "completed": st["completed"],
                                     "pending": st["pending"],
                                     "processing": st["processing"],
                                     "failed": st["failed"]})
    print(json.dumps(result))


def cmd_retrieve_projects(base, key, a):
    me = _whoami(base, key)
    account = a.account or me.get("email", "?")
    mode = _mode(a)
    d = jget(base, key, "GET", "/api/v1/knowledge/")
    items = d.get("items", []) if isinstance(d, dict) else d
    selected = []
    for k in items:
        name = k.get("name", "")
        if not fnmatch.fnmatch(((k.get("user") or {}).get("email") or ""), account):
            continue
        tail = name.split("--", 1)[1] if "--" in name else name
        if a.host and not name.startswith(a.host + "--"):
            continue
        if a.project and a.project not in tail:
            continue
        if a.kb_glob and not fnmatch.fnmatch(name, a.kb_glob):
            continue
        repo = _parse_repo(k.get("description", "")) or (
            tail.rsplit("-", 1)[-1] if "-" in tail else tail)
        selected.append((k["id"], name, repo))
    all_hits = []
    errors = []
    score_order = "asc"  # every KB call uses the same mode; capture the first
    for kb_id, name, repo in selected:
        hits, so, err = _search_one_kb(base, key, kb_id, a.query, a.k, mode)
        if err:
            errors.append({"kb_name": name, "error": err})
            continue
        if not errors and not all_hits:
            score_order = so
        for h in hits:
            h["kb_name"] = name
            h["repo"] = repo
            all_hits.append(h)
    # Sort by score_order: hybrid/lexical return an RRF score (higher=better,
    # desc); vector returns a cosine distance (lower=better, asc). Sorting
    # ascending unconditionally reverses the hybrid ranking — this fixes that.
    reverse = score_order == "desc"
    fill = float("-inf") if reverse else float("inf")
    all_hits.sort(key=lambda h: h["distance"] if isinstance(h.get("distance"), (int, float)) else fill,
                  reverse=reverse)
    all_hits = all_hits[:a.k]
    print(json.dumps({"kbs": len(selected), "score_order": score_order,
                      "hits": all_hits, "errors": errors}))


def cmd_status_projects(base, key, a):
    me = _whoami(base, key)
    host = a.host or _short_host()
    d = jget(base, key, "GET", "/api/v1/knowledge/")
    items = d.get("items", []) if isinstance(d, dict) else d
    account = (me.get("email") or "").lower()
    mine = [k for k in items if ((k.get("user") or {}).get("email") or "").lower() == account]
    target = None
    if a.project:
        for k in mine:
            tail = k.get("name", "").split("--", 1)[1] if "--" in k.get("name", "") else k.get("name", "")
            if a.project in tail:
                target = k
                break
        if not target:
            print(json.dumps({"error": "no project KB matches --project %r" % a.project}))
            sys.exit(1)
    else:
        cwd = os.path.realpath(os.getcwd())
        root = os.path.expanduser("~/.claude/projects")
        cur = cwd
        while True:
            encoded = _encode_path(cur)
            if os.path.isdir(os.path.join(root, encoded, "memory")):
                kb_name = "%s--%s" % (host, encoded.lstrip("-"))
                target = next((k for k in mine if k.get("name") == kb_name), None)
                if target:
                    break
            parent = os.path.dirname(cur)
            if parent == cur:  # reached /
                break
            cur = parent
        if not target:
            print(json.dumps({"error": "not indexed: run index-projects "
                                        "(no project KB for cwd=%s)" % cwd, "cwd": cwd}))
            sys.exit(1)
    kb_id = target["id"]
    kb_name = target["name"]
    st = _kb_status(base, key, kb_id)
    if a.wait:
        deadline = _wait_deadline()
        while (st["pending"] or st["processing"]) and time.time() < deadline:
            time.sleep(10)
            st = _kb_status(base, key, kb_id)
    print(json.dumps({"kb_id": kb_id, "kb_name": kb_name,
                      "completed": st["completed"], "pending": st["pending"],
                      "processing": st["processing"], "failed": st["failed"],
                      "failed_files": st["failed_files"]}))


def main():
    p = argparse.ArgumentParser(
        prog="owui.py",
        description="Read-scoped Open WebUI REST wrapper (non-admin user key).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="print the key's email + role")
    sub.add_parser("kbs", help="list knowledge bases visible to this key")
    sp = sub.add_parser("kb", help="print one knowledge base metadata"); sp.add_argument("id")
    sp = sub.add_parser("search-kbs", help="search KB names"); sp.add_argument("query")

    sp = sub.add_parser("retrieve", help="retrieve documents from a KB by name or id (semantic)")
    sp.add_argument("kb", help="KB name or id"); sp.add_argument("query")
    sp.add_argument("--k", type=int, default=5)
    sp.add_argument("--mode", choices=["hybrid", "lexical", "vector"], default=None,
                    help="retrieval mode (default hybrid; lexical = pure FTS, vector = pure vector)")
    sp.add_argument("--no-hybrid", action="store_true",
                    help="deprecated alias for --mode vector (pure vector)")

    sp = sub.add_parser("rag", help="RAG chat grounded on one or more KBs (proxied by api-gateway /memory/rag)")
    sp.add_argument("question"); sp.add_argument("--kb", action="append", default=[])

    sp = sub.add_parser("file", help="print a file's extracted text content"); sp.add_argument("id")
    sp.add_argument("--raw", action="store_true",
                    help="fetch the ORIGINAL bytes (/content) instead of the extracted text")

    sp = sub.add_parser("index-projects",
                        help="index ~/.claude/projects/*/memory/*.md into OWUI KBs (one KB per project, user key)")
    sp.add_argument("--root", default="~/.claude/projects", help="projects root")
    sp.add_argument("--host", default=None, help="host segment of KB name (default $HOSTNAME short)")
    sp.add_argument("--project", default=None, help="substring filter on the encoded project dir")
    sp.add_argument("--dry-run", action="store_true", help="plan only; no writes")
    sp.add_argument("--wait", action="store_true", help="poll until the drain completes (deadline 600s)")
    sp.add_argument("--no-cleanup", action="store_true", help="do not delete KB files whose source is gone")

    sp = sub.add_parser("retrieve-projects",
                        help="retrieve across project-memory KBs (filters: --host/--project/--account/--kb-glob)")
    sp.add_argument("query")
    sp.add_argument("--host", default=None, help="filter by host segment (name starts with <host>--)")
    sp.add_argument("--project", default=None, help="substring filter on the project part of the KB name")
    sp.add_argument("--account", default=None, help="KB owner email, or fnmatch glob like '*@corp.com' / '*' for all visible (default: the caller)")
    sp.add_argument("--mine", action="store_true", help="alias for the default (account = caller)")
    sp.add_argument("--kb-glob", default=None, help="fnmatch glob on the KB name")
    sp.add_argument("--k", type=int, default=5, help="top-k hits after merge")
    sp.add_argument("--mode", choices=["hybrid", "lexical", "vector"], default=None,
                    help="retrieval mode (default hybrid; lexical = pure FTS, vector = pure vector)")
    sp.add_argument("--no-hybrid", action="store_true",
                    help="deprecated alias for --mode vector (pure vector)")

    sp = sub.add_parser("status-projects",
                        help="drain status of the current repo's project-memory KB (walks up cwd)")
    sp.add_argument("--project", default=None, help="substring match on a project KB name (overrides cwd walk-up)")
    sp.add_argument("--host", default=None, help="host segment (default $HOSTNAME short)")
    sp.add_argument("--wait", action="store_true", help="poll until pending+processing == 0")

    a = p.parse_args()
    base = base_url()
    key = api_key()

    {
        "whoami": cmd_whoami, "kbs": cmd_kbs, "kb": cmd_kb, "search-kbs": cmd_search_kbs,
        "retrieve": cmd_retrieve, "rag": cmd_rag, "file": cmd_file,
        "index-projects": cmd_index_projects, "retrieve-projects": cmd_retrieve_projects,
        "status-projects": cmd_status_projects,
    }[a.cmd](base, key, a)


if __name__ == "__main__":
    main()