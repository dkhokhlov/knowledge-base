#!/usr/bin/env python3
"""Read-scoped CLI wrapper for a self-hosted Open WebUI knowledge base.

Authenticates with the non-admin agent API key (OPENWEBUI_USER_API_KEY), which is
read-only: it can list/search knowledge bases, semantic-search a KB, and do RAG
chat grounded on a KB. It cannot upload, modify, or delete KBs or files. For the
full admin API surface, browse /openapi.json or /api/docs with the admin key.

The stack is fronted by Caddy at KB_HOST: OWUI REST is at the KB_HOST root
(/api/* via Caddy catch-all -> openwebui:8080). The kb-gateway memory endpoints are
at /memory/* on the same KB_HOST. One URL, one key.

Zero dependencies (Python 3.8+ stdlib). Config resolution priority:
    CLI flags  >  environment variables  >  --env-file (a .env.local)

Env vars: KB_HOST (or KB_HOST_PORT), KB_API_KEY
(fallback OPENWEBUI_USER_API_KEY), optional OPENWEBUI_MODEL for RAG chat.
"""
import argparse
import json
import os
import sys
import tempfile
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
        print("%-38s  files=%-3s  write=%-5s  %s" % (
            k.get("id", ""), k.get("file_count", "?"),
            k.get("write_access"), k.get("name", "")))


def cmd_kb(base, key, a):
    d = jget(base, key, "GET", "/api/v1/knowledge/%s" % a.id)
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

    a = p.parse_args()
    for ef in a.env_file:
        load_env_file(ef)
    base = base_url(a)
    key = api_key(a)

    {
        "whoami": cmd_whoami, "kbs": cmd_kbs, "kb": cmd_kb, "search-kbs": cmd_search_kbs,
        "search": cmd_search, "rag": cmd_rag, "file": cmd_file,
    }[a.cmd](base, key, a)


if __name__ == "__main__":
    main()