#!/usr/bin/env python3
"""Thin REST client for the kb-gateway (Graphiti memory + admin user provisioning).

Zero dependencies (Python 3.10+ stdlib). The agent holds only KB_API_KEY (an
Open WebUI key) + KB_HOST — nothing else. All authorization is done server-side
by the kb-gateway (identity + role are derived from the key by the gateway; the
CLI cannot influence them). Works on any host that can reach KB_HOST.

The gateway is fronted by Caddy at KB_HOST: memory/facts under /memory/*, admin
provisioning at POST /admin/users, aggregated health at /health. OWUI REST is at
the same KB_HOST root (/api/*). One URL, one key.

Config resolution priority (same as owui.py):
    CLI flags  >  environment variables  >  --env-file (repeatable; e.g. .env then .env.local)

Env vars: KB_HOST (or KB_HOST_PORT), KB_API_KEY
          (fallback OPENWEBUI_USER_API_KEY).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def load_env_file(path):
    """Parse KEY=VALUE lines from a .env-style file into os.environ, OVERRIDING
    any inherited value (explicit --env-file wins over the shell env; a later
    --env-file wins over an earlier one). No ${} interpolation (values literal)."""
    if not path or not os.path.exists(path):
        return
    with open(path) as f:
        for ln in f:
            s = ln.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip()
            if v and v[0] in ("'", '"'):
                q = v[0]
                end = v.find(q, 1)
                v = v[1:end] if end != -1 else v[1:]
            elif "#" in v:
                v = v.split("#", 1)[0].strip()
            os.environ[k] = v


def base_url(args):
    if args.base_url:
        return args.base_url.rstrip("/")
    if os.environ.get("KB_HOST"):
        return os.environ["KB_HOST"].rstrip("/")
    if os.environ.get("KB_HOST_PORT"):
        return "http://localhost:%s" % os.environ["KB_HOST_PORT"]
    sys.exit("FAIL  no KB_HOST: pass --base-url or set KB_HOST "
             "(or KB_HOST_PORT, or --env-file)")


def api_key(args):
    if args.key:
        return args.key
    if os.environ.get("KB_API_KEY"):
        return os.environ["KB_API_KEY"]
    if os.environ.get("OPENWEBUI_USER_API_KEY"):
        return os.environ["OPENWEBUI_USER_API_KEY"]
    sys.exit("FAIL  no API key: pass --key or set KB_API_KEY (or --env-file)")


def call(base, key, method, path, body=None, query=None):
    url = base + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + key}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode() or "")
    except urllib.error.URLError as e:
        sys.exit("FAIL  gateway unreachable: %s (is KB_HOST correct? "
                 "is the stack up?)" % e)


def jget(base, key, method, path, body=None, query=None):
    """Call the gateway; exit non-zero with the gateway's message on non-200.
    Returns the parsed JSON on 200."""
    code, txt = call(base, key, method, path, body, query)
    try:
        data = json.loads(txt) if txt else None
    except Exception:
        data = None
    if code != 200:
        msg = (data.get("error") if isinstance(data, dict) else None) or txt[:300]
        sys.exit("FAIL  %s %s -> HTTP %s: %s" % (method, path, code, msg))
    return data


# --- subcommands -------------------------------------------------------------

def cmd_whoami(base, key, a):
    d = jget(base, key, "GET", "/memory/whoami")
    print(json.dumps({"email": d.get("email"), "role": d.get("role"),
                      "id": d.get("id")}))


def cmd_groups(base, key, a):
    d = jget(base, key, "GET", "/memory/groups")
    print(json.dumps({"groups": d.get("groups", [])}))


def cmd_add(base, key, a):
    body = {"text": a.text, "name": a.name}
    if a.group:
        body["group"] = a.group
    if a.source_description:
        body["source_description"] = a.source_description
    d = jget(base, key, "POST", "/memory/add", body)
    print(json.dumps(d))


def cmd_search(base, key, a):
    d = jget(base, key, "POST", "/memory/search", {"query": a.query, "k": a.k})
    print(json.dumps({"facts": d.get("facts", [])}))


def cmd_episodes(base, key, a):
    d = jget(base, key, "GET", "/memory/episodes", query={"max": a.max})
    print(json.dumps({"episodes": d.get("episodes", [])}))


def cmd_status(base, key, a):
    d = jget(base, key, "GET", "/memory/status")
    print(json.dumps({"status": d.get("status")}))


def cmd_forget(base, key, a):
    d = jget(base, key, "POST", "/memory/forget", {"group": a.group})
    print(json.dumps(d))


def cmd_delete_edge(base, key, a):
    d = jget(base, key, "POST", "/memory/delete-edge", {"uuid": a.uuid})
    print(json.dumps(d))


def cmd_delete_episode(base, key, a):
    d = jget(base, key, "POST", "/memory/delete-episode", {"uuid": a.uuid})
    print(json.dumps(d))


def cmd_user_create(base, key, a):
    """Admin: create a new KB user. The gateway returns the new user's email,
    a generated temp password, and their KB_API_KEY. Relay these to the
    requesting human administrator ONLY — do not persist them. Output is the
    raw gateway response (compact JSON)."""
    body = {"email": a.email, "name": a.name}
    if a.role:
        body["role"] = a.role
    d = jget(base, key, "POST", "/admin/users", body)
    print(json.dumps(d))


def main():
    p = argparse.ArgumentParser(
        prog="kb_gateway.py",
        description="Thin REST client for the kb-gateway (Graphiti memory + "
                    "admin user provisioning). Authorized server-side via KB_API_KEY.",
    )
    p.add_argument("--base-url", help="KB_HOST URL (e.g. http://localhost:3000)")
    p.add_argument("--key", help="API key (KB_API_KEY)")
    p.add_argument("--env-file", action="append", default=[],
                   help=".env-style file to load KB_HOST + key from (repeatable)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="print the key's email + role (identity from the gateway)")
    sub.add_parser("groups", help="list all groups that currently have data (read-all)")

    sp = sub.add_parser("add", help="add a memory to YOUR personal group (user:<email>)")
    sp.add_argument("text"); sp.add_argument("--name", default="kb-memory")
    sp.add_argument("--group", help="only your own personal group_id is accepted; any other group -> 403 (no shared write groups). Default: your personal user:<email>.")
    sp.add_argument("--source-description", help="optional source description")

    sp = sub.add_parser("search", help="search facts across ALL groups (read-only)")
    sp.add_argument("query"); sp.add_argument("--k", type=int, default=10)

    sp = sub.add_parser("episodes", help="list episodes across ALL groups (read-only)")
    sp.add_argument("--max", type=int, default=10)

    sub.add_parser("status", help="graphiti server + DB status (read-only, global)")

    sp = sub.add_parser("forget", help="clear a group's memory (owner or admin only)")
    sp.add_argument("group")

    sp = sub.add_parser("delete-edge", help="delete one entity edge by uuid (owner/admin)")
    sp.add_argument("uuid")
    sp = sub.add_parser("delete-episode", help="delete one episode by uuid (owner/admin)")
    sp.add_argument("uuid")

    sp = sub.add_parser("user-create", help="ADMIN: create a new KB user + issue its KB_API_KEY")
    sp.add_argument("--email", required=True); sp.add_argument("--name", required=True)
    sp.add_argument("--role", default="user", help="role (default user; admin only may call)")

    a = p.parse_args()
    for ef in a.env_file:
        load_env_file(ef)
    base = base_url(a)
    key = api_key(a)

    {
        "whoami": cmd_whoami, "groups": cmd_groups, "add": cmd_add, "search": cmd_search,
        "episodes": cmd_episodes, "status": cmd_status, "forget": cmd_forget,
        "delete-edge": cmd_delete_edge, "delete-episode": cmd_delete_episode,
        "user-create": cmd_user_create,
    }[a.cmd](base, key, a)


if __name__ == "__main__":
    main()