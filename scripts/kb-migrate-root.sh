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
#   gdrive-exclude.conf       -> ./root/.kb-ignore (per-directory)  (INI -> gitignore)
#   .env.local: GDRIVE_KB_ID  -> removed                        (name-based resolution)
#   compose mount ./gdrive:/gdrive -> ./root:/kb-source:ro      (already in the new compose.yml)
#
# Exclude deny-list translation: the OLD INI (gdrive-exclude.conf / .exclude.conf,
# [section] headers) becomes the NEW per-directory .kb-ignore chain (gitignore-style).
# [*] -> ./root/.kb-ignore (globals); each [gdrive/X] -> ./root/gdrive/X/.kb-ignore.
# scripts/exclude_to_kb_ignore.py does the translation (patterns verbatim; a bare
# [X] is re-prefixed to gdrive/X). Idempotent on ./root/.kb-ignore already existing.
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

# --- 1b. rebuild the api-gateway image (before any destructive step) -----------
# `make start` (scripts/start.sh -> `docker compose up -d`) does NOT rebuild a
# locally-built image -- it recreates the CONTAINER only. Build the new image
# from ./gateway NOW so a build failure aborts BEFORE the corpus move; step 5
# `make start` then recreates the gateway with this fresh image. Without this,
# a stale pre-migration image (old code: GDRIVE_ROOT -> /gdrive) under the new
# ./root:/kb-source:ro mount would 422 on an empty root (or mass-delete with
# force=1).
say "==> building the api-gateway image from the new ./gateway code..."
docker compose build api-gateway
say "OK  api-gateway image rebuilt (new code: dir= + KB_SOURCE_ROOT)"

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

# --- 3. exclude deny-list -> ./root/.kb-ignore (per-directory gitignore) -------
# The deny-list is now .kb-ignore (gitignore-style, per-directory), not the old
# INI .exclude.conf. Idempotent: if ./root/.kb-ignore (globals) already exists, the
# translation was done. Otherwise translate whichever source exists -- the
# post-migrate ./root/.exclude.conf (INI, sections already gdrive/-prefixed), or
# the pre-migration gdrive-exclude.conf (INI, bare [X] headers -- the translator
# re-prefixes them to gdrive/X). scripts/exclude_to_kb_ignore.py writes one
# .kb-ignore per section ([*] -> ./root/.kb-ignore globals; [gdrive/X] ->
# ./root/gdrive/X/.kb-ignore). Patterns are copied verbatim (rclone-native and
# gitignore semantics agree), so no pattern rewriting is needed.
if [ -f root/.kb-ignore ]; then
  say "OK  ./root/.kb-ignore already present (deny-list translation already done)"
elif [ -f root/.exclude.conf ]; then
  if ! python3 scripts/exclude_to_kb_ignore.py --src root/.exclude.conf --target-root root >/dev/null; then
    say "FAIL  exclude_to_kb_ignore failed on root/.exclude.conf"; exit 1
  fi
  say "OK  translated ./root/.exclude.conf -> ./root/.kb-ignore (per-directory .kb-ignore files)"
elif [ -f gdrive-exclude.conf ]; then
  if ! python3 scripts/exclude_to_kb_ignore.py --src gdrive-exclude.conf --target-root root >/dev/null; then
    say "FAIL  exclude_to_kb_ignore failed on gdrive-exclude.conf"; exit 1
  fi
  say "OK  translated gdrive-exclude.conf -> ./root/.kb-ignore ([*] globals; per-drive [X] -> gdrive/X/.kb-ignore)"
else
  say "OK  no exclude deny-list to migrate (create ./root/.kb-ignore files if needed)"
fi

# --- 4. remove GDRIVE_KB_ID from .env.local -----------------------------------
if [ -f .env.local ] && grep -qE '^[[:space:]]*GDRIVE_KB_ID=' .env.local; then
  # Atomic rewrite: mktemp in the REPO dir (same filesystem as .env.local) so the
  # final `mv` is a rename, not a cross-device copy/replace. mktemp under /tmp is
  # a different device here -> an interruption mid-copy could truncate .env.local.
  # Do NOT mask a read error with `|| true`: grep -v exit 2 (I/O error) would
  # otherwise replace .env.local with an empty/partial file. Exit 0 = lines kept,
  # 1 = file had only GDRIVE_KB_ID (empty result is valid), 2 = error.
  tmp=$(mktemp ./.env.local.XXXXXX); chmod 600 "$tmp"
  grep -vE '^[[:space:]]*GDRIVE_KB_ID=' .env.local > "$tmp"; rc=$?
  if [ "$rc" -eq 2 ]; then
    rm -f "$tmp"
    say "FAIL  could not rewrite .env.local (read error); left .env.local unchanged" >&2
    exit 1
  fi
  mv -f "$tmp" .env.local
  say "OK  removed GDRIVE_KB_ID from .env.local (resolution is now by name at runtime)"
else
  say "OK  GDRIVE_KB_ID not present in .env.local (nothing to remove)"
fi

# --- 5. recreate api-gateway with the new compose mount -----------------------
# compose.yml in this commit already mounts ./root:/kb-source:ro + sets
# KB_SOURCE_ROOT=/kb-source. The image was rebuilt in step 1b, so `make start`
# (docker compose up -d) recreates the gateway with the NEW image + the new
# mount/env. A mount/env change forces a CONTAINER recreate, NOT an image
# rebuild -- that is why step 1b builds first. OWUI / the vector store keep
# running.
say "==> recreating api-gateway with the new ./root:/kb-source:ro mount (make start)..."
make start

# --- 5b. verify the new gateway + post-move dry-run parity gate ----------------
# The rebuilt image is proven by a dry_run=1 /index against the MOVED tree: a
# stale image would 422 on the empty /gdrive root or error on the `dir` param.
# dry_run runs ZERO mutations (gateway app.py: returns the sync/diff plan only).
# Assert key-shape parity: added=modified=deleted=0 -- dir=gdrive keeps manifest
# keys identical to the old source=gdrive shape. If any delta > 0 the dir= root
# is WRONG: abort BEFORE the operator's `make gdrive-sync` would apply it.
O="${KB_HOST%/}"
adm=(-H "Authorization: Bearer ${OPENWEBUI_ADMIN_API_KEY}")
say "==> waiting for the new api-gateway /health..."
healthy=0
for _ in $(seq 1 30); do
  if curl -sS --max-time 5 "${O}/health" >/dev/null 2>&1; then healthy=1; break; fi
  sleep 1
done
if [ "$healthy" != "1" ]; then
  say "FAIL  api-gateway did not become healthy after make start -- aborting before the parity gate" >&2
  exit 1
fi
say "OK  api-gateway healthy"
if ! KB_ID=$(KB=gdrive ./scripts/kb-bootstrap.sh --resolve 2>/dev/null); then
  say "FAIL  could not resolve the 'gdrive' KB by name for the parity gate (run: make kb-bootstrap KB=gdrive)" >&2
  exit 1
fi
# Retry the dry_run /index: /health readiness != /index readiness right after
# `make start` -- Caddy + OWUI are also recreated, and /index -> owui.sync_diff
# needs OWUI wired (a 200 /health can precede that by tens of seconds). Poll
# up to ~90s before declaring failure; dry_run is zero-mutation so retries are safe.
plan=""
dr=""
for _ in $(seq 1 18); do
  dr=$(curl -sS --max-time 30 -X POST "${O}/index?kb_id=${KB_ID}&dir=gdrive&dry_run=1" "${adm[@]}" 2>/dev/null || true)
  plan=$(printf '%s' "$dr" | python3 -c 'import sys,json
try:
  d=json.load(sys.stdin)
  if not d.get("dry_run"): print("NOTDRY")
  else: print("%d %d %d %d" % (int(d.get("added",0)), int(d.get("modified",0)), int(d.get("deleted",0)), int(d.get("unmodified",0))))
except Exception: print("ERR")' 2>/dev/null || echo "ERR")
  case "$plan" in
    ERR|NOTDRY|"") sleep 5 ;;
    *) break ;;
  esac
done
if [ "$plan" = "ERR" ] || [ "$plan" = "NOTDRY" ] || [ -z "$plan" ]; then
  say "FAIL  post-move dry_run /index did not return a dry-run plan (got: ${dr:0:160}) -- the gateway may be stale, or Caddy/OWUI still wiring after make start; aborting" >&2
  exit 1
fi
read -r g_added g_mod g_del g_unmod <<< "$plan"
say "post-move dry_run: added=${g_added} modified=${g_mod} deleted=${g_del} unmodified=${g_unmod}"
if [ "$g_added" != "0" ] || [ "$g_mod" != "0" ] || [ "$g_del" != "0" ]; then
  say "FAIL  key-shape parity broken: added=${g_added} modified=${g_mod} deleted=${g_del} (expected all 0; dir=gdrive keeps manifest keys identical)" >&2
  say "       DO NOT run make gdrive-sync -- it would apply these deltas. Revert the move (mv root/gdrive gdrive) and investigate the dir= root." >&2
  exit 1
fi
say "OK  key-shape parity holds (added=0 modified=0 deleted=0 unmodified=${g_unmod}) -- safe to run make gdrive-sync"
say ""
say "DONE  migration complete. Next:"
say "  make kb-bootstrap KB=gdrive   # re-assert the public-read grant on the existing KB"
say "  make gdrive-sync              # incremental reconcile (NO mass drain: dir=gdrive keeps keys identical)"
say "  make kb-status KB=gdrive      # verify the same file count as before migration"