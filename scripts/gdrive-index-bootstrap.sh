#!/usr/bin/env bash
# Provision the Open WebUI "gdrive" knowledge base that kb-gateway indexes the
# local ./gdrive tree into (stateless POST /index, no sidecar). The gateway
# references a KB by id and does NOT create KBs, so this script:
#   1. finds or creates a KB named "gdrive" (idempotent);
#   2. grants the agent user read access so the read-scoped agent key
#      (OPENWEBUI_USER_API_KEY) can search / RAG the KB. The agent user id is
#      resolved FROM that key via GET /api/v1/auths/ (the same tamper-proof
#      identity-from-key pattern the kb-gateway uses — no email env var needed);
#      the grant is merged with any existing grants so admin-added group grants
#      are preserved;
#   3. writes GDRIVE_KB_ID into .env.local.
#
# Preconditions:
#   - Stack running and healthy (`make start`).
#   - OPENWEBUI_ADMIN_API_KEY + OPENWEBUI_USER_API_KEY in .env.local
#     (provisioned by `make api-keys`).
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
: "${OPENWEBUI_USER_API_KEY:?FAIL  OPENWEBUI_USER_API_KEY not set in .env.local (run: make api-keys)}"

# Steps 1-2 run in Python (stdlib urllib); they print GDRIVE_KB_ID as the last
# stdout line (stderr carries the human progress log).
read -r KB_ID < <(python3 - <<'PY'
import os, json, urllib.request, urllib.error, sys

O = os.environ.get("KB_HOST") or ("http://localhost:%s" % os.environ.get("KB_HOST_PORT", "3000"))
AK = os.environ["OPENWEBUI_ADMIN_API_KEY"]
UK = os.environ["OPENWEBUI_USER_API_KEY"]
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

# --- resolve the agent user id FROM its own key (tamper-proof, no email var) --
# GET /api/v1/auths/ with the user key returns the key owner's {id,email,role}.
me = jget("GET", "/api/v1/auths/", token=UK)
agent_uid = (me or {}).get("id")
if not agent_uid:
    sys.exit("FAIL  could not resolve agent user id from OPENWEBUI_USER_API_KEY via /api/v1/auths/")
agent_email = (me or {}).get("email", "?")

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
             {"name": KB_NAME, "description": "Indexed from local gdrive/ via kb-gateway"})
    kb_id = d["id"]
    print("OK    created KB %s: %s" % (KB_NAME, kb_id), file=sys.stderr)

# --- grant the agent user READ, merged with existing grants ------------------
# access/update REPLACES the full grant set, so read current grants and re-post
# the union (normalized to the three client fields) to avoid clobbering any
# admin-added group grants.
kb_detail = jget("GET", "/api/v1/knowledge/%s" % kb_id)
existing = kb_detail.get("access_grants") or []
grants = []
seen = set()
for g in existing:
    pt, pid, perm = g.get("principal_type"), g.get("principal_id"), g.get("permission")
    if not pt or not pid or not perm:
        continue
    key = (pt, pid)
    if key in seen:
        continue
    seen.add(key)
    grants.append({"principal_type": pt, "principal_id": pid, "permission": perm})
need = ("user", agent_uid)
if need not in seen:
    grants.append({"principal_type": "user", "principal_id": agent_uid, "permission": "read"})
    print("OK    granting agent user %s read on KB %s" % (agent_email, kb_id), file=sys.stderr)
else:
    print("OK    agent user %s already has access on KB %s" % (agent_email, kb_id), file=sys.stderr)
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

printf '\nDone. gdrive KB id: %s\n' "$KB_ID"
printf 'Index the tree: make gdrive-sync   |   status: make gdrive-status\n'