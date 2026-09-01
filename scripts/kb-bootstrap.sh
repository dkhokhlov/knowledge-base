#!/usr/bin/env bash
# Provision (or resolve) an Open WebUI knowledge base that api-gateway indexes a
# local ./root/<name>/ tree into (stateless POST /index, no sidecar). The gateway
# references a KB by id and does NOT create KBs, so this script:
#   1. finds or creates a KB named <name> (idempotent);
#   2. grants PUBLIC READ (user:*) so every authenticated user (any account on
#      this instance) can search / RAG the KB -- the chosen public-read model. The
#      admin key makes the call (bypasses the sharing.public_knowledge filter),
#      so no per-user permission is needed here; the grant is merged with any
#      existing grants (deduped by (principal_type, principal_id, permission))
#      so admin-added group grants are preserved.
# Resolution is by NAME (the gateway is stateless; no *_KB_ID env is written). The
# OWUI knowledge list is paginated, so the lookup paginates to exhaustion and is
# unique-or-fail: 0 matches in --resolve = "run make kb-bootstrap"; >1 = ambiguous
# (OWUI does not enforce unique names). The gdrive KB is just <name>=gdrive.
#
# Modes:
#   KB=<name> ./kb-bootstrap.sh             bootstrap (find-or-create + grant) one KB
#   ./kb-bootstrap.sh                       bootstrap EVERY top-level non-dot subdir of ./root
#   KB=<name> ./kb-bootstrap.sh --resolve   resolve only: print the kb_id on stdout
#                                           (fail-fast on 0 or >1 matches; no create/grant)
#
# Preconditions:
#   - Stack running and healthy (`make start`).
#   - OPENWEBUI_ADMIN_API_KEY in .env.local (provisioned by `make api-keys`).
#   - KB_HOST set (shell-sourced; see .env.template).
#
# Idempotent: re-running re-asserts the grant and prints the same kb_id.
# Indexing itself is manual: run `make kb-sync` (or `make gdrive-sync` for the
# gdrive KB) to populate; `make kb-status` reads GET /status.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck source=/dev/null
. ./.env
# shellcheck source=/dev/null
. ./.env.local
set +a

: "${OPENWEBUI_ADMIN_API_KEY:?FAIL  OPENWEBUI_ADMIN_API_KEY not set in .env.local (run: make api-keys)}"
: "${KB_HOST:?FAIL  KB_HOST not set -- export KB_HOST=http://<host>:<port> (see .env.template)}"

RESOLVE=0
for a in "$@"; do
  case "$a" in
    --resolve) RESOLVE=1 ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# //; s/^#$//'; exit 0 ;;
    *) echo "FAIL  unknown arg: $a (use --resolve)" >&2; exit 2 ;;
  esac
done
export RESOLVE

# The Python block paginates GET /api/v1/knowledge/ to exhaustion (one pass),
# then per name either resolves (print id, unique-or-fail) or bootstraps
# (find-or-create + grant). For a single named bootstrap (or --resolve), the
# final stdout line is the kb_id (stderr carries the human progress log);
# bootstrap-all prints a per-KB report on stderr only.
python3 - <<'PY'
import os, json, sys, urllib.request, urllib.error, urllib.parse

O = os.environ["KB_HOST"].rstrip("/")
AK = os.environ["OPENWEBUI_ADMIN_API_KEY"]
RESOLVE = os.environ.get("RESOLVE") == "1"
NAME = os.environ.get("KB", "").strip()
import os as _os
ROOT = _os.path.join(_os.getcwd(), "root")

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

# --- paginate the full KB list ONCE ------------------------------------------
all_kbs = []
page = 1
while True:
    d = jget("GET", "/api/v1/knowledge/", query={"page": page})
    items = d.get("items", []) if isinstance(d, dict) else (d or [])
    if not items:
        break
    all_kbs.extend(items)
    total = d.get("total") if isinstance(d, dict) else None
    if total is not None and len(all_kbs) >= total:
        break
    page += 1
    if page > 1000:
        print("WARN  stopped paginating at page 1000 (total=%s)" % total, file=sys.stderr)
        break

def matches_for(name):
    return [k for k in all_kbs if k.get("name") == name]

def grant_public_read(kb_id, name):
    detail = jget("GET", "/api/v1/knowledge/%s" % kb_id)
    existing = detail.get("access_grants") or []
    grants, seen = [], set()
    for g in existing:
        pt, pid, perm = g.get("principal_type"), g.get("principal_id"), g.get("permission")
        if not pt or not pid or not perm:
            continue
        key = (pt, pid, perm)
        if key in seen:
            continue
        seen.add(key); grants.append({"principal_type": pt, "principal_id": pid, "permission": perm})
    if ("user", "*", "read") in seen:
        print("OK    public read (user:*) already granted on KB %s (%s)" % (name, kb_id), file=sys.stderr)
        return
    grants.append({"principal_type": "user", "principal_id": "*", "permission": "read"})
    jget("POST", "/api/v1/knowledge/%s/access/update" % kb_id, {"access_grants": grants})
    print("OK    granted public read (user:*) on KB %s (%s)" % (name, kb_id), file=sys.stderr)

# --- decide the name set ------------------------------------------------------
if NAME:
    names = [NAME]
else:
    if RESOLVE:
        sys.exit("FAIL  --resolve requires KB=<name>")
    if not _os.path.isdir(ROOT):
        sys.exit("FAIL  ./root not found (run: make kb-migrate-root, or create ./root/<name>/)")
    names = sorted(d for d in _os.listdir(ROOT)
                   if not d.startswith(".") and _os.path.isdir(_os.path.join(ROOT, d)))
    if not names:
        sys.exit("FAIL  no top-level subdirs under ./root (drop a folder at ./root/<name>/)")

single = len(names) == 1
exit_code = 0
last_id = None
for name in names:
    ms = matches_for(name)
    if RESOLVE:
        if len(ms) == 0:
            print("FAIL  no KB named %r -- run: make kb-bootstrap KB=%s" % (name, name), file=sys.stderr)
            exit_code = 1; continue
        if len(ms) > 1:
            ids = ", ".join(k.get("id", "?") for k in ms)
            print("FAIL  ambiguous: %d KBs named %r (%s) -- rename duplicates in OWUI" % (len(ms), name, ids), file=sys.stderr)
            exit_code = 1; continue
        kb_id = ms[0]["id"]
        print("OK    resolved KB %s -> %s" % (name, kb_id), file=sys.stderr)
        last_id = kb_id
        if single:
            print(kb_id)  # final stdout line for capture
        continue
    # bootstrap mode
    if len(ms) > 1:
        ids = ", ".join(k.get("id", "?") for k in ms)
        print("FAIL  ambiguous: %d KBs named %r (%s) -- rename duplicates before bootstrap" % (len(ms), name, ids), file=sys.stderr)
        exit_code = 1; continue
    if ms:
        kb_id = ms[0]["id"]
        print("OK    KB %s already exists: %s" % (name, kb_id), file=sys.stderr)
    else:
        d = jget("POST", "/api/v1/knowledge/create",
                 {"name": name, "description": "Indexed from local root/%s/ via api-gateway" % name})
        kb_id = d["id"]
        print("OK    created KB %s: %s" % (name, kb_id), file=sys.stderr)
    grant_public_read(kb_id, name)
    last_id = kb_id
    if single:
        print(kb_id)  # final stdout line for capture

if exit_code:
    sys.exit(exit_code)
if single and last_id is None:
    sys.exit("FAIL  no kb_id produced")
PY