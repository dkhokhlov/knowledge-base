#!/usr/bin/env bash
# Create the Open WebUI admin account from OPENWEBUI_FIRST_USER/PASSWORD in
# .env.local, via the signup API. The first registrant becomes admin (OWUI
# default); this script automates that step so a from-scratch run does not
# require a manual UI signup.
#
# Idempotent: if the account already exists (signin succeeds), it is a no-op.
# If signin fails because the account is absent, it signs up. If signin fails
# but the account exists (password mismatch), it fails with a clear message.
#
# Preconditions:
#   - Stack running and healthy (`make start`).
#   - ENABLE_SIGNUP=true (compose default; required for the signup call).
#   - OPENWEBUI_FIRST_USER / OPENWEBUI_FIRST_PASSWORD set in .env.local.
#
# Run after `make start`, before `make api-keys` (api-keys.sh signs in as this
# admin). Exits non-zero on any failure.
set -euo pipefail
cd "$(dirname "$0")/.."

# .env (config of record) + .env.local (secrets + test creds) -> exported env.
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
ADMIN_USER = os.environ.get("OPENWEBUI_FIRST_USER", "")
ADMIN_PASS = os.environ.get("OPENWEBUI_FIRST_PASSWORD", "")
ADMIN_NAME = os.environ.get("OPENWEBUI_ADMIN_NAME") or "admin"

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
        return e.code, (e.read().decode() or "")
    except urllib.error.URLError as e:
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
        return d.get("token", ""), d.get("role", "")
    return "", None

def whoami(token):
    code, d, _ = jget("GET", "/api/v1/auths/", token)
    if code == 200 and d:
        return d.get("role", "")
    return ""

def out(msg): print(msg)

# --- 1. try signin (idempotent fast path) ------------------------------------
token, role = signin(ADMIN_USER, ADMIN_PASS)
if token:
    role = whoami(token) or role
    if role != "admin":
        sys.exit("FAIL  %s exists but role=%s (expected admin; first registrant must be admin)" % (ADMIN_USER, role))
    out("OK    admin %s already exists (signin ok, role=admin)" % ADMIN_USER)
    sys.exit(0)

# --- 2. signin failed -> create via signup (first registrant -> admin) -------
code, d, txt = jget("POST", "/api/v1/auths/signup", None,
                    {"name": ADMIN_NAME, "email": ADMIN_USER, "password": ADMIN_PASS})
if code == 200 and d and d.get("token"):
    role = d.get("role", "")
    if role != "admin":
        sys.exit("FAIL  signup returned role=%s (expected admin; was another user registered first?)" % role)
    out("OK    created admin %s (role=admin)" % ADMIN_USER)
    sys.exit(0)
# 409 (or any non-200 with "already") means the account exists but signin
# failed -> password mismatch in .env.local, not a missing account.
if code == 409 or (txt and "already" in txt.lower()):
    sys.exit("FAIL  %s already exists but signin failed -> OPENWEBUI_FIRST_PASSWORD mismatch in .env.local" % ADMIN_USER)
sys.exit("FAIL  signup -> %s %s" % (code, (txt or "")[:200]))
PY