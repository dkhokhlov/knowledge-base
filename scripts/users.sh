#!/usr/bin/env bash
# Operator tool to manage Open WebUI KB users (admin-only). Replaces the /kb
# skill's former `user-create` admin command: admin functions are done by the
# operator via make targets (more transparent + controlled), matching the
# gdrive /index pattern (operator-only, not in the skill).
#
# Subcommands:
#   create  -> POST /admin/users (kb-gateway robust flow: create + signin +
#              genkey + verify + rollback). Args via env: EMAIL, NAME, ROLE
#              (default user). Prints the gateway response (email,
#              temp_password, kb_api_key, role, id) as pretty JSON (indent 2).
#              Relay the returned temp_password + kb_api_key to the new account
#              out-of-band ONLY; do NOT persist them (the gateway never does).
#   list    -> GET /api/v1/users/all (admin). All users. Pretty JSON (indent 2).
#   search  -> GET /api/v1/users/?query=<q>&page=1 (admin). Substring on
#              name/email. Arg via env: QUERY. Pretty JSON (indent 2).
#
# Preconditions:
#   - Stack running and healthy (`make start`).
#   - OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys).
#
# Reads OPENWEBUI_ADMIN_API_KEY (Bearer) + KB_HOST_PORT (-> KB_HOST) from the
# sourced .env / .env.local. Does NOT use the shell KB_API_KEY (the agent key
# in ~/.api_keys) — admin ops need the admin key.
#
# Usage:
#   make users-create EMAIL=alice@example.com NAME=Alice
#   make users-list
#   make users-search QUERY=agent
set -euo pipefail
cd "$(dirname "$0")/.."

# .env (config of record) + .env.local (secrets) -> exported env.
set -a
# shellcheck source=/dev/null
. ./.env
# shellcheck source=/dev/null
. ./.env.local
set +a

CMD="${1:-}"
case "$CMD" in
  create|list|search) ;;
  *) echo "Usage: $0 {create|list|search}  (or: make users-create/users-list/users-search)" >&2; exit 2;;
esac

python3 - "$CMD" <<'PY'
import json, os, sys, urllib.error, urllib.parse, urllib.request

CMD = sys.argv[1]
O = os.environ.get("KB_HOST") or ("http://localhost:%s" % os.environ.get("KB_HOST_PORT", "3000"))
KEY = os.environ.get("OPENWEBUI_ADMIN_API_KEY", "")
if not KEY:
    sys.exit("FAIL  OPENWEBUI_ADMIN_API_KEY not set in .env.local (run: make api-keys)")


def req(method, path, body=None, query=None):
    url = O + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + KEY}
    if body is not None:
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode() or "")
    except urllib.error.URLError as e:
        sys.exit("FAIL  gateway/OWUI unreachable: %s (is the stack up? is KB_HOST correct?)" % e)


def dump(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def parse_or_err(code, txt, label):
    try:
        d = json.loads(txt) if txt else None
    except Exception:
        d = None
    if code != 200:
        msg = (d.get("error") if isinstance(d, dict) else None) or (txt or "")[:300]
        sys.exit("FAIL  %s -> HTTP %s: %s" % (label, code, msg))
    return d


if CMD == "create":
    email = os.environ.get("EMAIL", "")
    name = os.environ.get("NAME", "")
    if not email or not name:
        sys.exit("FAIL  create needs EMAIL= and NAME= (e.g. make users-create EMAIL=a@b.com NAME=Alice)")
    body = {"email": email, "name": name, "role": os.environ.get("ROLE", "user")}
    code, txt = req("POST", "/admin/users", body)
    d = parse_or_err(code, txt, "POST /admin/users")
    dump(d)
    sys.stderr.write("NOTE  relay temp_password + kb_api_key to the new account "
                     "out-of-band; do NOT persist them.\n")

elif CMD == "list":
    code, txt = req("GET", "/api/v1/users/all")
    dump(parse_or_err(code, txt, "GET /api/v1/users/all"))

elif CMD == "search":
    q = os.environ.get("QUERY", "")
    if not q:
        sys.exit("FAIL  search needs QUERY= (e.g. make users-search QUERY=agent)")
    code, txt = req("GET", "/api/v1/users/", query={"query": q, "page": 1})
    dump(parse_or_err(code, txt, "GET /api/v1/users/"))
PY