#!/usr/bin/env bash
# One-time backfill: write the source-attribute kv into the KB `description` of
# existing KBs created before the source-attribute landed.
#
# OWUI's REST API cannot write the KB `meta` JSONB field (KnowledgeForm has no
# `meta`; create/update silently drop it), so the source attribute lives in the
# writable `description` as `<prose lead> | <kv>`. `kb kbs` parses the kv. New KBs
# get the kv at create time (kb-bootstrap.sh for root; kb.py for projects). This
# script backfills the OLD KBs that have only the prose lead (or the legacy kv
# order without a `source=` token).
#
# Idempotent + non-destructive:
#   - SKIP any KB whose description already carries a `source=` kv (the
#     idempotency guard — never clobber an already-migrated description).
#   - For a root KB (prose `Indexed from local root/<name>/ via api-gateway`):
#     append `| source=root | host=<host> | path=<name>`. A pre-migration root
#     KB has `Indexed from local <name>/ via kb-gateway` (no `root/` segment);
#     it is recognized as root and rebuilt to the canonical `root/<name>/` form.
#   - For a projects KB (`Claude projects memory | repo=.. | host=.. | project=..
#     | path=..`): insert `source=projects-memory` and rebuild the kv in the
#     canonical order, reusing the existing kvs (host falls back to this host).
#   - Unparseable description -> skip + log (do not fabricate).
#   - `access_grants` is NOT clobbered: /knowledge/{id}/update uses
#     exclude={'access_grants'}, so the user:* public-read grant survives.
#
# Side effect: each description write re-embeds the KB metadata vector
# (OWUI embed_knowledge_base_metadata embeds f'{name}\n{description}'). The
# embed is wrapped (a failure logs + returns, it does NOT fail the update), so
# the description commits to SQLite even if Ollama is down (the vector catches up
# on the next embed). Keeping the prose lead keeps that embedding meaningful.
# Run on the stack host (host IS knowable there).
#
# Preconditions:
#   - Stack running + healthy.
#   - OPENWEBUI_ADMIN_API_KEY in .env.local (provisioned by `make api-keys`).
#   - KB_HOST set (shell-sourced; see .env.template).
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck source=/dev/null
. ./.env
# shellcheck source=/dev/null
. ./.env.local 2>/dev/null || true
set +a

: "${KB_HOST:?FAIL  KB_HOST not set -- export KB_HOST=http://<host>:<port> (see .env.template)}"
: "${OPENWEBUI_ADMIN_API_KEY:?FAIL  OPENWEBUI_ADMIN_API_KEY not set in .env.local (run: make api-keys)}"

python3 - <<'PY'
import os, json, sys, socket, urllib.request, urllib.error, urllib.parse

O = os.environ["KB_HOST"].rstrip("/")
AK = os.environ["OPENWEBUI_ADMIN_API_KEY"]
HOST = (socket.gethostname() or "unknown").split(".")[0]

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

def jput(method, path, body):
    st, txt = call(method, path, body)
    if st != 200:
        return False, "HTTP %s: %s" % (st, (txt or "")[:200])
    return True, ""

def parse_kv(desc):
    kv = {}
    for tok in (desc or "").split("|"):
        tok = tok.strip()
        if "=" in tok:
            k, _, v = tok.partition("=")
            kv[k.strip()] = v.strip()
    return kv

def build_root_desc(name):
    return ("Indexed from local root/%s/ via api-gateway | source=root | host=%s | path=%s"
            % (name, HOST, name))

def build_projects_desc(name, desc):
    # Reuse the existing kvs; canonical order with source first. host falls back
    # to this host (a projects KB's host is the machine that indexed it).
    kv = parse_kv(desc)
    return ("Claude projects memory | source=projects-memory | host=%s | project=%s | repo=%s | path=%s"
            % (kv.get("host") or HOST, kv.get("project", ""), kv.get("repo", ""),
               kv.get("path", "")))

# Paginate the full KB list.
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

updated, skipped_migrated, skipped_unparseable, failed = 0, 0, 0, 0
for k in all_kbs:
    kid = k.get("id")
    name = k.get("name", "?")
    desc = k.get("description") or ""
    kv = parse_kv(desc)
    if "source" in kv:
        print("SKIP  %s (%s) already has source=%s" % (name, kid, kv["source"]), file=sys.stderr)
        skipped_migrated += 1
        continue
    if desc.startswith("Indexed from local root/") or desc.startswith("Indexed from local "):
        # Both the new-migration form (`local root/<name>/`) and the pre-migration
        # form (`local <name>/ via kb-gateway`, no `root/`) are root KBs.
        new_desc = build_root_desc(name)
    elif desc.startswith("Claude projects memory"):
        new_desc = build_projects_desc(name, desc)
    else:
        print("SKIP  %s (%s) unparseable description: %r" % (name, kid, desc[:120]),
              file=sys.stderr)
        skipped_unparseable += 1
        continue
    ok, err = jput("POST", "/api/v1/knowledge/%s/update" % kid,
                   {"name": name, "description": new_desc})
    if not ok:
        print("FAIL  %s (%s) update failed: %s" % (name, kid, err), file=sys.stderr)
        failed += 1
        continue
    print("OK    %s (%s) -> %s" % (name, kid, new_desc), file=sys.stderr)
    updated += 1

print("DONE  backfill: updated=%d skipped(migrated)=%d skipped(unparseable)=%d failed=%d (total=%d)"
      % (updated, skipped_migrated, skipped_unparseable, failed, len(all_kbs)))
PY