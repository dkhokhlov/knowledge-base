#!/usr/bin/env bash
# Provision the Open WebUI admin API key and persist it to .env.local.
# Idempotent (re-running keeps the existing key; set FORCE=1 to rotate it).
#
# Preconditions:
#   - Stack running and healthy (`make start`).
#   - OPENWEBUI_FIRST_USER / OPENWEBUI_FIRST_PASSWORD in .env.local = an admin
#     account (the first UI registrant becomes admin).
#
# What it does:
#   1. Enables API keys stack-wide (auth.enable_api_keys) if disabled.
#   2. Enables the non-admin API-key permission (user.permissions.features.api_keys)
#      so operator-created users (make users-create) can hold API keys.
#   3. Grants '*' read on the chat model so any non-admin user can RAG chat.
#   4. Gets or generates the admin API key.
#   5. Writes OPENWEBUI_ADMIN_API_KEY into .env.local (chmod 0600 preserved).
#
# No dedicated agent user is provisioned. Operator users (and per-run ephemeral
# test users) are created separately via `make users-create`; their keys are the
# operator's KB_API_KEY, not a provisioned identity.
#
# Security note: OPENWEBUI_ADMIN_API_KEY grants full admin (bypasses access
# control). Do not hand it to agents.
set -euo pipefail
cd "$(dirname "$0")/.."

# .env (config of record) + .env.local (secrets + test creds) -> exported env.
# Capture a `make api-keys OCR_ENABLED=<val>` override before `set -a; . ./.env`
# (which would clobber it with .env's value); restore after sourcing.
_OCR_ENABLED_OVR="${OCR_ENABLED:-}"
set -a
# shellcheck source=/dev/null
. ./.env
# shellcheck source=/dev/null
. ./.env.local
set +a
if [ -n "$_OCR_ENABLED_OVR" ]; then export OCR_ENABLED="$_OCR_ENABLED_OVR"; fi

python3 - <<'PY'
import os, json, tempfile, urllib.request, urllib.error, sys

# OWUI is fronted by Caddy at the KB_HOST root; reach its /api/* there.
_kb_host = os.environ.get("KB_HOST")
if not _kb_host:
    sys.exit("FAIL  KB_HOST not set -- export KB_HOST=http://<host>:<port> (see .env.template)")
O = _kb_host.rstrip("/")
ADMIN_USER = os.environ.get("OPENWEBUI_FIRST_USER", "")
ADMIN_PASS = os.environ.get("OPENWEBUI_FIRST_PASSWORD", "")
FORCE = os.environ.get("FORCE", "") == "1"

if not ADMIN_USER or not ADMIN_PASS:
    sys.exit("FAIL  OPENWEBUI_FIRST_USER / OPENWEBUI_FIRST_PASSWORD not set in .env.local (admin account)")

def req(method, path, token=None, body=None):
    url = O + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except urllib.error.URLError as e:
        # Transport error (connection refused, timeout, DNS). Return a non-200
        # code so callers fail gracefully instead of raising past a finally
        # block.
        return 0, "URLError: %s" % e

def jget(method, path, token=None, body=None):
    code, txt = req(method, path, token, body)
    if code != 200:
        return code, None, txt
    try:
        return code, json.loads(txt), txt
    except Exception:
        return code, None, txt

def signin(email, password):
    code, d, _ = jget("POST", "/api/v1/auths/signin", None, {"email": email, "password": password})
    if code == 200 and d:
        return d.get("token", "")
    return ""

def out(msg): print(msg)

# --- 1. admin signin ---------------------------------------------------------
admin_jwt = signin(ADMIN_USER, ADMIN_PASS)
if not admin_jwt:
    sys.exit("FAIL  admin signin failed for %s (check OPENWEBUI_FIRST_USER/PASSWORD)" % ADMIN_USER)
out("OK    admin signin -> JWT")

# --- 2. enable API keys stack-wide (admin config round-trip) -----------------
code, cfg, _ = jget("GET", "/api/v1/auths/admin/config", admin_jwt)
if code != 200 or cfg is None:
    sys.exit("FAIL  GET /api/v1/auths/admin/config -> %s" % code)
changed_cfg = False
if not cfg.get("ENABLE_API_KEYS"):
    cfg["ENABLE_API_KEYS"] = True
    changed_cfg = True
if changed_cfg:
    code, d, txt = jget("POST", "/api/v1/auths/admin/config", admin_jwt, cfg)
    if code != 200:
        sys.exit("FAIL  enable ENABLE_API_KEYS -> %s %s" % (code, txt[:200]))
    out("OK    enabled ENABLE_API_KEYS (auth.enable_api_keys)")
else:
    out("OK    ENABLE_API_KEYS already enabled")

# --- 3. enable non-admin API-key permission (default permissions) ------------
code, perms, _ = jget("GET", "/api/v1/users/default/permissions", admin_jwt)
if code != 200 or perms is None:
    sys.exit("FAIL  GET /api/v1/users/default/permissions -> %s" % code)
feat = perms.get("features") or {}
if not feat.get("api_keys"):
    feat["api_keys"] = True
    perms["features"] = feat
    code, d, txt = jget("POST", "/api/v1/users/default/permissions", admin_jwt, perms)
    if code != 200:
        sys.exit("FAIL  enable features.api_keys -> %s %s" % (code, txt[:200]))
    out("OK    enabled features.api_keys (non-admin users may use API keys)")
else:
    out("OK    features.api_keys already enabled")

# --- 3b. grant '*' read on the chat model so any non-admin user can RAG chat --
# Without a model access grant, a non-admin user sees 0 models and
# /api/chat/completions returns "Model not found". Same '*' pattern as KB grants.
# Grant the chat model the api-gateway requests: OPENWEBUI_MODEL (inserted by
# the api-gateway for POST /memory/rag). Declared in .env.template (independent
# from GRAPHITI_MODEL/extraction); it must be a model `make pull-models` creates.
_chat_model = os.environ.get("OPENWEBUI_MODEL")
if not _chat_model:
    sys.exit("FAIL  OPENWEBUI_MODEL not set -- declare it in .env.template")
CHAT_MODEL = _chat_model
code, ml, _ = jget("GET", "/api/models", admin_jwt)
mids = []
if code == 200 and isinstance(ml, dict):
    mids = [m.get("id") for m in (ml.get("data") or []) if isinstance(m, dict)]
if CHAT_MODEL not in mids:
    sys.exit("FAIL  chat model %s not found in admin's model list (run: make pull-models); not granting — non-admin RAG chat would fail" % CHAT_MODEL)
grant = {"resource_type": "model", "resource_id": CHAT_MODEL,
         "principal_type": "user", "principal_id": "*", "permission": "read"}
code, md, txt = jget("POST", "/api/v1/models/model/access/update", admin_jwt,
                     {"id": CHAT_MODEL, "name": CHAT_MODEL, "access_grants": [grant]})
if code != 200:
    sys.exit("FAIL  model access grant on %s -> %s %s (non-admin RAG chat would fail)" % (CHAT_MODEL, code, txt[:160]))
out("OK    granted '*' read on chat model %s (non-admin users can RAG chat)" % CHAT_MODEL)

# --- 4. get-or-generate the admin API key ------------------------------------
def key_for(jwt, label, existing, expect_email):
    """Return a working API key for the account that owns `jwt`. Keep `existing`
    only if it authenticates AND belongs to the expected account; otherwise
    generate a fresh key for this account (rotates/replaces)."""
    if existing and not FORCE:
        code, d, _ = jget("GET", "/api/v1/auths/", existing)
        if code == 200 and d \
           and (d.get("email") or "").casefold() == (expect_email or "").casefold():
            return existing, "kept"
        out("WARN  existing %s key did not match expected account; regenerating" % label)
    code, d, txt = jget("POST", "/api/v1/auths/api_key", jwt)
    if code != 200 or not d:
        sys.exit("FAIL  generate %s api key -> %s %s" % (label, code, txt[:200]))
    return d.get("api_key", ""), "generated"

admin_existing = os.environ.get("OPENWEBUI_ADMIN_API_KEY", "")
admin_key, admin_act = key_for(admin_jwt, "admin", admin_existing, expect_email=ADMIN_USER)
out("OK    %s ADMIN api key (%s)" % (admin_act, admin_key[:10] + "..."))

# --- 5. upsert .env.local (chmod 0600 preserved) -----------------------------
env_path = ".env.local"
new_vals = {
    "OPENWEBUI_ADMIN_API_KEY": admin_key,
}
lines = []
if os.path.exists(env_path):
    with open(env_path) as f:
        lines = f.read().splitlines()
seen = set()
out_lines = []
for ln in lines:
    k = ln.split("=", 1)[0] if "=" in ln else None
    if k in new_vals:
        out_lines.append("%s=%s" % (k, new_vals[k]))
        seen.add(k)
    else:
        out_lines.append(ln)
for k, v in new_vals.items():
    if k not in seen:
        out_lines.append("%s=%s" % (k, v))
# Write to a temp file then atomically replace, so a crash or disk-full
# mid-write cannot truncate .env.local or leave it partially written.
tmp_fd, tmp_path = tempfile.mkstemp(dir=".", prefix=".env.local.", suffix=".tmp")
try:
    with os.fdopen(tmp_fd, "w") as f:
        f.write("\n".join(out_lines) + "\n")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, env_path)
except Exception:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    raise

out("")
out("Wrote to .env.local (chmod 0600):")
out("  OPENWEBUI_ADMIN_API_KEY=<full admin>  (do NOT give to agents)")
out("")
out("Verify (with your own user key in KB_API_KEY, set via `make users-create`):")
out("  curl -s -H \"Authorization: Bearer $KB_API_KEY\" %s/api/v1/knowledge/" % O)
PY

# When OCR is enabled, point OWUI at the markitdown-ocr external engine (the
# one post-start DB step, folded into the standard chain). .env.local on disk
# now has OPENWEBUI_ADMIN_API_KEY (written above); OCR_SERVICE_TOKEN was
# generated by `make bootstrap` (or is present from a prior run). config-ocr.sh
# re-sources both files itself. A `make api-keys OCR_ENABLED=false` override
# skips this.
if [ "${OCR_ENABLED:-true}" = "true" ]; then
  echo "==> OCR_ENABLED=true: pointing OWUI at the markitdown-ocr external engine"
  ./scripts/config-ocr.sh
fi

# Recreate the api-gateway so it picks up the OPENWEBUI_ADMIN_API_KEY just
# written to .env.local. The gateway was started (in `make provision` step 3 /
# `make start`) BEFORE this script wrote the key, and compose only interpolates
# ${OPENWEBUI_ADMIN_API_KEY:-} at `docker compose up` time; the container env is
# fixed at start and is not hot-reloaded. Without this, the gateway runs with an
# empty admin key (POST /index -> "OPENWEBUI_ADMIN_API_KEY not set in gateway
# env") until the next `make start`. Re-source .env.local so the just-written key
# is in the shell env compose interpolates from; `up -d` recreates only the
# gateway and only if the interpolated value changed (idempotent no-op on re-run
# with an unchanged key).
set -a
# shellcheck source=/dev/null
. ./.env.local
set +a
docker compose up -d api-gateway