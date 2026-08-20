#!/usr/bin/env bash
# Provision Open WebUI API keys for the admin and a dedicated non-admin agent
# user, and persist them to .env.local. Idempotent (re-running keeps existing
# keys/passwords; set FORCE=1 to rotate the API keys).
#
# Preconditions:
#   - Stack running and healthy (`make start`).
#   - OPENWEBUI_TEST_USER / OPENWEBUI_TEST_PASSWORD in .env.local = an admin
#     account (the first UI registrant becomes admin).
#
# What it does:
#   1. Enables API keys stack-wide (auth.enable_api_keys) if disabled.
#   2. Enables the non-admin API-key permission (user.permissions.features.api_keys).
#   3. Creates a non-admin agent user (OPENWEBUI_USER) if missing, by briefly
#      re-enabling signup (ENABLE_SIGNUP) with DEFAULT_USER_ROLE=user, then
#      restoring both to their prior values. Signup ends disabled.
#   4. Gets or generates an API key for the admin and for the agent user.
#   5. Writes into .env.local (chmod 0600 preserved):
#        OPENWEBUI_USER, OPENWEBUI_USER_PASSWORD,
#        OPENWEBUI_ADMIN_API_KEY, OPENWEBUI_USER_API_KEY
#
# Security note: OPENWEBUI_ADMIN_API_KEY grants full admin (bypasses access
# control). Hand agents the OPENWEBUI_USER_API_KEY (non-admin, read-scoped via
# the existing '*' KB read grants; cannot write to the admin's KBs).
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
import os, json, secrets, tempfile, time, urllib.request, urllib.error, sys

# OWUI is fronted by Caddy at the KB_HOST root; reach its /api/* there.
O = os.environ.get("KB_HOST") or ("http://localhost:%s" % os.environ.get("KB_HOST_PORT", "3000"))
ADMIN_USER = os.environ.get("OPENWEBUI_TEST_USER", "")
ADMIN_PASS = os.environ.get("OPENWEBUI_TEST_PASSWORD", "")
AGENT_USER = os.environ.get("OPENWEBUI_USER") or "agent@local.test"
AGENT_NAME = os.environ.get("OPENWEBUI_USER_NAME") or "Agent"
FORCE = os.environ.get("FORCE", "") == "1"

if not ADMIN_USER or not ADMIN_PASS:
    sys.exit("FAIL  OPENWEBUI_TEST_USER / OPENWEBUI_TEST_PASSWORD not set in .env.local (admin account)")

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
        # block (e.g. the temp-signup restore below).
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
    sys.exit("FAIL  admin signin failed for %s (check OPENWEBUI_TEST_USER/PASSWORD)" % ADMIN_USER)
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

# --- 3b. grant '*' read on the chat model so the agent user can RAG chat -----
# Without a model access grant, a non-admin user sees 0 models and
# /api/chat/completions returns "Model not found". Same '*' pattern as KB grants.
# Grant the chat model the agent actually requests: OPENWEBUI_MODEL (read by the
# kb skill wrapper), falling back to MODEL_NAME (the stack's chat LLM), then the
# built-in default. .env keeps OPENWEBUI_MODEL in sync with MODEL_NAME.
CHAT_MODEL = os.environ.get("OPENWEBUI_MODEL") or os.environ.get("MODEL_NAME") or "gemma4:12b"
code, ml, _ = jget("GET", "/api/models", admin_jwt)
mids = []
if code == 200 and isinstance(ml, dict):
    mids = [m.get("id") for m in (ml.get("data") or []) if isinstance(m, dict)]
if CHAT_MODEL not in mids:
    sys.exit("FAIL  chat model %s not found in admin's model list (run: make pull-models); not granting — agent RAG chat would fail" % CHAT_MODEL)
grant = {"resource_type": "model", "resource_id": CHAT_MODEL,
         "principal_type": "user", "principal_id": "*", "permission": "read"}
code, md, txt = jget("POST", "/api/v1/models/model/access/update", admin_jwt,
                     {"id": CHAT_MODEL, "name": CHAT_MODEL, "access_grants": [grant]})
if code != 200:
    sys.exit("FAIL  model access grant on %s -> %s %s (agent RAG chat would fail)" % (CHAT_MODEL, code, txt[:160]))
out("OK    granted '*' read on chat model %s (agent can RAG chat)" % CHAT_MODEL)

# --- 4. ensure the non-admin agent user exists -------------------------------
agent_pass = os.environ.get("OPENWEBUI_USER_PASSWORD", "")
agent_jwt = signin(AGENT_USER, agent_pass) if agent_pass else ""
if agent_jwt:
    out("OK    agent user %s exists (signin ok)" % AGENT_USER)
else:
    # Need to create it. The admin user-create API (POST /api/v1/auths/add)
    # exists, but it can set a new account to 'pending' (unable to sign in) on
    # some builds. The signup path with DEFAULT_USER_ROLE=user is the proven
    # way to get a signable non-admin agent account here. Per-account provisioning
    # via auths/add (with a signin + api_key flow) is done by the kb-gateway's
    # admin endpoint (POST /admin/users) — see README "KB user provisioning".
    if not agent_pass:
        agent_pass = secrets.token_hex(16)
    code, cfg2, _ = jget("GET", "/api/v1/auths/admin/config", admin_jwt)
    snap_signup = cfg2.get("ENABLE_SIGNUP")
    snap_role = cfg2.get("DEFAULT_USER_ROLE")
    cfg2["ENABLE_SIGNUP"] = True
    cfg2["DEFAULT_USER_ROLE"] = "user"
    code, d, txt = jget("POST", "/api/v1/auths/admin/config", admin_jwt, cfg2)
    if code != 200:
        sys.exit("FAIL  temp-enable signup -> %s %s" % (code, txt[:200]))
    code, sd, txt = jget("POST", "/api/v1/auths/signup", None,
                         {"name": AGENT_NAME, "email": AGENT_USER, "password": agent_pass})
    # Restore signup/role regardless of signup outcome, retrying transport
    # errors (a URLError here must not leave signup enabled), then verify it
    # actually took effect.
    cfg2["ENABLE_SIGNUP"] = snap_signup
    cfg2["DEFAULT_USER_ROLE"] = snap_role
    last_err = ""
    for attempt in range(3):
        rcode, _, rtxt = jget("POST", "/api/v1/auths/admin/config", admin_jwt, cfg2)
        if rcode == 200:
            break
        last_err = "%s %s" % (rcode, (rtxt or "")[:160])
        time.sleep(1 + attempt)
    else:
        sys.exit("FAIL  restore signup config after retries: %s (signup may still be ENABLED — check the admin UI)" % last_err)
    vcode, vcfg, _ = jget("GET", "/api/v1/auths/admin/config", admin_jwt)
    if vcode != 200 or not isinstance(vcfg, dict) \
       or vcfg.get("ENABLE_SIGNUP") != snap_signup \
       or vcfg.get("DEFAULT_USER_ROLE") != snap_role:
        sys.exit("FAIL  signup config did not restore to ENABLE_SIGNUP=%s DEFAULT_USER_ROLE=%s (got %s) — check the admin UI"
                 % (snap_signup, snap_role, vcfg))
    if code != 200 or not sd:
        sys.exit("FAIL  signup %s -> %s %s" % (AGENT_USER, code, txt[:200]))
    agent_jwt = sd.get("token", "")
    if not agent_jwt:
        agent_jwt = signin(AGENT_USER, agent_pass)
    out("OK    created agent user %s (signup re-enabled then restored)" % AGENT_USER)

# --- 5. get-or-generate API keys ---------------------------------------------
def key_for(jwt, label, existing, expect_email, expect_role=None):
    """Return a working API key for the account that owns `jwt`. Keep `existing`
    only if it authenticates AND belongs to the expected account (and role, if
    given); otherwise generate a fresh key for this account (rotates/replaces).
    This prevents a valid admin key sitting in the non-admin slot from being
    kept as the read-scoped agent key."""
    if existing and not FORCE:
        code, d, _ = jget("GET", "/api/v1/auths/", existing)
        if code == 200 and d \
           and (d.get("email") or "").casefold() == (expect_email or "").casefold() \
           and (expect_role is None or d.get("role") == expect_role):
            return existing, "kept"
        out("WARN  existing %s key did not match expected account/role; regenerating" % label)
    code, d, txt = jget("POST", "/api/v1/auths/api_key", jwt)
    if code != 200 or not d:
        sys.exit("FAIL  generate %s api key -> %s %s" % (label, code, txt[:200]))
    return d.get("api_key", ""), "generated"

admin_existing = os.environ.get("OPENWEBUI_ADMIN_API_KEY", "")
admin_key, admin_act = key_for(admin_jwt, "admin", admin_existing, expect_email=ADMIN_USER)
out("OK    %s ADMIN api key (%s)" % (admin_act, admin_key[:10] + "..."))

user_existing = os.environ.get("OPENWEBUI_USER_API_KEY", "")
user_key, user_act = key_for(agent_jwt, "agent", user_existing,
                             expect_email=AGENT_USER, expect_role="user")
out("OK    %s USER  api key (%s)" % (user_act, user_key[:10] + "..."))

# --- 6. upsert .env.local (chmod 0600 preserved) -----------------------------
env_path = ".env.local"
new_vals = {
    "OPENWEBUI_USER": AGENT_USER,
    "OPENWEBUI_USER_PASSWORD": agent_pass,
    "OPENWEBUI_ADMIN_API_KEY": admin_key,
    "OPENWEBUI_USER_API_KEY": user_key,
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
out("  OPENWEBUI_USER=%s" % AGENT_USER)
out("  OPENWEBUI_USER_PASSWORD=<%d chars>" % len(agent_pass))
out("  OPENWEBUI_ADMIN_API_KEY=<full admin>  (do NOT give to agents)")
out("  OPENWEBUI_USER_API_KEY=<read-scoped>  (hand this to agents)")
out("")
out("Verify:")
out("  curl -s -H \"Authorization: Bearer $OPENWEBUI_USER_API_KEY\" %s/api/v1/knowledge/" % O)
PY