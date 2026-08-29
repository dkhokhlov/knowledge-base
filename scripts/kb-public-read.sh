#!/usr/bin/env bash
# Operator tool: make every Open WebUI knowledge base readable by every
# authenticated user (public read, principal user:*), and enable the
# sharing.public_knowledge default permission so non-admin owners can grant
# public read on the KBs they create (the index-projects flow).
#
# Why: OWUI has no "read all KBs" role for non-admins and no auto-grant on KB
# create; visibility is owner + explicit per-KB grants. This script is the
# one-time fix for an already-running stack (backfill existing KBs) AND the
# safety net for any KB created outside the controlled flows (e.g. via the OWUI
# UI). It is admin-only and idempotent.
#
#   A. ensure sharing.public_knowledge=true in default user permissions (live,
#      no restart; same merge-and-replace-POST pattern projects-index-bootstrap
#      uses for workspace.knowledge).
#   B. paginate GET /api/v1/knowledge/ (page size PAGE_ITEM_COUNT, ~30); for
#      each KB read its access_grants and merge user:* read (deduped by the full
#      (principal_type, principal_id, permission) tuple) via
#      POST /api/v1/knowledge/{id}/access/update (which REPLACES the grant set,
#      so existing group/user grants are preserved).
#
# Preconditions:
#   - Stack running and healthy (`make start`).
#   - OPENWEBUI_ADMIN_API_KEY in .env.local (provisioned by `make api-keys`).
#
# Idempotent: re-running re-asserts the permission and the per-KB grant; KBs
# already public-read are skipped (no access/update call).
# Does NOT touch workspace.knowledge (that is projects-index-bootstrap's job).
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
import os, json, urllib.parse, urllib.request, urllib.error, sys

_kb_host = os.environ.get("KB_HOST")
if not _kb_host:
    sys.exit("FAIL  KB_HOST not set -- export KB_HOST=http://<host>:<port> (see .env.template)")
O = _kb_host.rstrip("/")
AK = os.environ["OPENWEBUI_ADMIN_API_KEY"]

def call(method, path, body=None, query=None):
    url = O + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + AK}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode(errors="replace") or "")
    except urllib.error.URLError as e:
        sys.exit("FAIL  OWUI unreachable: %s (is the stack up? is KB_HOST correct?)" % e)

def jget(method, path, body=None, query=None):
    st, txt = call(method, path, body, query)
    if st != 200:
        sys.exit("FAIL  %s %s -> HTTP %s: %s" % (method, path, st, (txt or "")[:300]))
    return json.loads(txt) if txt else None

# --- A. enable sharing.public_knowledge in default permissions --------------
st, txt = call("GET", "/api/v1/users/default/permissions")
if st != 200:
    sys.exit("FAIL  GET /api/v1/users/default/permissions -> HTTP %s: %s" % (st, (txt or "")[:300]))
perms = json.loads(txt)
perm_changed = False
if not (perms.get("sharing") or {}).get("public_knowledge", False):
    perms.setdefault("sharing", {})["public_knowledge"] = True
    perm_changed = True
if perm_changed:
    st, txt = call("POST", "/api/v1/users/default/permissions", perms)
    if st != 200:
        sys.exit("FAIL  POST /api/v1/users/default/permissions -> HTTP %s: %s" % (st, (txt or "")[:300]))
    print("OK    enabled sharing.public_knowledge (default permissions)", file=sys.stderr)
else:
    print("OK    sharing.public_knowledge already enabled", file=sys.stderr)

# --- B. backfill public read (user:*) on every KB ----------------------------
def normalize(g):
    pt = g.get("principal_type") if isinstance(g, dict) else getattr(g, "principal_type", None)
    pid = g.get("principal_id") if isinstance(g, dict) else getattr(g, "principal_id", None)
    perm = g.get("permission") if isinstance(g, dict) else getattr(g, "permission", None)
    return pt, pid, perm

PAGE = 1
total = None
granted = []
already = []
failed = []
while True:
    d = jget("GET", "/api/v1/knowledge/", query={"page": PAGE})
    items = d.get("items", []) if isinstance(d, dict) else (d or [])
    total = d.get("total", total) if isinstance(d, dict) else total
    if not items:
        break
    for k in items:
        kb_id = k.get("id")
        kb_name = k.get("name", "?")
        if not kb_id:
            continue
        detail = jget("GET", "/api/v1/knowledge/%s" % kb_id)
        existing = detail.get("access_grants") or []
        grants = []
        seen = set()
        for g in existing:
            pt, pid, perm = normalize(g)
            if not pt or not pid or not perm:
                continue
            key = (pt, pid, perm)
            if key in seen:
                continue
            seen.add(key)
            grants.append({"principal_type": pt, "principal_id": pid, "permission": perm})
        if ("user", "*", "read") in seen:
            already.append(kb_name)
            continue
        grants.append({"principal_type": "user", "principal_id": "*", "permission": "read"})
        st, txt = call("POST", "/api/v1/knowledge/%s/access/update" % kb_id,
                       {"access_grants": grants})
        if st != 200:
            failed.append({"name": kb_name, "id": kb_id, "error": "HTTP %s: %s" % (st, (txt or "")[:200])})
            print("FAIL  grant on KB %s (%s) -> %s" % (kb_name, kb_id, failed[-1]["error"]), file=sys.stderr)
        else:
            granted.append(kb_name)
            print("OK    granted public read on KB %s (%s)" % (kb_name, kb_id), file=sys.stderr)
    if total is not None and len(granted) + len(already) + len(failed) >= total:
        break
    PAGE += 1
    # Guard against an unbounded loop if total is missing/wrong.
    if PAGE > 1000:
        print("WARN  stopped paginating at page 1000 (total=%s)" % total, file=sys.stderr)
        break

print(json.dumps({"permission_enabled": perm_changed,
                  "granted": granted, "already_public": already,
                  "failed": failed, "total_kbs": total}, ensure_ascii=False))
if failed:
    sys.exit(1)
PY