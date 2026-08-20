#!/usr/bin/env bash
# Set the Open WebUI RAG template to a strict-grounding version: answer only
# from the retrieved context; refuse ("The indexed documents do not contain
# this information.") when the answer is not in the context; do not use outside
# knowledge; do not invent names, terms, file names, or artifacts.
#
# Idempotent: re-running just re-asserts the same template.
#
# Preconditions:
#   - Stack running and healthy (`make start`).
#   - OPENWEBUI_ADMIN_API_KEY in .env.local (provisioned by `make api-keys`).
#
# Why this exists: the default RAG_TEMPLATE tells the model to fall back to its
# own knowledge when the answer is not in the context, which makes ~12B models
# confabulate plausible-but-wrong details (e.g. wrong vendor, invented file
# names). The strict template removes that license. Grounding itself (injecting
# the KB chunks) is done by the caller passing files:[{type:collection,id:<kb>}]
# to /api/chat/completions; this template governs what the model does with them.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck source=/dev/null
. ./.env
# shellcheck source=/dev/null
. ./.env.local
set +a

python3 - <<'PY'
import os, json, urllib.request, urllib.error, sys

# OWUI is fronted by Caddy at the KB_HOST root; reach its /api/* there.
O = os.environ.get("KB_HOST") or ("http://localhost:%s" % os.environ.get("KB_HOST_PORT", "3000"))
AK = os.environ.get("OPENWEBUI_ADMIN_API_KEY", "")
if not AK:
    sys.exit("FAIL  OPENWEBUI_ADMIN_API_KEY not set in .env.local (run: make api-keys)")

H = {"Authorization": "Bearer " + AK, "Content-Type": "application/json"}

NEW = """### Task:
Respond to the user query using ONLY the provided context. Do not use outside knowledge.

### Grounding rules:
- Answer only from the text inside <context>. If the answer is not present in the context, reply exactly: "The indexed documents do not contain this information." Do not guess, and do not use your own knowledge.
- Do not invent names, terms, file names, artifact names, formats, or steps that do not appear in the context.
- If the context is unreadable or of poor quality, say so and answer only from the legible parts.
- Respond in the same language as the user query.

### Citations:
- Include inline citations as [id] ONLY when a <source> tag has an explicit id attribute (for example, <source id="1">).
- Do not cite when the <source> tag has no id attribute.
- Do not use XML tags in your response.
- Keep citations concise and tied to the stated information.

### Output:
Give a clear, direct answer to the user query, grounded only in the context, with inline citations [id] only when a <source> id attribute is present.

<context>
{{CONTEXT}}
</context>
"""

REQUEST_TIMEOUT = 15

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(O + path, data=data, headers=H, method=method)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except urllib.error.URLError as e:
        # Transport error (connection refused, timeout, DNS). Return a non-200
        # code so callers fail cleanly instead of raising past a partial update.
        return 0, "URLError: %s" % e

def parse_json(text, label):
    try:
        return json.loads(text)
    except (TypeError, ValueError) as e:
        sys.exit("FAIL  %s returned invalid JSON: %s" % (label, e))

st, txt = call("POST", "/api/v1/retrieval/config/update", {"RAG_TEMPLATE": NEW})
if st != 200:
    sys.exit("FAIL  update RAG_TEMPLATE -> HTTP %s: %s" % (st, txt[:200]))

st, txt = call("GET", "/api/v1/retrieval/config")
d = parse_json(txt, "GET /api/v1/retrieval/config")
if d.get("RAG_TEMPLATE") != NEW:
    sys.exit("FAIL  RAG_TEMPLATE did not stick")
print("OK    strict-grounding RAG_TEMPLATE set (len=%d)" % len(d["RAG_TEMPLATE"]))
print("      merge sanity: TOP_K=%s CHUNK_SIZE=%s" % (d.get("TOP_K"), d.get("CHUNK_SIZE")))

# --- sync rag.ollama.base_url to OLLAMA_HOST ---------------------------------
# Open WebUI persists rag.ollama.base_url in webui.db on first boot and ignores
# later OLLAMA_HOST changes, so the embedder can drift to a stale host while chat
# works (file upload then "process/status=failed", RAG search returns 0 hits). Reconcile
# via the embedding API: GET the current config, change only the ollama URL, POST
# it back. /embedding/update REPLACES the whole config, so we preserve
# engine/model/batch/async/concurrent/key from the GET. Idempotent.
OLLAMA_URL = (os.environ.get("OLLAMA_HOST") or "http://host.docker.internal:11434").rstrip("/")

st, txt = call("GET", "/api/v1/retrieval/embedding")
if st != 200:
    sys.exit("FAIL  get embedding config -> HTTP %s: %s" % (st, txt[:200]))
emb = parse_json(txt, "GET /api/v1/retrieval/embedding")
oc = emb.get("ollama_config") or {}
cur_url = (oc.get("url") or "").rstrip("/")
if cur_url == OLLAMA_URL:
    print("OK    rag.ollama.base_url already in sync: %s" % OLLAMA_URL)
else:
    payload = {
        "RAG_EMBEDDING_ENGINE": emb.get("RAG_EMBEDDING_ENGINE", "ollama"),
        "RAG_EMBEDDING_MODEL": emb.get("RAG_EMBEDDING_MODEL", os.environ.get("RAG_EMBEDDING_MODEL", "nomic-embed-text")),
        "RAG_EMBEDDING_BATCH_SIZE": emb.get("RAG_EMBEDDING_BATCH_SIZE", 1),
        "ENABLE_ASYNC_EMBEDDING": emb.get("ENABLE_ASYNC_EMBEDDING", True),
        "RAG_EMBEDDING_CONCURRENT_REQUESTS": emb.get("RAG_EMBEDDING_CONCURRENT_REQUESTS", 0),
        "ollama_config": {"url": OLLAMA_URL, "key": oc.get("key", "")},
    }
    st, txt = call("POST", "/api/v1/retrieval/embedding/update", payload)
    if st != 200:
        sys.exit("FAIL  update rag.ollama.base_url -> HTTP %s: %s" % (st, txt[:200]))
    st, txt = call("GET", "/api/v1/retrieval/embedding")
    new_url = ((parse_json(txt, "GET /api/v1/retrieval/embedding").get("ollama_config") or {}).get("url") or "").rstrip("/")
    if new_url != OLLAMA_URL:
        sys.exit("FAIL  rag.ollama.base_url did not sync: got %s expected %s" % (new_url, OLLAMA_URL))
    print("OK    synced rag.ollama.base_url: %s -> %s" % (cur_url or "<unset>", OLLAMA_URL))
PY