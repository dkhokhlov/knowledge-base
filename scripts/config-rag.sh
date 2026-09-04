#!/usr/bin/env bash
# Set the Open WebUI RAG config from this repo (the same template for every
# environment -- live main stack and e2e iso clones alike):
#   - RAG_TEMPLATE: strict grounding -- answer only from the retrieved context;
#     refuse ("The indexed documents do not contain this information.") when
#     the answer is not in the context; do not use outside knowledge; do not
#     invent names, terms, file names, or artifacts.
#   - CHUNK_MIN_SIZE_TARGET (from .env, the single source -- .env.template):
#     activates _coalesce_spans (patch 5) -- spans under that many chars merge
#     forward into the next while the combined span fits in CHUNK_SIZE. The
#     image default is 0 (header-strict, no coalescing). .env seeds the
#     persistent config at FIRST BOOT only (webui.db wins over env afterwards);
#     this script re-asserts the same value over the DB. No literal default
#     here -- the value is declared in .env.template and this fails loudly if
#     it is missing (same discipline as compose ${VAR:?}).
#   - CHUNK_SIZE (chunk ceiling) and TOP_K (RAG-chat retrieval top-k; a
#     per-request k on /retrieval/query/collection overrides it): same .env
#     single source + strict read + re-assert-over-DB discipline as
#     CHUNK_MIN_SIZE_TARGET.
#   - ENABLE_RAG_HYBRID_SEARCH, RAG_HYBRID_BM25_WEIGHT, RAG_TOP_K_RERANKER (the
#     hybrid-retrieval master switch, the BM25/vector blend, and the reranker
#     candidate cap): same .env single source + strict read + re-assert-over-DB
#     discipline. OWUI's /retrieval/config key drops the RAG_ prefix for the
#     latter two (HYBRID_BM25_WEIGHT, TOP_K_RERANKER); this script reads the
#     .env (RAG_-prefixed) names and POSTs the unprefixed keys.
#
# Idempotent: re-running just re-asserts the same values.
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
import os, json, re, urllib.request, urllib.error, sys

# OWUI is fronted by Caddy at the KB_HOST root; reach its /api/* there.
_kb_host = os.environ.get("KB_HOST")
if not _kb_host:
    sys.exit("FAIL  KB_HOST not set -- export KB_HOST=http://<host>:<port> (see .env.template)")
O = _kb_host.rstrip("/")
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

_min = os.environ.get("CHUNK_MIN_SIZE_TARGET")
if not _min:
    sys.exit("FAIL  CHUNK_MIN_SIZE_TARGET not set -- declare it in .env.template")
MIN_SIZE = int(_min)
_sz = os.environ.get("CHUNK_SIZE")
if not _sz:
    sys.exit("FAIL  CHUNK_SIZE not set -- declare it in .env.template")
CHUNK_SZ = int(_sz)
_tk = os.environ.get("TOP_K")
if not _tk:
    sys.exit("FAIL  TOP_K not set -- declare it in .env.template")
TOP_K = int(_tk)
_emb_model = os.environ.get("RAG_EMBEDDING_MODEL")
if not _emb_model:
    sys.exit("FAIL  RAG_EMBEDDING_MODEL not set -- declare it in .env.template")
_hy = os.environ.get("ENABLE_RAG_HYBRID_SEARCH")
if not _hy:
    sys.exit("FAIL  ENABLE_RAG_HYBRID_SEARCH not set -- declare it in .env.template")
HYBRID = _hy.strip().lower() in ("1", "true", "yes", "on")
_bw = os.environ.get("RAG_HYBRID_BM25_WEIGHT")
if not _bw:
    sys.exit("FAIL  RAG_HYBRID_BM25_WEIGHT not set -- declare it in .env.template")
BM25_W = float(_bw)
_tkr = os.environ.get("RAG_TOP_K_RERANKER")
if not _tkr:
    sys.exit("FAIL  RAG_TOP_K_RERANKER not set -- declare it in .env.template")
TOP_K_RR = int(_tkr)
st, txt = call("POST", "/api/v1/retrieval/config/update",
               {"RAG_TEMPLATE": NEW, "CHUNK_MIN_SIZE_TARGET": MIN_SIZE,
                "CHUNK_SIZE": CHUNK_SZ, "TOP_K": TOP_K,
                "ENABLE_RAG_HYBRID_SEARCH": HYBRID,
                "HYBRID_BM25_WEIGHT": BM25_W,
                "TOP_K_RERANKER": TOP_K_RR})
if st != 200:
    sys.exit("FAIL  update RAG config -> HTTP %s: %s"
             % (st, txt[:200]))

st, txt = call("GET", "/api/v1/retrieval/config")
d = parse_json(txt, "GET /api/v1/retrieval/config")
if d.get("RAG_TEMPLATE") != NEW:
    sys.exit("FAIL  RAG_TEMPLATE did not stick")
if d.get("CHUNK_MIN_SIZE_TARGET") != MIN_SIZE:
    sys.exit("FAIL  CHUNK_MIN_SIZE_TARGET did not stick (got %s, want %s)"
             % (d.get("CHUNK_MIN_SIZE_TARGET"), MIN_SIZE))
if d.get("CHUNK_SIZE") != CHUNK_SZ:
    sys.exit("FAIL  CHUNK_SIZE did not stick (got %s, want %s)"
             % (d.get("CHUNK_SIZE"), CHUNK_SZ))
if d.get("TOP_K") != TOP_K:
    sys.exit("FAIL  TOP_K did not stick (got %s, want %s)"
             % (d.get("TOP_K"), TOP_K))
if d.get("ENABLE_RAG_HYBRID_SEARCH") != HYBRID:
    sys.exit("FAIL  ENABLE_RAG_HYBRID_SEARCH did not stick (got %s, want %s)"
             % (d.get("ENABLE_RAG_HYBRID_SEARCH"), HYBRID))
if d.get("HYBRID_BM25_WEIGHT") != BM25_W:
    sys.exit("FAIL  HYBRID_BM25_WEIGHT did not stick (got %s, want %s)"
             % (d.get("HYBRID_BM25_WEIGHT"), BM25_W))
if d.get("TOP_K_RERANKER") != TOP_K_RR:
    sys.exit("FAIL  TOP_K_RERANKER did not stick (got %s, want %s)"
             % (d.get("TOP_K_RERANKER"), TOP_K_RR))
print("OK    strict-grounding RAG_TEMPLATE set (len=%d)" % len(d["RAG_TEMPLATE"]))
print("      merge sanity: TOP_K=%s CHUNK_SIZE=%s CHUNK_MIN_SIZE_TARGET=%s"
      % (d.get("TOP_K"), d.get("CHUNK_SIZE"), d.get("CHUNK_MIN_SIZE_TARGET")))
print("      hybrid: ENABLE_RAG_HYBRID_SEARCH=%s HYBRID_BM25_WEIGHT=%s TOP_K_RERANKER=%s"
      % (d.get("ENABLE_RAG_HYBRID_SEARCH"), d.get("HYBRID_BM25_WEIGHT"), d.get("TOP_K_RERANKER")))

# --- sync rag.ollama.base_url + assert embed-concurrency herd bound ------------
# Open WebUI persists the retrieval config in webui.db on first boot and ignores
# later env changes. Two fields drift this way and both break the embed path:
#   1. rag.ollama.base_url -- the embedder points at a stale host (chat works,
#      file upload fails, RAG search returns 0 hits).
#   2. RAG_EMBEDDING_BATCH_SIZE / RAG_EMBEDDING_CONCURRENT_REQUESTS -- OWUI defaults
#      BATCH=1 and CONCURRENT=0 (unlimited). A 5195-chunk file then fires 5195
#      concurrent /api/embed calls and kills the Ollama nomic runner (HTTP 400
#      /tokenize EOF). .env.template pins 32/4; assert them here so an existing
#      webui.db gets the bound, not only a fresh one (env seeds a fresh DB).
# Reconcile via the embedding API: GET the current config, POST it back with the
# env values. /embedding/update REPLACES the whole config, so we preserve
# engine/model/key from the GET and set url + the 3 concurrency keys from .env.
# Idempotent (POST only when a tracked field differs).
_ollama_host = os.environ.get("OLLAMA_HOST")
if not _ollama_host:
    sys.exit("FAIL  OLLAMA_HOST not set -- export it or uncomment in .env (see .env.template)")
OLLAMA_URL = _ollama_host.rstrip("/")
# OWUI uses this URL INSIDE its container, where localhost is the container's
# own loopback (no Ollama). Apply the same localhost->host.docker.internal
# translation the container entrypoint shim (scripts/ollama-host.sh) and
# preflight apply, so OLLAMA_HOST=http://localhost:11434 (the shell convention)
# writes http://host.docker.internal:11434 to the DB.
OLLAMA_URL = re.sub(r'(https?://)(localhost|127\.0\.0\.1)([:/]|$)', r'\1host.docker.internal\3', OLLAMA_URL)

# Embed-concurrency herd bound -- .env single source (no literal defaults).
_ebs = os.environ.get("RAG_EMBEDDING_BATCH_SIZE")
if not _ebs:
    sys.exit("FAIL  RAG_EMBEDDING_BATCH_SIZE not set -- declare it in .env.template")
EBS = int(_ebs)
if EBS < 1:
    sys.exit("FAIL  RAG_EMBEDDING_BATCH_SIZE=%s -- must be >= 1 (1 = one request per chunk, herd)" % _ebs)
_ecr = os.environ.get("RAG_EMBEDDING_CONCURRENT_REQUESTS")
if not _ecr:
    sys.exit("FAIL  RAG_EMBEDDING_CONCURRENT_REQUESTS not set -- declare it in .env.template")
ECR = int(_ecr)
if ECR < 1:
    sys.exit("FAIL  RAG_EMBEDDING_CONCURRENT_REQUESTS=%s -- must be >= 1 (0 = unlimited thundering herd)" % _ecr)
_ase = os.environ.get("ENABLE_ASYNC_EMBEDDING")
if not _ase:
    sys.exit("FAIL  ENABLE_ASYNC_EMBEDDING not set -- declare it in .env.template")
ASE = _ase.strip().lower() in ("1", "true", "yes", "on")

st, txt = call("GET", "/api/v1/retrieval/embedding")
if st != 200:
    sys.exit("FAIL  get embedding config -> HTTP %s: %s" % (st, txt[:200]))
emb = parse_json(txt, "GET /api/v1/retrieval/embedding")
oc = emb.get("ollama_config") or {}
cur_url = (oc.get("url") or "").rstrip("/")
cur_ebs = int(emb.get("RAG_EMBEDDING_BATCH_SIZE") or 0)
cur_ecr = int(emb.get("RAG_EMBEDDING_CONCURRENT_REQUESTS") or 0)
cur_ase = bool(emb.get("ENABLE_ASYNC_EMBEDDING"))
url_sync = cur_url == OLLAMA_URL
keys_sync = (cur_ebs == EBS and cur_ecr == ECR and cur_ase == ASE)
if url_sync and keys_sync:
    print("OK    rag.ollama.base_url in sync (%s); embed herd bound in sync (BATCH=%s CONCURRENT=%s ASYNC=%s)"
          % (OLLAMA_URL, cur_ebs, cur_ecr, cur_ase))
else:
    payload = {
        "RAG_EMBEDDING_ENGINE": emb.get("RAG_EMBEDDING_ENGINE", "ollama"),
        "RAG_EMBEDDING_MODEL": emb.get("RAG_EMBEDDING_MODEL", _emb_model),
        "RAG_EMBEDDING_BATCH_SIZE": EBS,
        "ENABLE_ASYNC_EMBEDDING": ASE,
        "RAG_EMBEDDING_CONCURRENT_REQUESTS": ECR,
        "ollama_config": {"url": OLLAMA_URL, "key": oc.get("key", "")},
    }
    st, txt = call("POST", "/api/v1/retrieval/embedding/update", payload)
    if st != 200:
        sys.exit("FAIL  update embedding config -> HTTP %s: %s" % (st, txt[:200]))
    st, txt = call("GET", "/api/v1/retrieval/embedding")
    chk = parse_json(txt, "GET /api/v1/retrieval/embedding")
    new_url = ((chk.get("ollama_config") or {}).get("url") or "").rstrip("/")
    new_ebs = int(chk.get("RAG_EMBEDDING_BATCH_SIZE") or 0)
    new_ecr = int(chk.get("RAG_EMBEDDING_CONCURRENT_REQUESTS") or 0)
    new_ase = bool(chk.get("ENABLE_ASYNC_EMBEDDING"))
    if new_url != OLLAMA_URL:
        sys.exit("FAIL  rag.ollama.base_url did not sync: got %s expected %s" % (new_url, OLLAMA_URL))
    if not (new_ebs == EBS and new_ecr == ECR and new_ase == ASE):
        sys.exit("FAIL  embed herd bound did not sync: got BATCH=%s CONCURRENT=%s ASYNC=%s, want BATCH=%s CONCURRENT=%s ASYNC=%s"
                 % (new_ebs, new_ecr, new_ase, EBS, ECR, ASE))
    print("OK    synced embedding config: base_url %s->%s, herd bound BATCH %s->%s CONCURRENT %s->%s ASYNC %s->%s"
          % (cur_url or "<unset>", new_url, cur_ebs, new_ebs, cur_ecr, new_ecr, cur_ase, new_ase))
PY