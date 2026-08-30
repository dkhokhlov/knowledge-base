#!/usr/bin/env python3
"""Create the one ephemeral throwaway test user for an e2e iso-clone env.

The shell (scripts/e2e-env.sh e2e_ephemeral_user) sources the clone .env and
.env.local BEFORE it calls this script, so KB_HOST and OPENWEBUI_ADMIN_API_KEY
are in os.environ. This script does the precise work in Python, not in the
shell, so the structured gateway response is never passed through a shell
capture. The shell only checks the exit code of this process.

What this script does:
  1. POST /admin/users  ->  {email, temp_password, kb_api_key, role, id}
  2. validate HTTP 200 and kb_api_key present
  3. atomic upsert KB_API_KEY=<key> into --env-local (chmod 0600)

It mirrors scripts/users.sh `create` (the operator tool) but writes the key to
.env.local instead of printing JSON for a human to relay. The two are different
consumers: this one is automation; users.sh is an operator tool.

It exits non-zero on any failure (HTTP not 200, missing kb_api_key, write
error). Then the shell `|| return 1` fires with a clear cause.
"""
import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request


def _fail(msg):
    sys.stderr.write("FAIL  " + msg + "\n")
    sys.exit(1)


def _post_admin_user(kb_host, admin_key, email, name, role):
    url = kb_host.rstrip("/") + "/admin/users"
    body = json.dumps({"email": email, "name": name, "role": role}).encode()
    headers = {
        "Authorization": "Bearer " + admin_key,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode() or "")
    except urllib.error.URLError as e:
        _fail("gateway/OWUI unreachable: %s (is the stack up? is KB_HOST correct?)" % e)


def _upsert_env_key(path, key):
    """Atomic upsert of KB_API_KEY=<key> into a .env-style file (chmod 0600)."""
    prefix = "KB_API_KEY="
    lines = open(path).read().splitlines() if os.path.exists(path) else []
    seen = False
    out = []
    for ln in lines:
        if ln.startswith(prefix):
            out.append(prefix + key)
            seen = True
        else:
            out.append(ln)
    if not seen:
        out.append(prefix + key)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".", prefix=".env.", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write("\n".join(out) + "\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main():
    ap = argparse.ArgumentParser(
        description="Create the e2e ephemeral test user and write its KB_API_KEY to .env.local"
    )
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--role", default="user")
    ap.add_argument(
        "--env-local",
        default=".env.local",
        help="path to .env.local to upsert KB_API_KEY into",
    )
    args = ap.parse_args()

    kb_host = os.environ.get("KB_HOST")
    if not kb_host:
        _fail("KB_HOST not set (the shell must source .env before it calls this script)")
    admin_key = os.environ.get("OPENWEBUI_ADMIN_API_KEY", "")
    if not admin_key:
        _fail(
            "OPENWEBUI_ADMIN_API_KEY not set (the shell must source .env.local "
            "before it calls this script; run make api-keys)"
        )

    code, txt = _post_admin_user(kb_host, admin_key, args.email, args.name, args.role)
    try:
        d = json.loads(txt) if txt else None
    except Exception:
        d = None
    if code != 200:
        msg = (d.get("error") if isinstance(d, dict) else None) or (txt or "")[:300]
        _fail("POST /admin/users -> HTTP %s: %s" % (code, msg))
    if not isinstance(d, dict):
        _fail("POST /admin/users -> 200 but the body is not a JSON object: %r" % (txt or "")[:300])
    ukey = d.get("kb_api_key") or ""
    if not ukey:
        _fail("POST /admin/users -> 200 but no kb_api_key in the response: %s" % (txt or "")[:300])

    try:
        _upsert_env_key(args.env_local, ukey)
    except Exception as e:
        _fail("write KB_API_KEY to %s failed: %s" % (args.env_local, e))

    print("OK    ephemeral user %s -> KB_API_KEY written to %s" % (args.email, args.env_local))


if __name__ == "__main__":
    main()