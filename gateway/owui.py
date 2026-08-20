"""Open WebUI HTTP client (stdlib urllib) for identity + user provisioning.

All calls go to OWUI over the container-internal `owui_net` network. The
gateway never holds a static admin key of its own — it forwards the caller's
KB_API_KEY (an OWUI key) to authorize admin operations, and signs in as a
newly created user with that user's temp password to obtain that user's own
JWT (never the admin's) for per-user API-key generation (codex #3).

Raises OwuiError (mapped by app.py to 503 on transport failure, left as the
caller's concern for 4xx). Duplicate-email on add_user is surfaced as a
distinct OwuiConflict so the gateway can return 409.
"""
import json
import os
import urllib.error
import urllib.request


class OwuiError(Exception):
    """Transport failure talking to OWUI (-> 503)."""


class OwuiConflict(Exception):
    """OWUI rejected a create because the email already exists (-> 409)."""


def _base():
    return os.environ.get("OWUI_URL", "http://openwebui:8080").rstrip("/")


def _timeout():
    return float(os.environ.get("OWUI_TIMEOUT", "15"))


def _req(method, path, token=None, body=None):
    url = _base() + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode() or "")
    except urllib.error.URLError as e:
        raise OwuiError("OWUI unreachable: %s" % e)


def _j(method, path, token=None, body=None):
    code, txt = _req(method, path, token, body)
    try:
        data = json.loads(txt) if txt else None
    except Exception:
        data = None
    return code, data, txt


def whoami(api_key):
    """Resolve the caller's identity from their OWUI key. Returns
    {id,email,role} or raises OwuiError. code != 200 -> returns None (caller
    maps to 401)."""
    try:
        code, data, _ = _j("GET", "/api/v1/auths/", api_key)
    except OwuiError:
        raise
    if code != 200:
        return None
    if not isinstance(data, dict) or not data.get("email"):
        return None
    return {"id": data.get("id"), "email": data.get("email"),
            "role": data.get("role")}


def signin(email, password):
    """Sign in as a user; return that user's JWT, or None on bad creds."""
    code, data, _ = _j("POST", "/api/v1/auths/signin", None,
                       {"email": email, "password": password})
    if code == 200 and isinstance(data, dict):
        return data.get("token", "")
    return None


def add_user(admin_key, name, email, password, role):
    """Create a user via the admin add endpoint. role is passed explicitly
    (OWUI defaults to 'pending', which cannot sign in). Returns the new user
    dict {id, email, role, ...}. Raises OwuiConflict on duplicate email."""
    code, data, txt = _j("POST", "/api/v1/auths/add", admin_key,
                         {"name": name, "email": email, "password": password,
                          "role": role})
    if code == 200 and isinstance(data, dict) and data.get("id"):
        return data
    # OWUI signals a duplicate email as 400/409 with a mention of existing.
    low = (txt or "").lower()
    if code in (400, 409) or "already" in low or "exists" in low:
        raise OwuiConflict("email %s already exists" % email)
    raise OwuiError("add_user -> HTTP %s: %s" % (code, (txt or "")[:200]))


def gen_api_key(jwt):
    """Generate an API key for the account that owns `jwt`. Returns the key
    string, or raises OwuiError."""
    code, data, txt = _j("POST", "/api/v1/auths/api_key", jwt)
    if code == 200 and isinstance(data, dict) and data.get("api_key"):
        return data["api_key"]
    raise OwuiError("gen_api_key -> HTTP %s: %s" % (code, (txt or "")[:200]))


def delete_user(admin_key, user_id):
    """Admin delete of a user by id. Best-effort (used for rollback). Returns
    True on success, False otherwise."""
    code, _, _ = _j("DELETE", "/api/v1/users/%s" % user_id, admin_key)
    return code in (200, 204)


def revoke_api_key(jwt):
    """Best-effort revoke of the API key belonging to the JWT owner."""
    code, _, _ = _j("DELETE", "/api/v1/auths/api_key", jwt)
    return code in (200, 204)


# --- capability probe (codex #3) --------------------------------------------
# The provisioning flow depends on endpoints that vary by Open WebUI image
# tag (OPENWEBUI_IMAGE_TAG is mutable). Probe /openapi.json (no auth) at startup
# and report which required paths+methods are present. The gateway exposes
# /admin/users only if all are present; otherwise it returns 501.
PROVISIONING_PATHS = {
    "POST /api/v1/auths/add": ("POST", "/api/v1/auths/add"),
    "POST /api/v1/auths/signin": ("POST", "/api/v1/auths/signin"),
    "POST /api/v1/auths/api_key": ("POST", "/api/v1/auths/api_key"),
    "GET /api/v1/auths/": ("GET", "/api/v1/auths/"),
    "DELETE /api/v1/auths/api_key": ("DELETE", "/api/v1/auths/api_key"),
    "DELETE /api/v1/users/{user_id}": ("DELETE", "/api/v1/users/{user_id}"),
}


def provisioning_capabilities():
    """Return (ok: bool, missing: [str], error: str|None) by reading OWUI's
    OpenAPI schema. `ok` is True only if every required path+method is present."""
    try:
        code, data, _ = _j("GET", "/openapi.json")
    except OwuiError as e:
        return False, [], str(e)
    if code != 200 or not isinstance(data, dict):
        return False, [], "openapi.json unreadable (HTTP %s)" % code
    paths = data.get("paths") or {}
    missing = []
    for label, (method, path) in PROVISIONING_PATHS.items():
        spec = paths.get(path)
        if not isinstance(spec, dict) or method.lower() not in spec:
            missing.append(label)
    return (len(missing) == 0), missing, None