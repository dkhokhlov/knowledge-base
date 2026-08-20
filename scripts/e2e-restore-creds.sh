#!/usr/bin/env bash
# Restore admin (and optional agent) credentials into .env.local after a
# `make clear-all` + `make bootstrap` wiped them. `make bootstrap` recreates
# .env.local from .env.local.example with a fresh WEBUI_SECRET_KEY and EMPTY
# OPENWEBUI_TEST_USER/PASSWORD; this script puts the stashed admin creds back
# so `make admin-signup` + `make api-keys` can sign in.
#
# In-place key replacement (not append): .env.local.example's last line has no
# trailing newline, so a bare `cat stash >> .env.local` would glue the first
# stashed line onto it. Replacing ^KEY=.* lines avoids that and keeps the
# freshly generated WEBUI_SECRET_KEY.
#
# Usage: e2e-restore-creds.sh <stash-file>
#   stash-file = 0600 file with KEY=VALUE lines (OPENWEBUI_TEST_USER,
#                OPENWEBUI_TEST_PASSWORD, optional OPENWEBUI_USER).
set -euo pipefail
cd "$(dirname "$0")/.."

stash="${1:?stash file required}"
[ -f .env.local ] || { echo "FAIL  no .env.local to restore into (run make bootstrap first)" >&2; exit 1; }
[ -f "$stash" ]  || { echo "FAIL  stash file not found: $stash" >&2; exit 1; }

python3 - "$stash" <<'PY'
import os, sys
stash, env = sys.argv[1], ".env.local"
vals = {}
with open(stash) as f:
    for line in f:
        line = line.rstrip("\n")
        if "=" in line:
            k, v = line.split("=", 1)
            if v:
                vals[k] = v
with open(env) as f:
    lines = f.read().split("\n")
seen = set()
for i, ln in enumerate(lines):
    for k, v in vals.items():
        if ln.startswith(k + "="):
            lines[i] = "%s=%s" % (k, v)
            seen.add(k)
for k, v in vals.items():
    if k not in seen:
        lines.append("%s=%s" % (k, v))
with open(env, "w") as f:
    f.write("\n".join(lines))
os.chmod(env, 0o600)
print("OK    restored into .env.local: %s" % ", ".join(sorted(vals)))
PY