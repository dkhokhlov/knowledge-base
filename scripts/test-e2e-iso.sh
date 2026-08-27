#!/usr/bin/env bash
# Isolated e2e: clone the repo to a throwaway, gitignored .test-e2e/ IN the repo
# tree and run `make test-e2e` there under a SEPARATE compose project (`kb-e2e`)
# so the LIVE stack on this host keeps running (different container names,
# different host port, different project/volumes/networks). `make test-e2e` is
# destructive (wipes .env / .env.local / ./data), so this isolates that
# destruction from the live deployment.
#
# Why a clone is not enough on its own: compose.yml hardcodes
# `container_name: kb-*` (project-name-independent), so a second stack would
# collide with the live `kb-*` containers. compose.e2e.override.yml overrides
# every name to `kb-e2e-*` and is merged only via COMPOSE_FILE. KB_HOST_PORT +
# OLLAMA_HOST are baked into the CLONE's .env.template so they survive
# test-e2e's internal `make clean-all` (rm .env) -> `make bootstrap` (recreates
# .env from .env.template).
#
# The clone lives in .test-e2e/ (on disk, NOT /tmp shmem -- the e2e ./data +
# ./gdrive corpus are too large for tmpfs). It is gitignored; `make clean-test`
# tears the stack down + removes the dir.
#
# Costs vs in-place `make test-e2e`:
#  - a second full stack runs alongside the live one (RAM + GPU contention on
#    the shared external Ollama, mini4);
#  - ./gdrive is gitignored, so the clone re-rclone-downloads the corpus;
#  - compose.e2e.override.yml must list every service (a new service added to
#    compose.yml without a line there collides loudly on `up`).
#
# On success: the e2e stack is torn down (`make clean`) and .test-e2e removed.
# On failure: the stack + clone are LEFT for debugging (run `make clean-test`).
#
# Usage: make test-e2e-iso [E2E_PORT=3010] [OCR_ENABLED=false] [E2E_KEEP=1]
#   E2E_PORT  - host port for the e2e Caddy (default 3010; must not collide with
#               the live KB_HOST_PORT, default 3000).
#   E2E_KEEP - 1 = leave the e2e stack running + .test-e2e on success too.
# Requires: OLLAMA_HOST set (shell env or the live .env), rclone `gdrive`
# remote configured, and the locally-built openwebui overlay image present.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
E2E_PORT="${E2E_PORT:-3010}"
E2E_KEEP="${E2E_KEEP:-0}"
CLONE="$SRC/.test-e2e"

# OLLAMA_HOST: prefer shell env, else the live .env, else derive from the
# running kb-graphiti container (its OPENAI_BASE_URL = $OLLAMA_HOST/v1, already
# translated by the shim; strip /v1). The clone needs it (compose :? fails
# without it); baked into the clone's .env.template below. The live .env usually
# has it commented (the operator keeps it in the shell env), so the container
# fallback makes this work from any shell as long as the live stack is up.
if [ -z "${OLLAMA_HOST:-}" ]; then
  OLLAMA_HOST="$(grep -E '^OLLAMA_HOST=' "$SRC/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
fi
if [ -z "${OLLAMA_HOST:-}" ] && docker inspect kb-graphiti >/dev/null 2>&1; then
  base="$(docker inspect kb-graphiti --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | awk -F= '/^OPENAI_BASE_URL=/{print $2}')"
  OLLAMA_HOST="${base%/v1}"
fi
[ -n "${OLLAMA_HOST:-}" ] || { echo "FAIL  OLLAMA_HOST not set (export it, set it in $SRC/.env, or run with the live stack up)" >&2; exit 1; }

# Refuse to clobber a leftover .test-e2e (a prior failed run left it for
# debugging). Tear it down first.
if [ -e "$CLONE" ]; then
  echo "FAIL  $CLONE already exists (a prior run left it for debugging). Run: make clean-test" >&2
  exit 1
fi

# Clone from the LOCAL repo (origin may be behind; this repo's HEAD is current).
# --no-local forces the transport (no hardlinks) so it works across filesystems.
echo "==> clone $SRC -> $CLONE"
git clone --no-local "$SRC" "$CLONE"
cd "$CLONE"

# Bake port + OLLAMA_HOST into the clone's .env.template so they survive
# test-e2e's clean-all (rm .env) -> bootstrap (recreates .env from .env.template).
# OLLAMA_HOST is commented (#OLLAMA_HOST=) in the template; uncomment + set it.
sed -i "s/^KB_HOST_PORT=3000/KB_HOST_PORT=$E2E_PORT/" .env.template
sed -i "s|^#OLLAMA_HOST=|OLLAMA_HOST=$OLLAMA_HOST|" .env.template

# Separate compose project + override file (kb-e2e-* container names) so the
# live kb-* stack is untouched. COMPOSE_FILE/COMPOSE_PROJECT_NAME are honored by
# every bare `docker compose` in the Makefile + scripts (start.sh, clean-all).
export COMPOSE_PROJECT_NAME=kb-e2e
export COMPOSE_FILE=compose.yml:compose.e2e.override.yml
# test_10 docker-inspects/execs the OCR container; test_04 already honors
# OWUI_CONTAINER. Point both at the e2e-prefixed names so `make test` (run by
# test-e2e) targets the e2e stack, not the live one.
export MARKITDOWN_CONTAINER=kb-e2e-markitdown-ocr
export OWUI_CONTAINER=kb-e2e-openwebui

# Reuse the live ./gdrive mirror to avoid re-rclone-downloading a large corpus
# (./gdrive grows over time and is gitignored, so the clone has no copy).
# Symlink the clone's ./gdrive to the live mirror and skip the rclone sync
# (GDRIVE_SKIP_RCLONE=1) -- the e2e indexes the live files into the e2e KB
# (separate DB) + drains them. ./gdrive is read-only here (only rclone writes
# it, and we skip it), so the live mirror is untouched.
# Fallback: if the live mirror is absent/empty, do a real rclone sync into the
# clone's own ./gdrive (first run downloads the corpus).
if [ -d "$SRC/gdrive" ] && [ -n "$(ls -A "$SRC/gdrive" 2>/dev/null)" ]; then
  ln -s "$SRC/gdrive" "$CLONE/gdrive"
  export GDRIVE_SKIP_RCLONE=1
  echo "==> reuse live ./gdrive mirror ($SRC/gdrive -> $CLONE/gdrive); skipping rclone re-download"
else
  echo "==> no live ./gdrive mirror; e2e will rclone-sync a fresh corpus into $CLONE/gdrive"
fi

# Seed admin creds (test-e2e REFUSES without .env.local). bootstrap creates
# .env.local + a generated admin account; test-e2e stashes+restores the creds
# across its own clean-all.
echo "==> make bootstrap (seed .env/.env.local for $CLONE)"
make bootstrap

rc=0
echo "==> make test-e2e (in $CLONE, project $COMPOSE_PROJECT_NAME, port $E2E_PORT)"
make test-e2e || rc=$?

if [ "$rc" -eq 0 ]; then
  if [ "$E2E_KEEP" = "1" ]; then
    echo "==> test-e2e-iso PASS (stack left running on port $E2E_PORT; clone at $CLONE; tear down with: make clean-test)"
  else
    echo "==> test-e2e-iso PASS -> tearing down the e2e stack + removing $CLONE"
    # clean-test downs the kb-e2e project + removes .test-e2e (as root via an
    # alpine container, since OWUI/Neo4j write root/neo4j-owned files that a host
    # rm -rf cannot delete -- the same pattern clean-all uses for ./data).
    cd "$SRC" && make clean-test
  fi
else
  echo "==> test-e2e-iso FAIL (rc=$rc). E2e stack + clone LEFT for debugging." >&2
  echo "    port: $E2E_PORT   project: $COMPOSE_PROJECT_NAME" >&2
  echo "    tear down + remove:  make clean-test" >&2
fi
exit "$rc"