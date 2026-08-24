#!/usr/bin/env bash
# One-time admin enable for projects-memory indexing (Option A).
#
# The kb-gateway has NO user-key OWUI-KB write path (its /index uses the admin
# key internally). Projects memory is indexed by the skill-side wrapper
# (skills/claude/scripts/owui.py index-projects), which calls OWUI REST
# directly with the caller's USER key so each project KB is owned by the caller
# (KB.user.email == account; search filters by KB owner). OWUI gates KB
# creation on the workspace.knowledge permission, which is FALSE by default in
# this deployment. This script enables it once (admin, idempotent) and verifies
# with a disposable user-key probe KB.
#
#   1. GET /api/v1/users/default/permissions (admin); if workspace.knowledge is
#      already true, skip.
#   2. Else flip workspace.knowledge=true and POST the FULL body back (replace,
#      not merge — like the access-grants gotcha). The agent user record has no
#      stored per-user permissions field, so flipping the default propagates to
#      the existing agent user.
#   3. Verify: create a disposable probe KB with the USER key. 403 means the
#      enable did not take (FAIL loudly). 200/201/409 all mean KB creation is
#      permitted (409 = a leftover probe from a prior run; cleaned up). Delete
#      the probe (admin) on the way out.
#
# Preconditions:
#   - Stack running and healthy (`make start`).
#   - OPENWEBUI_ADMIN_API_KEY + OPENWEBUI_USER_API_KEY in .env.local
#     (provisioned by `make api-keys`).
#
# Idempotent: re-running is a no-op once the permission is on.
# No *_KB_ID is written to .env.local — index-projects derives KB names and
# lists KBs at run time. Run once before the first `index-projects`.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck source=/dev/null
. ./.env
# shellcheck source=/dev/null
. ./.env.local
set +a

: "${OPENWEBUI_ADMIN_API_KEY:?FAIL  OPENWEBUI_ADMIN_API_KEY not set in .env.local (run: make api-keys)}"
: "${OPENWEBUI_USER_API_KEY:?FAIL  OPENWEBUI_USER_API_KEY not set in .env.local (run: make api-keys)}"

python3 - <<'PY'
import os, json, urllib.request, urllib.error, sys

O = os.environ.get("KB_HOST") or ("http://localhost:%s" % os.environ.get("KB_HOST_PORT", "3000"))
AK = os.environ["OPENWEBUI_ADMIN_API_KEY"]
UK = os.environ["OPENWEBUI_USER_API_KEY"]
PROBE = "projects-index-bootstrap-probe"

def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    headers["Authorization"] = "Bearer " + (token or AK)
    req = urllib.request.Request(O + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode(errors="replace") or "")

# --- 1. read default permissions; is workspace.knowledge already on? ----------
st, txt = call("GET", "/api/v1/users/default/permissions")
if st != 200:
    sys.exit("FAIL  GET /api/v1/users/default/permissions -> HTTP %s: %s" % (st, txt[:300]))
perms = json.loads(txt)
wk = (perms.get("workspace") or {}).get("knowledge", False)
if wk:
    print("OK    workspace.knowledge already enabled (default permissions)")
else:
    # --- 2. flip workspace.knowledge=true; POST the FULL body back (replace) -
    perms.setdefault("workspace", {})["knowledge"] = True
    st, txt = call("POST", "/api/v1/users/default/permissions", perms)
    if st != 200:
        sys.exit("FAIL  POST /api/v1/users/default/permissions -> HTTP %s: %s" % (st, txt[:300]))
    print("OK    enabled workspace.knowledge (default permissions)")

# --- 3. verify with a disposable user-key probe KB -----------------------------
# Clean up any leftover probe from a prior run (admin can delete any KB).
st, txt = call("GET", "/api/v1/knowledge/")
items = json.loads(txt) if txt else []
items = items.get("items", []) if isinstance(items, dict) else items
for k in items:
    if k.get("name") == PROBE:
        call("DELETE", "/api/v1/knowledge/%s/delete" % k.get("id"), token=AK)
        print("OK    removed leftover probe KB %s" % k.get("id"), file=sys.stderr)
        break

st, txt = call("POST", "/api/v1/knowledge/create",
               {"name": PROBE, "description": "projects-index-bootstrap probe"}, token=UK)
if st == 403:
    sys.exit("FAIL  user-key KB create still 403 after enable: %s\n"
             "       the permission did not propagate to the agent user. "
             "Restart Open WebUI (make restart) and re-run, or check the user record." % txt[:300])
if st not in (200, 201):
    sys.exit("FAIL  probe KB create -> HTTP %s: %s" % (st, txt[:300]))
probe_id = (json.loads(txt) or {}).get("id")
print("OK    user key created probe KB %s (workspace.knowledge verified)" % probe_id)

# Delete the probe (admin). The caller owns it (user key created it), but the
# admin key is used for cleanup so a permission edge case cannot leave a orphan.
st, txt = call("DELETE", "/api/v1/knowledge/%s/delete" % probe_id, token=AK)
if st not in (200, 204):
    print("WARN  probe KB delete -> HTTP %s: %s (leftover %s; delete it via the admin UI)"
          % (st, txt[:200], probe_id), file=sys.stderr)
else:
    print("OK    removed probe KB %s" % probe_id)

print("OK    projects-memory indexing is ready: run `index-projects` (kb skill)")
PY