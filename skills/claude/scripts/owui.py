#!/usr/bin/env python3
"""CLI wrapper for a self-hosted Open WebUI knowledge base.

Two surfaces, one key (the non-admin agent key, OPENWEBUI_USER_API_KEY):

  * KB surface (read-scoped): list/search KBs, semantic-search a KB, RAG chat
    grounded on a KB, read file content. The agent key is read-only here: it
    cannot upload, modify, or delete KBs/files it does not own.

  * Projects-memory surface (user-key writes to OWNED KBs): index
    ~/.claude/projects/<encoded>/memory/*.md into OWUI KBs (one KB per project),
    search across those KBs, and check a repo's index status. The user key
    CREATES + OWNS each project KB (KB.user.email == caller) and uploads/deletes
    files in it. OWUI gates KB creation on the workspace.knowledge permission,
    which is off by default: run `make projects-bootstrap` once (admin) to enable
    it before the first `index-projects`.

The stack is fronted by Caddy at KB_HOST: OWUI REST is at the KB_HOST root
(/api/* via Caddy catch-all -> openwebui:8080). The kb-gateway memory endpoints are
at /memory/* on the same KB_HOST. One URL, one key.

Zero dependencies (Python 3.8+ stdlib). Config resolution priority:
    CLI flags  >  environment variables  >  --env-file (a .env-style file)

Env vars: KB_HOST (or KB_HOST_PORT), KB_API_KEY
(fallback OPENWEBUI_USER_API_KEY), optional OPENWEBUI_MODEL for RAG chat,
optional PROJECTS_WAIT (seconds; --wait deadline, default 600) + HOSTNAME
(host segment of the project KB name) for the projects-memory surface.
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


def load_env_file(path):
    """Parse KEY=VALUE lines from a .env-style file into os.environ, OVERRIDING
    any inherited value (so explicit --env-file config wins over the shell env,
    and a later --env-file wins over an earlier one — matching `make api-keys`,
    which sources .env then .env.local the same way). For a quoted value, takes
    the content between the matching quotes (discarding any inline comment after
    the closing quote). For an unquoted value, drops an inline # comment. No ${}
    interpolation (values are literal)."""
    if not path or not os.path.exists(path):
        return
    with open(path) as f:
        for ln in f:
            s = ln.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip()
            if v and v[0] in ("'", '"'):
                q = v[0]
                end = v.find(q, 1)
                v = v[1:end] if end != -1 else v[1:]   # quoted: content up to closing quote
            elif "#" in v:
                v = v.split("#", 1)[0].strip()         # unquoted: drop inline comment
            os.environ[k] = v                       # override: --env-file wins over inherited shell env


def base_url(args):
    if args.base_url:
        return args.base_url.rstrip("/")
    if os.environ.get("KB_HOST"):
        return os.environ["KB_HOST"].rstrip("/")
    if os.environ.get("KB_HOST_PORT"):
        return "http://localhost:%s" % os.environ["KB_HOST_PORT"]
    sys.exit("FAIL  no KB_HOST: pass --base-url or set KB_HOST "
             "(or KB_HOST_PORT, or --env-file)")


def api_key(args):
    if args.key:
        return args.key
    # KB_API_KEY is the unified key for the stack (an Open WebUI key). Fall back
    # to OPENWEBUI_USER_API_KEY for backward compatibility with existing .env.local.
    if os.environ.get("KB_API_KEY"):
        return os.environ["KB_API_KEY"]
    if os.environ.get("OPENWEBUI_USER_API_KEY"):
        return os.environ["OPENWEBUI_USER_API_KEY"]
    sys.exit("FAIL  no API key: pass --key or set KB_API_KEY (or --env-file)")


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


def flatten_chroma(d):
    """Chroma response: {documents:[[…]], distances:[[…]], metadatas:[[…]], ids:[[…]]}
    — one inner list per collection_name. Flatten to a list of hit dicts."""
    docs = d.get("documents", [[]])
    dists = d.get("distances", [[]])
    metas = d.get("metadatas", [[]])
    ids = d.get("ids", [[]])
    out = []
    for sub_docs, sub_d, sub_m, sub_i in zip(docs, dists, metas, ids):
        for j, t in enumerate(sub_docs or []):
            m = (sub_m[j] if j < len(sub_m or []) else {}) or {}
            out.append({
                "id": sub_i[j] if j < len(sub_i or []) else "",
                "distance": sub_d[j] if j < len(sub_d or []) else None,
                "file": m.get("file_name") or m.get("name") or "",
                "text": t,
            })
    return out


# --- subcommands -------------------------------------------------------------

def cmd_whoami(base, key, a):
    d = jget(base, key, "GET", "/api/v1/auths/")
    print("%s role=%s" % (d.get("email", "?"), d.get("role", "?")))


def cmd_kbs(base, key, a):
    d = jget(base, key, "GET", "/api/v1/knowledge/")
    items = d.get("items", []) if isinstance(d, dict) else d
    if not items:
        print("(no knowledge bases visible to this key)")
        return
    for k in items:
        owner = ((k.get("user") or {}).get("email") or "-")
        print("%-38s  files=%-3s  write=%-5s  %s  owner=%s" % (
            k.get("id", ""), k.get("file_count", "?"),
            k.get("write_access"), k.get("name", ""), owner))


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
    print(json.dumps(d, indent=2))


def cmd_search_kbs(base, key, a):
    d = jget(base, key, "GET", "/api/v1/knowledge/search?query=%s" % urllib.parse.quote(a.query))
    items = d.get("items", []) if isinstance(d, dict) else d
    for k in items:
        print("%-38s  %s" % (k.get("id", ""), k.get("name", "")))


def cmd_search(base, key, a):
    body = {"collection_names": [a.kb_id], "query": a.query, "k": a.k, "hybrid": not a.no_hybrid}
    d = jget(base, key, "POST", "/api/v1/retrieval/query/collection", body)
    hits = flatten_chroma(d)
    print("hits: %d" % len(hits))
    for i, h in enumerate(hits):
        dist = h["distance"]
        dist_s = ("%.4f" % dist) if isinstance(dist, (int, float)) else "?"
        snip = (h["text"] or "").replace("\n", " ")[:200]
        print("[%d] dist=%s file=%s" % (i, dist_s, h["file"] or "?"))
        print("    %s" % snip)


def cmd_rag(base, key, a):
    model = a.model or os.environ.get("OPENWEBUI_MODEL") or os.environ.get("MODEL_NAME") or "gemma4:12b"
    body = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": a.question}],
    }
    if a.kb:
        # Ground the chat on KB(s). Open WebUI's /api/chat/completions reads
        # KBs from the top-level `files` field as collection items — NOT from a
        # `knowledge` field (ignored) or `metadata.knowledge` (metadata is
        # discarded and replaced server-side). type:collection -> whole-KB
        # vector search; type:file would scope to one file id.
        body["files"] = [{"type": "collection", "id": kid} for kid in a.kb]
    d = jget(base, key, "POST", "/api/chat/completions", body)
    content = (d.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    print(content or "(empty response)")


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
    # Fetch directly (not via call()) so we keep the Content-Type and can handle
    # a binary body. The /content endpoint returns the RAW file for binary
    # formats (PDF/DOCX/PPTX/XLSX/images), not the extracted text — so the old
    # call() path (r.read().decode()) raised UnicodeDecodeError on every PDF.
    # Text files decode and print; binary files are saved to a temp file with a
    # note pointing the caller at an extractor. The extracted (searchable) text
    # for a binary file is also available via `search <kb> "<query>"`.
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
        sys.exit("NOTE  file %s is binary (Content-Type: %s, %d bytes); the /content\n"
                 "  endpoint returns the RAW file, not extracted text. Saved to: %s\n"
                 "  PDF -> pdftotext -layout %s -  |  Office/image -> `search <kb> \"<query>\"`\n"
                 "  for the extracted text chunks, or open the saved file."
                 % (a.id, ctype or "?", len(raw), path, path))
    sys.stdout.write(txt if not txt.endswith("\n") else txt)


# --- projects-memory surface (user-key writes to OWNED KBs) ------------------
# Source: ~/.claude/projects/<encoded-dir>/memory/*.md (Claude auto-memory). The
# encoded dir is the absolute path with '/' and '.' -> '-' (leading '/' -> '-');
# decoding is lossy, so the exact encoded dir is the authoritative project key.

def _short_host():
    # HOSTNAME is not exported in non-interactive shells; platform.node() is.
    return (os.environ.get("HOSTNAME") or platform.node() or "unknown").split(".")[0]


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


def _search_one_kb(base, key, kb_id, query, k, hybrid):
    """POST /api/v1/retrieval/query/collection with a SINGLE collection_name so
    every hit is attributable to this KB (hit metadata carries no knowledge_id;
    one call per KB is the reliable attribution). Returns (hits, err)."""
    body = {"collection_names": [kb_id], "query": query, "k": k, "hybrid": hybrid}
    code, txt = call(base, key, "POST", "/api/v1/retrieval/query/collection", body)
    if code != 200:
        return [], "HTTP %s: %s" % (code, (txt or "")[:200])
    hits = flatten_chroma(json.loads(txt))
    for h in hits:
        h["kb_id"] = kb_id
    return hits, None


def _parse_repo(desc):
    """Extract repo=<x> from a KB description (set by index-projects)."""
    for tok in (desc or "").split("|"):
        tok = tok.strip()
        if tok.startswith("repo="):
            return tok[len("repo="):].strip()
    return None


def _wait_deadline():
    return time.time() + int(os.environ.get("PROJECTS_WAIT", "600"))


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
    if not projects:
        print("(no projects with memory/ found under %s)" % root)
        return
    d = jget(base, key, "GET", "/api/v1/knowledge/")
    items = d.get("items", []) if isinstance(d, dict) else d
    kb_by_name = {k.get("name", ""): k for k in items}
    agg = {"added": 0, "modified": 0, "reused": 0, "deleted": 0, "failed": 0}
    touched = []  # (kb_id, kb_name) with uploads, for --wait
    for encoded, mem in projects:
        project_path = _decode_project_path(encoded)
        repo = _repo_name(encoded, project_path)
        kb_name = "%s--%s" % (host, encoded.lstrip("-"))
        kb = kb_by_name.get(kb_name)
        kb_id = kb["id"] if kb else None
        created = "exists" if kb else ("would-create" if a.dry_run else "created")
        if not a.dry_run and not kb:
            desc = ("Claude projects memory | repo=%s | host=%s | project=%s | path=%s"
                    % (repo, host, encoded, project_path))
            code, txt = call(base, key, "POST", "/api/v1/knowledge/create",
                             {"name": kb_name, "description": desc})
            if code != 200:
                print("✗ %s  create -> HTTP %s: %s" % (kb_name, code, (txt or "")[:200]))
                agg["failed"] += 1
                continue
            kb_id = json.loads(txt).get("id")
        src = {}
        for fn in sorted(os.listdir(mem)):
            p = os.path.join(mem, fn)
            if os.path.isfile(p) and fn.endswith(".md"):
                src[fn] = (p, _sha256_file(p))
        existing = {}
        if kb_id:
            for f in _kb_files(base, key, kb_id):
                existing[f["filename"]] = f
        pc = {"added": 0, "modified": 0, "reused": 0, "deleted": 0, "failed": 0}
        if a.dry_run:
            for fn, (p, sha) in src.items():
                ex = existing.get(fn)
                if not ex:
                    pc["added"] += 1
                elif ex.get("file_hash") == sha:
                    pc["reused"] += 1
                else:
                    pc["modified"] += 1
            if not a.no_cleanup:
                pc["deleted"] = sum(1 for fn in existing if fn not in src)
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
                        pc["failed"] += 1; print("  ✗ %s: %s" % (fn, err))
                    else:
                        pc["reused"] += 1
                elif ex:
                    ok, derr = _delete_file(base, key, ex["id"])
                    if not ok:
                        pc["failed"] += 1; print("  ✗ %s: delete old -> %s" % (fn, derr))
                        continue
                    _, err = _upload_memory_file(base, key, kb_id, sha, fn, data, meta)
                    if err:
                        pc["failed"] += 1; print("  ✗ %s: %s" % (fn, err))
                    else:
                        pc["modified"] += 1
                else:
                    _, err = _upload_memory_file(base, key, kb_id, sha, fn, data, meta)
                    if err:
                        pc["failed"] += 1; print("  ✗ %s: %s" % (fn, err))
                    else:
                        pc["added"] += 1
            if not a.no_cleanup:
                for fn, f in existing.items():
                    if fn in src:
                        continue
                    ok, derr = _delete_file(base, key, f["id"])
                    if ok:
                        pc["deleted"] += 1
                    else:
                        pc["failed"] += 1; print("  ✗ %s: cleanup -> %s" % (fn, derr))
        print("✓ %s  %s  added=%d modified=%d reused=%d deleted=%d failed=%d  repo=%s"
              % (kb_name, created, pc["added"], pc["modified"], pc["reused"],
                 pc["deleted"], pc["failed"], repo))
        for k in agg:
            agg[k] += pc[k]
        if a.wait and kb_id and (pc["added"] or pc["modified"]):
            touched.append((kb_id, kb_name))
    if a.wait and touched:
        deadline = _wait_deadline()
        print("○ waiting for drain (deadline %ss)..." % int(os.environ.get("PROJECTS_WAIT", "600")))
        while time.time() < deadline:
            if all(not (_kb_status(base, key, kid)["pending"] or _kb_status(base, key, kid)["processing"])
                   for kid, _ in touched):
                break
            time.sleep(10)
        for kid, kname in touched:
            st = _kb_status(base, key, kid)
            print("  ○ %s  completed=%d pending=%d processing=%d failed=%d"
                  % (kname, st["completed"], st["pending"], st["processing"], st["failed"]))
    print("\nTOTAL  added=%d modified=%d reused=%d deleted=%d failed=%d  (projects=%d)"
          % (agg["added"], agg["modified"], agg["reused"], agg["deleted"], agg["failed"], len(projects)))


def cmd_search_projects(base, key, a):
    me = _whoami(base, key)
    account = a.account or me.get("email", "?")
    d = jget(base, key, "GET", "/api/v1/knowledge/")
    items = d.get("items", []) if isinstance(d, dict) else d
    selected = []
    for k in items:
        name = k.get("name", "")
        if ((k.get("user") or {}).get("email") or "") != account:
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
    if not selected:
        print("(no project KBs match the filters for account=%s)" % account)
        return
    all_hits = []
    for kb_id, name, repo in selected:
        hits, err = _search_one_kb(base, key, kb_id, a.query, a.k, not a.no_hybrid)
        if err:
            print("✗ %s: %s" % (name, err))
            continue
        for h in hits:
            h["kb_name"] = name
            h["repo"] = repo
            all_hits.append(h)
    all_hits.sort(key=lambda h: h["distance"] if isinstance(h.get("distance"), (int, float)) else 1.0)
    all_hits = all_hits[:a.k]
    print("KBs: %d | hits: %d" % (len(selected), len(all_hits)))
    for i, h in enumerate(all_hits):
        dist = h["distance"]
        dist_s = ("%.4f" % dist) if isinstance(dist, (int, float)) else "?"
        snip = (h["text"] or "").replace("\n", " ")[:200]
        print("[%d] dist=%s repo=%s kb=%s file=%s"
              % (i, dist_s, h["repo"], h["kb_name"], h["file"] or "?"))
        print("    %s" % snip)


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
            print("✗ no project KB matches --project %r" % a.project)
            return
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
            print("✗ not indexed: run `index-projects` (no project KB for cwd=%s)" % cwd)
            return
    kb_id = target["id"]
    kb_name = target["name"]
    st = _kb_status(base, key, kb_id)
    if a.wait:
        deadline = _wait_deadline()
        while (st["pending"] or st["processing"]) and time.time() < deadline:
            time.sleep(10)
            st = _kb_status(base, key, kb_id)
    if a.json:
        print(json.dumps({"kb_id": kb_id, "kb_name": kb_name,
                           "completed": st["completed"], "pending": st["pending"],
                           "processing": st["processing"], "failed": st["failed"],
                           "failed_files": st["failed_files"]}, indent=2))
        return
    print("✓ %s  completed=%d pending=%d processing=%d failed=%d"
          % (kb_name, st["completed"], st["pending"], st["processing"], st["failed"]))
    for f in st["failed_files"]:
        print("  ✗ %s — %s" % (f["filename"], (f["error"] or "")[:80]))


def main():
    p = argparse.ArgumentParser(
        prog="owui.py",
        description="Read-scoped Open WebUI REST wrapper (non-admin agent key).",
    )
    p.add_argument("--base-url", help="KB_HOST URL (e.g. http://localhost:3000)")
    p.add_argument("--key", help="API key (KB_API_KEY)")
    p.add_argument("--env-file", action="append", default=[],
                   help=".env-style file to load KB_HOST + key from (repeatable; e.g. .env then .env.local)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="print the key's email + role")
    sub.add_parser("kbs", help="list knowledge bases visible to this key")
    sp = sub.add_parser("kb", help="print one knowledge base metadata"); sp.add_argument("id")
    sp = sub.add_parser("search-kbs", help="search KB names"); sp.add_argument("query")

    sp = sub.add_parser("search", help="semantic-search documents in a KB")
    sp.add_argument("kb_id"); sp.add_argument("query")
    sp.add_argument("--k", type=int, default=4); sp.add_argument("--no-hybrid", action="store_true")

    sp = sub.add_parser("rag", help="RAG chat grounded on one or more KBs")
    sp.add_argument("question"); sp.add_argument("--kb", action="append", default=[])
    sp.add_argument("--model", help="chat model (default OPENWEBUI_MODEL or MODEL_NAME or gemma4:12b)")

    sp = sub.add_parser("file", help="print a file's text content"); sp.add_argument("id")

    sp = sub.add_parser("index-projects",
                        help="index ~/.claude/projects/*/memory/*.md into OWUI KBs (one KB per project, user key)")
    sp.add_argument("--root", default="~/.claude/projects", help="projects root")
    sp.add_argument("--host", default=None, help="host segment of KB name (default $HOSTNAME short)")
    sp.add_argument("--project", default=None, help="substring filter on the encoded project dir")
    sp.add_argument("--dry-run", action="store_true", help="plan only; no writes")
    sp.add_argument("--wait", action="store_true", help="poll until the drain completes (deadline PROJECTS_WAIT, default 600s)")
    sp.add_argument("--no-cleanup", action="store_true", help="do not delete KB files whose source is gone")

    sp = sub.add_parser("search-projects",
                        help="search across project-memory KBs (filters: --host/--project/--account/--kb-glob)")
    sp.add_argument("query")
    sp.add_argument("--host", default=None, help="filter by host segment (name starts with <host>--)")
    sp.add_argument("--project", default=None, help="substring filter on the project part of the KB name")
    sp.add_argument("--account", default=None, help="KB owner email (default: the caller)")
    sp.add_argument("--mine", action="store_true", help="alias for the default (account = caller)")
    sp.add_argument("--kb-glob", default=None, help="fnmatch glob on the KB name")
    sp.add_argument("--k", type=int, default=4, help="top-k hits after merge")
    sp.add_argument("--no-hybrid", action="store_true", help="pure vector search (no hybrid)")

    sp = sub.add_parser("status-projects",
                        help="drain status of the current repo's project-memory KB (walks up cwd)")
    sp.add_argument("--project", default=None, help="substring match on a project KB name (overrides cwd walk-up)")
    sp.add_argument("--host", default=None, help="host segment (default $HOSTNAME short)")
    sp.add_argument("--json", action="store_true", help="print the status dict as JSON")
    sp.add_argument("--wait", action="store_true", help="poll until pending+processing == 0")

    a = p.parse_args()
    for ef in a.env_file:
        load_env_file(ef)
    base = base_url(a)
    key = api_key(a)

    {
        "whoami": cmd_whoami, "kbs": cmd_kbs, "kb": cmd_kb, "search-kbs": cmd_search_kbs,
        "search": cmd_search, "rag": cmd_rag, "file": cmd_file,
        "index-projects": cmd_index_projects, "search-projects": cmd_search_projects,
        "status-projects": cmd_status_projects,
    }[a.cmd](base, key, a)


if __name__ == "__main__":
    main()