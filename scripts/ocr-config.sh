#!/usr/bin/env bash
# Point Open WebUI's retrieval/extraction at the markitdown-ocr external engine
# (CONTENT_EXTRACTION_ENGINE=external + URL + a non-empty API key), or clear it
# (disable -> OWUI falls back to its default loaders). Sets the keys in the OWUI
# DB via the admin retrieval-config API (merge semantics: only the posted keys
# change).
#
# Idempotent: re-running re-asserts the same values.
#
# Usage:
#   make ocr-config            # enable (default)
#   scripts/ocr-config.sh disable   # clear the external engine (make ocr-disable)
#
# Preconditions:
#   - Stack running and healthy (`make start` / `make ocr-bootstrap`).
#   - markitdown-ocr service up and /health green (for enable).
#   - OPENWEBUI_ADMIN_API_KEY + OCR_SERVICE_TOKEN in .env.local.
#
# Why this exists: OWUI's external extraction engine is global + all-or-nothing.
# When the engine is "external" + URL + a NON-EMPTY API key are set, OWUI routes
# EVERY ingest to markitdown-ocr (no per-type fallback; an empty result orphans).
# An empty API key makes OWUI silently skip the external engine and fall through
# to its default loaders, so the key MUST be non-empty for enable.
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:-enable}"

set -a
# shellcheck source=/dev/null
. ./.env
# shellcheck source=/dev/null
. ./.env.local
set +a

export MODE

python3 - <<'PY'
import os, json, urllib.request, urllib.error, sys

O = os.environ.get("KB_HOST") or ("http://localhost:%s" % os.environ.get("KB_HOST_PORT", "3000"))
AK = os.environ.get("OPENWEBUI_ADMIN_API_KEY", "")
if not AK:
    sys.exit("FAIL  OPENWEBUI_ADMIN_API_KEY not set in .env.local (run: make api-keys)")

MODE = os.environ.get("MODE", "enable")
H = {"Authorization": "Bearer " + AK, "Content-Type": "application/json"}
REQUEST_TIMEOUT = 15

# OWUI reaches the engine over owui_net by service name.
ENGINE_URL = "http://markitdown-ocr:8080"

if MODE == "enable":
    TOKEN = os.environ.get("OCR_SERVICE_TOKEN", "")
    if not TOKEN:
        sys.exit("FAIL  OCR_SERVICE_TOKEN not set in .env.local (required for enable)")
    WANT = {
        "CONTENT_EXTRACTION_ENGINE": "external",
        "EXTERNAL_DOCUMENT_LOADER_URL": ENGINE_URL,
        "EXTERNAL_DOCUMENT_LOADER_API_KEY": TOKEN,
        # OWUI validates HEADERS as a dict (OpenAPI anyOf: object|null), NOT a
        # string. Posting "{}" (a string) -> HTTP 422 dict_type. Empty dict =
        # no custom headers (the default).
        "EXTERNAL_DOCUMENT_LOADER_HEADERS": {},
    }
else:
    # Clear: empty values drop OWUI back to its default loaders. HEADERS is
    # dict-typed (OWUI anyOf: object|null) so clear it to {} (the default), not
    # a string (posting "" -> HTTP 422 dict_type).
    WANT = {
        "CONTENT_EXTRACTION_ENGINE": "",
        "EXTERNAL_DOCUMENT_LOADER_URL": "",
        "EXTERNAL_DOCUMENT_LOADER_API_KEY": "",
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

if MODE == "enable":
    print("OK    external extraction engine ENABLED -> %s" % ENGINE_URL)
    print("      CONTENT_EXTRACTION_ENGINE=external, API key set, no per-type fallback")
else:
    print("OK    external extraction engine CLEARED (OWUI default loaders)")
PY

# Disable also drops the MARKITDOWN_OCR_PROVISIONED marker from .env so
# `make start`/`make restart` no longer add --profile ocr. (Enable writes the
# marker in ocr-bootstrap.sh, after the service is healthy + this config sticks.)
# This is a bash heredoc (not a Makefile inline heredoc): GNU make splits each
# recipe line into a separate shell, so an inline heredoc body does not reach
# python — the marker edit must live in this script.
if [ "$MODE" = disable ]; then
  python3 - <<'PY'
import os
key = "MARKITDOWN_OCR_PROVISIONED"; f = ".env"
out = [ln for ln in open(f).read().splitlines() if not ln.startswith(key + "=")]
open(f, "w").write("\n".join(out) + "\n")
print("OK    removed %s marker from .env" % key)
PY
fi