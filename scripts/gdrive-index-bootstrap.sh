#!/usr/bin/env bash
# Provision the Open WebUI "gdrive" knowledge base that api-gateway indexes the
# local ./gdrive tree into (stateless POST /index, no sidecar). The gateway
# references a KB by id and does NOT create KBs, so this script:
#   1. finds or creates a KB named "gdrive" (idempotent);
#   2. grants PUBLIC READ (user:*) so every authenticated user (any account on
#      this instance) can search / RAG the KB — the chosen public-read model. The
#      admin key makes the call (bypasses the sharing.public_knowledge filter),
#      so no per-user permission is needed here; the grant is merged with any
#      existing grants (deduped by (principal_type, principal_id, permission))
#      so admin-added group grants are preserved;
#   3. writes GDRIVE_KB_ID into .env.local.
#
# Preconditions:
#   - Stack running and healthy (`make start`).
#   - OPENWEBUI_ADMIN_API_KEY in .env.local (provisioned by `make api-keys`).
#
# Idempotent: re-running re-asserts the grant and refreshes the same KB id.
# Indexing itself is manual: run `make gdrive-sync` (rclone + POST /index) to
# populate the KB; `make gdrive-status` reads GET /status.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck source=/dev/null
. ./.env
# shellcheck source=/dev/null
. ./.env.local
set +a

: "${OPENWEBUI_ADMIN_API_KEY:?FAIL  OPENWEBUI_ADMIN_API_KEY not set in .env.local (run: make api-keys)}"

# Steps 1-2 run in Python (stdlib urllib); they print GDRIVE_KB_ID as the last
# stdout line (stderr carries the human progress log).
read -r KB_ID < <(python3 - <<'PY'
import os, json, urllib.request, urllib.error, sys

_kb_host = os.environ.get("KB_HOST")
if not _kb_host:
    sys.exit("FAIL  KB_HOST not set -- export KB_HOST=http://<host>:<port> (see .env.template)")
O = _kb_host.rstrip("/")
AK = os.environ["OPENWEBUI_ADMIN_API_KEY"]
KB_NAME = "gdrive"

def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    headers["Authorization"] = "Bearer " + (token or AK)
    req = urllib.request.Request(O + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        sys.exit("FAIL  %s %s -> HTTP %s: %s" % (method, path, e.code, (e.read().decode() or "")[:300]))

def jget(method, path, body=None, token=None):
    st, txt = call(method, path, body, token=token)
    if st != 200:
        sys.exit("FAIL  %s %s -> HTTP %s: %s" % (method, path, st, txt[:300]))
    return json.loads(txt) if txt else None

# --- find or create the "gdrive" KB ------------------------------------------
st, txt = call("GET", "/api/v1/knowledge/")
if st != 200:
    sys.exit("FAIL  GET /api/v1/knowledge/ -> HTTP %s: %s" % (st, txt[:300]))
items = json.loads(txt)
items = items.get("items", []) if isinstance(items, dict) else items
kb = next((k for k in items if k.get("name") == KB_NAME), None)
if kb:
    kb_id = kb["id"]
    print("OK    KB %s already exists: %s" % (KB_NAME, kb_id), file=sys.stderr)
else:
    d = jget("POST", "/api/v1/knowledge/create",
             {"name": KB_NAME, "description": "Indexed from local gdrive/ via api-gateway"})
    kb_id = d["id"]
    print("OK    created KB %s: %s" % (KB_NAME, kb_id), file=sys.stderr)

# --- grant PUBLIC READ (user:*), merged with existing grants ------------------
# access/update REPLACES the full grant set, so read current grants and re-post
# the union (deduped by the full (principal_type, principal_id, permission)
# tuple) to avoid clobbering any admin-added group grants. user:* grants read
# to every AUTHENTICATED user (not internet-public; OWUI's 'anyone' principal is
# not used). The admin key bypasses the sharing.public_knowledge filter, so no
# permission is required to set this here.
kb_detail = jget("GET", "/api/v1/knowledge/%s" % kb_id)
existing = kb_detail.get("access_grants") or []
grants = []
seen = set()
for g in existing:
    pt, pid, perm = g.get("principal_type"), g.get("principal_id"), g.get("permission")
    if not pt or not pid or not perm:
        continue
    key = (pt, pid, perm)
    if key in seen:
        continue
    seen.add(key)
    grants.append({"principal_type": pt, "principal_id": pid, "permission": perm})
pub = ("user", "*", "read")
if pub not in seen:
    grants.append({"principal_type": "user", "principal_id": "*", "permission": "read"})
    print("OK    granting public read (user:*) on KB %s" % kb_id, file=sys.stderr)
else:
    print("OK    public read (user:*) already granted on KB %s" % kb_id, file=sys.stderr)
jget("POST", "/api/v1/knowledge/%s/access/update" % kb_id, {"access_grants": grants})

# Final line: the value the bash caller captures.
print(kb_id)
PY
)

# Step 3: write GDRIVE_KB_ID into .env.local (idempotent, preserving other lines
# + comments). Value is a UUID (no shell metachars), so a plain KEY=value line
# is safe.
update_env_local() {
  local key="$1" val="$2"
  if grep -q "^${key}=" .env.local; then
    python3 - "$key" "$val" <<'PY'
import sys, os
key, val = sys.argv[1], sys.argv[2]
f = ".env.local"
out = []
seen = False
for ln in open(f).read().splitlines():
    if ln.startswith(key + "="):
        out.append(key + "=" + val); seen = True
    else:
        out.append(ln)
if not seen:
    out.append(key + "=" + val)
open(f, "w").write("\n".join(out) + "\n")
os.chmod(f, 0o600)
PY
  else
    printf '%s=%s\n' "$key" "$val" >> .env.local
  fi
  chmod 600 .env.local
}

update_env_local GDRIVE_KB_ID "$KB_ID"
printf 'OK    wrote GDRIVE_KB_ID to .env.local\n'

# Recreate api-gateway so it picks up OPENWEBUI_ADMIN_API_KEY + GDRIVE_KB_ID from
# .env.local (the gateway holds the admin key for /index writes + defaults
# kb_id from GDRIVE_KB_ID). `make start` launched it before these existed; a
# bare restart would NOT re-read compose env, so force-recreate is required.
# Re-source .env.local (just written) so the interpolated values are current.
set -a; . ./.env 2>/dev/null || true; . ./.env.local 2>/dev/null || true; set +a
docker compose up -d --no-deps --force-recreate api-gateway >/dev/null
printf 'OK    recreated api-gateway (admin key + GDRIVE_KB_ID now in env)\n'

printf '\nDone. gdrive KB id: %s\n' "$KB_ID"
printf 'Index the tree: make gdrive-sync   |   status: make gdrive-status\n'