#!/usr/bin/env bash
# ONE-TIME migration to the ./root/ source root (idempotent, fail-fast).
#
# Before this commit the framework indexed exactly one source: Google Drive,
# rcloned into ./gdrive/, reconciled into a "gdrive" KB via POST /index?source=gdrive.
# Now every top-level subdir of ./root/ is one KB (named after the subdir); the
# gdrive flow is ./root/gdrive/. This script moves an EXISTING live deployment from
# the old layout to the new one:
#
#   ./gdrive/                 -> ./root/gdrive/                 (gitignored corpus)
#   gdrive-exclude.conf       -> ./root/.exclude.conf           (section headers re-prefixed)
#   .env.local: GDRIVE_KB_ID  -> removed                        (name-based resolution)
#   compose mount ./gdrive:/gdrive -> ./root:/kb-source:ro      (already in the new compose.yml)
#
# Section rewrite in .exclude.conf: [*] stays VERBATIM (it is the global deny-list,
# applies to every KB); every per-drive [X] becomes [gdrive/X] (root-relative under
# ./root/). Comment lines and pattern bodies are unchanged.
#
# Safety: the gdrive drain must be TERMINAL (pending+processing == 0) before the
# move -- moving ./gdrive while a per-file extract->embed->link is in flight would
# orphan that background task. The guard checks /status (new dir= shape, then the
# old source= shape for a still-running pre-pull gateway); if /status is unreachable
# it tells you to stop the stack first (a stopped stack has no in-flight drain).
#
# Run AFTER `git pull` of this commit and BEFORE `make start` (so the running
# gateway is still the old code for the drain check, and the new compose.yml is on
# disk for the recreate). Then:
#   make kb-bootstrap KB=gdrive   (re-assert the grant on the existing KB by name)
#   make gdrive-sync              (incremental reconcile -- NO mass drain: dir=gdrive
#                                  keeps manifest keys identical to the old source=gdrive)
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck source=/dev/null
. ./.env 2>/dev/null || true
# shellcheck source=/dev/null
. ./.env.local 2>/dev/null || true
set +a

say() { printf '%s\n' "$*"; }

# --- 1. drain-terminal guard --------------------------------------------------
# pending+processing must be 0 before moving ./gdrive. Try the new /status shape
# (dir=gdrive, resolve by name) first; fall back to the old shape (source=gdrive,
# GDRIVE_KB_ID) for a still-running pre-pull gateway. pending/processing come from
# OWUI (the KB's file states), not the source walk, so they are authoritative even
# if the walk root is mid-migration.
drain_ok=0
if [ -n "${KB_HOST:-}" ] && [ -n "${OPENWEBUI_ADMIN_API_KEY:-}" ]; then
  O="${KB_HOST%/}"
  adm=(-H "Authorization: Bearer ${OPENWEBUI_ADMIN_API_KEY}")
  # new shape: resolve by name + dir=gdrive
  if kid=$(KB=gdrive ./scripts/kb-bootstrap.sh --resolve 2>/dev/null); then
    inf=$(curl -sS --max-time 60 "${O}/status?kb_id=${kid}&dir=gdrive&json=1" "${adm[@]}" 2>/dev/null \
      | python3 -c 'import sys,json
try:
  d=json.load(sys.stdin); print(int(d["pending"])+int(d["processing"]))
except Exception: print("ERR")' 2>/dev/null || echo "ERR")
    if [ "$inf" = "0" ]; then drain_ok=1; fi
  fi
  # old shape fallback (pre-pull gateway still running old code)
  if [ "$drain_ok" = "0" ] && [ -n "${GDRIVE_KB_ID:-}" ]; then
    inf=$(curl -sS --max-time 60 "${O}/status?source=gdrive&kb_id=${GDRIVE_KB_ID}&json=1" "${adm[@]}" 2>/dev/null \
      | python3 -c 'import sys,json
try:
  d=json.load(sys.stdin); print(int(d["pending"])+int(d["processing"]))
except Exception: print("ERR")' 2>/dev/null || echo "ERR")
    if [ "$inf" = "0" ]; then drain_ok=1; fi
  fi
fi
if [ "$drain_ok" = "0" ]; then
  say "FAIL  could not confirm the gdrive drain is terminal (pending+processing==0)." >&2
  say "       Ensure the gateway is up and the drain is finished: make gdrive-status." >&2
  say "       Or STOP the stack first (a stopped stack has no in-flight drain): make stop, then re-run: make kb-migrate-root" >&2
  exit 1
fi
say "OK  gdrive drain is terminal (pending+processing==0)"

# --- 2. move ./gdrive -> ./root/gdrive ----------------------------------------
if [ -d root/gdrive ]; then
  if [ -d gdrive ]; then
    say "FAIL  both ./gdrive and ./root/gdrive exist -- ambiguous state; remove one before re-running." >&2
    exit 1
  fi
  say "OK  ./root/gdrive already present (corpus move already done)"
else
  if [ ! -d gdrive ]; then
    say "OK  no ./gdrive to move (nothing to migrate; a fresh ./root/ layout is in place)"
  else
    mkdir -p root
    # Same filesystem -> mv is an instant rename. Cross-filesystem -> mv falls back
    # to copy+unlink (slow on a large corpus); warn so the operator knows it is not
    # hung. Refuse a partial state: root/gdrive must not appear until the move ends.
    same_fs=0
    if [ "$(stat -c %m gdrive 2>/dev/null || echo X)" = "$(stat -c %m root 2>/dev/null || echo Y)" ]; then
      same_fs=1
    fi
    if [ "$same_fs" = "0" ]; then
      say "WARN  ./gdrive and ./root are on different filesystems -- mv will copy (may take a while for a large corpus); not hung." >&2
    fi
    mv gdrive root/gdrive
    say "OK  moved ./gdrive -> ./root/gdrive"
  fi
fi

# --- 3. gdrive-exclude.conf -> ./root/.exclude.conf (re-prefix sections) -------
if [ -f root/.exclude.conf ]; then
  say "OK  ./root/.exclude.conf already present (exclude rewrite already done)"
elif [ -f gdrive-exclude.conf ]; then
  # [*] stays verbatim (global deny); a non-* section already under gdrive/ stays;
  # any other [X] -> [gdrive/X]. Comments + pattern bodies unchanged.
  awk '
    /^\[[^]]*\][[:space:]]*$/ {
      h = $0; sub(/^\[/, "", h); sub(/\][[:space:]]*$/, "", h)
      if (h == "*" )                 { print; next }
      if (substr(h,1,7) == "gdrive/") { print; next }
      print "[gdrive/" h "]"
      next
    }
    { print }
  ' gdrive-exclude.conf > root/.exclude.conf
  say "OK  rewrote gdrive-exclude.conf -> ./root/.exclude.conf ([*] verbatim; per-drive [X] -> [gdrive/X])"
else
  say "OK  no gdrive-exclude.conf to migrate (a fresh .exclude.conf.example is tracked; create ./root/.exclude.conf from it if needed)"
fi

# --- 4. remove GDRIVE_KB_ID from .env.local -----------------------------------
if [ -f .env.local ] && grep -qE '^[[:space:]]*GDRIVE_KB_ID=' .env.local; then
  # In-place edit via a temp file (preserve mode + the rest of .env.local).
  tmp=$(mktemp); chmod 600 "$tmp"
  grep -vE '^[[:space:]]*GDRIVE_KB_ID=' .env.local > "$tmp" || true
  mv "$tmp" .env.local
  say "OK  removed GDRIVE_KB_ID from .env.local (resolution is now by name at runtime)"
else
  say "OK  GDRIVE_KB_ID not present in .env.local (nothing to remove)"
fi

# --- 5. recreate api-gateway with the new compose mount -----------------------
# compose.yml in this commit already mounts ./root:/kb-source:ro + sets
# KB_SOURCE_ROOT=/kb-source. `make start` re-reads compose and recreates the
# gateway (the mount change forces it). OWUI / the vector store keep running.
say "==> recreating api-gateway with the new ./root:/kb-source:ro mount (make start)..."
make start
say ""
say "DONE  migration complete. Next:"
say "  make kb-bootstrap KB=gdrive   # re-assert the public-read grant on the existing KB"
say "  make gdrive-sync              # incremental reconcile (NO mass drain: dir=gdrive keeps keys identical)"
say "  make kb-status KB=gdrive      # verify the same file count as before migration"