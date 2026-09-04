#!/usr/bin/env bash
# Point Open WebUI's retrieval/extraction at the markitdown-ocr external engine
# (CONTENT_EXTRACTION_ENGINE=external + URL + a non-empty API key) by setting
# the keys in the OWUI DB via the admin retrieval-config API (merge semantics:
# only the posted keys change). Read-back asserts each key stuck.
#
# Enable-only. Auto-run by `make api-keys` when OCR_ENABLED=true (the one
# post-start DB step, folded into the standard chain); re-assert manually with
# `make config-ocr` (e.g. after a DB reset). No-op when OCR_ENABLED!=true.
#
# Preconditions:
#   - Stack running and healthy (`make start`).
#   - markitdown-ocr service up (started by `make start` via COMPOSE_PROFILES=ocr in .env).
#   - OPENWEBUI_ADMIN_API_KEY + OCR_SERVICE_TOKEN in .env.local.
#
# Why this exists: OWUI's external extraction engine is global + all-or-nothing.
# When CONTENT_EXTRACTION_ENGINE=external + URL + a NON-EMPTY API key are set,
# OWUI routes EVERY ingest to markitdown-ocr (no per-type fallback; an empty
# result orphans). An empty API key makes OWUI silently skip the external engine
# and fall through to its default loaders, so the key MUST be non-empty.
set -euo pipefail
cd "$(dirname "$0")/.."

# Capture a `make config-ocr OCR_ENABLED=<val>` override before sourcing .env
# (which would clobber it).
_OCR_ENABLED_OVR="${OCR_ENABLED:-}"
set -a
# shellcheck source=/dev/null
. ./.env
# shellcheck source=/dev/null
. ./.env.local
set +a
if [ -n "$_OCR_ENABLED_OVR" ]; then export OCR_ENABLED="$_OCR_ENABLED_OVR"; fi

# No-op when OCR is disabled (unset defaults to enabled per the .env contract).
if [ "${OCR_ENABLED:-true}" != "true" ]; then
  echo "OCR_ENABLED=${OCR_ENABLED:-<unset>} — nothing to configure (markitdown-ocr disabled)"
  exit 0
fi

python3 - <<'PY'
import os, json, urllib.request, urllib.error, sys

_kb_host = os.environ.get("KB_HOST")
if not _kb_host:
    sys.exit("FAIL  KB_HOST not set -- export KB_HOST=http://<host>:<port> (see .env.template)")
O = _kb_host.rstrip("/")
AK = os.environ.get("OPENWEBUI_ADMIN_API_KEY", "")
if not AK:
    sys.exit("FAIL  OPENWEBUI_ADMIN_API_KEY not set in .env.local (run: make api-keys)")

H = {"Authorization": "Bearer " + AK, "Content-Type": "application/json"}
REQUEST_TIMEOUT = 15

# OWUI reaches the engine over owui_net by service name.
ENGINE_URL = "http://markitdown-ocr:8080"

TOKEN = os.environ.get("OCR_SERVICE_TOKEN", "")
if not TOKEN:
    sys.exit("FAIL  OCR_SERVICE_TOKEN not set in .env.local (required; run: make bootstrap with OCR_ENABLED=true)")
WANT = {
    "CONTENT_EXTRACTION_ENGINE": "external",
    "EXTERNAL_DOCUMENT_LOADER_URL": ENGINE_URL,
    "EXTERNAL_DOCUMENT_LOADER_API_KEY": TOKEN,
    # OWUI validates HEADERS as a dict (OpenAPI anyOf: object|null), NOT a
    # string. Posting "{}" (a string) -> HTTP 422 dict_type. Empty dict =
    # no custom headers (the default).
    "EXTERNAL_DOCUMENT_LOADER_HEADERS": {},
}

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(O + path, data=data, headers=H, method=method)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except urllib.error.URLError as e:
        return 0, "URLError: %s" % e

def parse_json(text, label):
    try:
        return json.loads(text)
    except (TypeError, ValueError) as e:
        sys.exit("FAIL  %s returned invalid JSON: %s" % (label, e))

# GET first: shows the operator the current values + confirms the key names the
# OWUI image actually uses (the names are owned by the image, not this repo).
st, txt = call("GET", "/api/v1/retrieval/config")
if st != 200:
    sys.exit("FAIL  GET /api/v1/retrieval/config -> HTTP %s: %s" % (st, txt[:200]))
before = parse_json(txt, "GET /api/v1/retrieval/config")
print("INFO  current: engine=%r url=%r key_set=%s" % (
    before.get("CONTENT_EXTRACTION_ENGINE"),
    before.get("EXTERNAL_DOCUMENT_LOADER_URL"),
    bool(before.get("EXTERNAL_DOCUMENT_LOADER_API_KEY")),
))

st, txt = call("POST", "/api/v1/retrieval/config/update", WANT)
if st != 200:
    sys.exit("FAIL  update extraction config -> HTTP %s: %s" % (st, txt[:200]))

# Read-back + assert each key stuck to the exact value we posted.
st, txt = call("GET", "/api/v1/retrieval/config")
if st != 200:
    sys.exit("FAIL  read-back GET -> HTTP %s: %s" % (st, txt[:200]))
after = parse_json(txt, "GET /api/v1/retrieval/config")
for k, v in WANT.items():
    got = after.get(k)
    if got != v:
        sys.exit("FAIL  %s did not stick: posted %r got %r" % (k, v, got))

print("OK    external extraction engine ENABLED -> %s" % ENGINE_URL)
print("      CONTENT_EXTRACTION_ENGINE=external, API key set, no per-type fallback")
PY