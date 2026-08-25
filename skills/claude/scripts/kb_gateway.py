#!/usr/bin/env python3
"""Thin REST client for the kb-gateway (Graphiti memory).

Zero dependencies (Python 3.10+ stdlib). The agent holds only KB_API_KEY (an
Open WebUI key) + KB_HOST — nothing else. All authorization is done server-side
by the kb-gateway (identity + role are derived from the key by the gateway; the
CLI cannot influence them). Works on any host that can reach KB_HOST.

The gateway is fronted by Caddy at KB_HOST: memory/facts under /memory/*,
aggregated health at /health. OWUI REST is at the same KB_HOST root (/api/*).
One URL, one key.

Config: the wrapper is a thin client. It reads ONLY two env vars from the
shell environment — KB_HOST and KB_API_KEY. It does not read .env / .env.local
files (set both in your shell before invoking it).

Env vars: KB_HOST, KB_API_KEY.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def base_url():
    if os.environ.get("KB_HOST"):
        return os.environ["KB_HOST"].rstrip("/")
    sys.exit("FAIL  no KB_HOST: set KB_HOST in your shell env "
             "(e.g. export KB_HOST=http://localhost:3000)")


def api_key():
    if os.environ.get("KB_API_KEY"):
        return os.environ["KB_API_KEY"]
    sys.exit("FAIL  no API key: set KB_API_KEY in your shell env")


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


def main():
    p = argparse.ArgumentParser(
        prog="kb_gateway.py",
        description="Thin REST client for the kb-gateway (Graphiti memory). "
                    "Authorized server-side via KB_API_KEY.",
    )
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

    a = p.parse_args()
    base = base_url()
    key = api_key()

    {
        "whoami": cmd_whoami, "groups": cmd_groups, "add": cmd_add, "search": cmd_search,
        "episodes": cmd_episodes, "status": cmd_status, "forget": cmd_forget,
        "delete-edge": cmd_delete_edge, "delete-episode": cmd_delete_episode,
    }[a.cmd](base, key, a)


if __name__ == "__main__":
    main()