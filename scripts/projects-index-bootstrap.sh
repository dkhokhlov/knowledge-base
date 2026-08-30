#!/usr/bin/env bash
# One-time admin enable for projects-memory indexing (Option A).
#
# The api-gateway has NO user-key OWUI-KB write path (its /index uses the admin
# key internally). Projects memory is indexed by the skill-side wrapper
# (skills/claude/scripts/owui.py index-projects), which calls OWUI REST
# directly with the caller's USER key so each project KB is owned by the caller
# (KB.user.email == account; search filters by KB owner). OWUI gates KB
# creation on the workspace.knowledge permission, which is FALSE by default in
# this deployment. This script enables it once (admin, idempotent) and re-reads
# the default permissions to verify the flags persisted. It also enables
# sharing.public_knowledge so a user key can grant public read (user:*) on the
# project KBs it creates, letting every provisioned user retrieve every project
# KB (the chosen public-read model).
#
#   1. GET /api/v1/users/default/permissions (admin); ensure both
#      workspace.knowledge and sharing.public_knowledge are true. POST the FULL
#      body back (replace, not merge — like the access-grants gotcha) only if a
#      flag changed. User records have no per-user permissions field, so flipping
#      the default propagates to existing users (no restart).
#   2. Re-GET the default permissions and assert both flags are true. POST
#      replaces (not merges) the full body, so an HTTP 200 alone does not prove
#      persistence; the re-read does. The "user key can create a KB" check is a
#      test-suite assertion (ephemeral user), not a bootstrap probe.
#
# Preconditions:
#   - Stack running and healthy (`make start`).
#   - OPENWEBUI_ADMIN_API_KEY in .env.local (provisioned by `make api-keys`).
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

python3 - <<'PY'
import os, json, urllib.request, urllib.error, sys

_kb_host = os.environ.get("KB_HOST")
if not _kb_host:
    sys.exit("FAIL  KB_HOST not set -- export KB_HOST=http://<host>:<port> (see .env.template)")
O = _kb_host.rstrip("/")
AK = os.environ["OPENWEBUI_ADMIN_API_KEY"]

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + AK}
    req = urllib.request.Request(O + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode(errors="replace") or "")

# --- 1. read default permissions; ensure workspace.knowledge AND ------------
#     sharing.public_knowledge are on. Both gate the projects-memory flow:
#     workspace.knowledge lets a user key CREATE a project KB; public_knowledge
#     lets the owner grant public read (user:*) so every user can retrieve it.
#     POST replaces the FULL body, so set both flags on the fetched dict and
#     post it back only if something changed (idempotent).
st, txt = call("GET", "/api/v1/users/default/permissions")
if st != 200:
    sys.exit("FAIL  GET /api/v1/users/default/permissions -> HTTP %s: %s" % (st, txt[:300]))
perms = json.loads(txt)
changed = []
if not (perms.get("workspace") or {}).get("knowledge", False):
    perms.setdefault("workspace", {})["knowledge"] = True
    changed.append("workspace.knowledge")
if not (perms.get("sharing") or {}).get("public_knowledge", False):
    perms.setdefault("sharing", {})["public_knowledge"] = True
    changed.append("sharing.public_knowledge")
if changed:
    st, txt = call("POST", "/api/v1/users/default/permissions", perms)
    if st != 200:
        sys.exit("FAIL  POST /api/v1/users/default/permissions -> HTTP %s: %s" % (st, txt[:300]))
    print("OK    enabled %s (default permissions)" % ", ".join(changed))
else:
    print("OK    workspace.knowledge + sharing.public_knowledge already enabled (default permissions)")

# --- 2. re-read default permissions; assert both flags persisted --------------
# POST replaces (not merges) the full body, so an HTTP 200 alone does not prove
# a flag persisted. Re-GET and assert both are true; without this the failure
# surfaces later as a buried created:"failed" 403 in index-projects.
st, txt = call("GET", "/api/v1/users/default/permissions")
if st != 200:
    sys.exit("FAIL  re-GET /api/v1/users/default/permissions -> HTTP %s: %s" % (st, txt[:300]))
perms = json.loads(txt)
wk = (perms.get("workspace") or {}).get("knowledge", False)
pk = (perms.get("sharing") or {}).get("public_knowledge", False)
if not (wk and pk):
    sys.exit("FAIL  permissions did not persist: workspace.knowledge=%s sharing.public_knowledge=%s"
             % (wk, pk))
print("OK    verified workspace.knowledge + sharing.public_knowledge persisted (default permissions)")

print("OK    projects-memory indexing is ready: run `index-projects` (kb skill)")
PY