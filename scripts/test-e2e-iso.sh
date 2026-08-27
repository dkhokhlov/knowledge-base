#!/usr/bin/env bash
# Isolated e2e: clone the repo to a throwaway, gitignored .test-e2e/ IN the repo
# tree and run the destructive e2e there under a SEPARATE compose project
# (`kb-e2e`) so the LIVE stack on this host keeps running (different container
# names, different host port, different project/volumes/networks). The
# destructive e2e (clean-state wipe + re-provision + rclone + full suite +
# test_09 drain) is inlined below -- there is NO standalone `make test-e2e`
# target that would wipe the live stack; this wrapper is the only entry point.
#
# Why a clone is not enough on its own: compose.yml hardcodes
# `container_name: kb-*` (project-name-independent), so a second stack would
# collide with the live `kb-*` containers. compose.e2e.override.yml overrides
# every name to `kb-e2e-*` and is merged only via COMPOSE_FILE. KB_HOST /
# KB_HOST_PORT / OLLAMA_HOST are pinned the STANDARD way: `make bootstrap`
# make-tunables (bootstrap.sh force-persists them into .env, the same mechanism
# it uses for OCR_ENABLED), and the inlined destructive body re-forwards them
# across its own clean-all (rm .env) -> bootstrap. The clone's .env.template is
# NEVER mutated.
#
# The clone lives in .test-e2e/ (on disk, NOT /tmp shmem -- the e2e ./data +
# ./gdrive corpus are too large for tmpfs). It is gitignored; `make clean-test`
# tears the stack down + removes the dir.
#
# Costs vs an in-place (un-isolated) clean-state run:
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
# without it); it is passed to `make bootstrap` below as a make-tunable that
# bootstrap.sh persists into .env. The live .env usually has it commented (the
# operator keeps it in the shell env), so the container fallback makes this work
# from any shell as long as the live stack is up.
if [ -z "${OLLAMA_HOST:-}" ]; then
  OLLAMA_HOST="$(grep -E '^OLLAMA_HOST=' "$SRC/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
fi
if [ -z "${OLLAMA_HOST:-}" ] && docker inspect kb-graphiti >/dev/null 2>&1; then
  # strip the "OPENAI_BASE_URL=" prefix (sub, not awk -F= $2, so a URL containing
  # '=' is not truncated), drop a trailing slash, then strip "/v1".
  base="$(docker inspect kb-graphiti --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | awk '/^OPENAI_BASE_URL=/{sub(/^OPENAI_BASE_URL=/,""); print}')"
  base="${base%/}"
  OLLAMA_HOST="${base%/v1}"
fi
[ -n "${OLLAMA_HOST:-}" ] || { echo "FAIL  OLLAMA_HOST not set (export it, set it in $SRC/.env, or run with the live stack up)" >&2; exit 1; }

# Isolate the e2e tree from the operator's shell profile. The operator's
# ~/.bash_env (sourced by BASH_ENV in EVERY non-interactive child bash: make
# recipe shells, bootstrap.sh, admin-signup.sh, ...) re-exports
# KB_HOST=http://mini2:3000 (the LIVE stack) + OLLAMA_HOST for the live
# deployment. Re-sourcing it in the e2e tree clobbers the KB_HOST make-tunable at
# bootstrap-CAPTURE time: bootstrap's bash sources BASH_ENV (KB_HOST=mini2:3000)
# BEFORE it reads ${KB_HOST:-}, so it would persist mini2:3000 to .env ->
# admin-signup hits the live stack -> 403. Unsetting BASH_ENV stops the re-source;
# OLLAMA_HOST reaches children via this export + normal inheritance, and KB_HOST
# via the make-tunable persisted to .env (sourced after any residual profile in
# every script, so .env wins regardless). unset KB_HOST drops the live-stack
# value inherited from the wrapper's own startup source of ~/.bash_env.
export OLLAMA_HOST
unset BASH_ENV KB_HOST

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

# Provision the e2e values the STANDARD way: `make bootstrap` make-tunables
# (KB_HOST / KB_HOST_PORT / OLLAMA_HOST), which bootstrap.sh force-persists into
# .env (the same mechanism it uses for OCR_ENABLED; see operations.md "Variable
# precedence"). The clone's .env.template is NEVER mutated -- it stays the
# tracked default, so the e2e provisions exactly the way a live `make bootstrap
# KB_HOST=... KB_HOST_PORT=...` would. The inlined destructive body re-forwards
# these same tunables to its internal `make bootstrap` across its own clean-all
# (which wipes .env then re-bootstraps), so the values survive.
E2E_KB_HOST="http://localhost:$E2E_PORT"

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

# ./gdrive: the clone has only the tracked .gitkeep + .tests fixture; the
# standard `make test-e2e-iso` runs a REAL rclone sync (make gdrive-sync) to
# download the live corpus into ./gdrive/<drive>/. No symlink, no reuse -- the
# e2e exercises the real rclone path (the point of the at-scale run). The live
# $SRC/gdrive mirror is untouched (the clone rclones from the gdrive remote,
# not from $SRC).
#
# gdrive-exclude.conf is gitignored (PII: Drive file paths) so the clone has no
# copy; without it rclone hits non-downloadable paths and aborts fail-fast. Copy
# the live one so the clone's rclone uses the same exclusions as the live stack.
# The clone is throwaway (clean-test wipes it), so the PII file is discarded
# with it -- it is never committed and never leaves this host.
[ -f "$SRC/gdrive-exclude.conf" ] && cp "$SRC/gdrive-exclude.conf" "$CLONE/gdrive-exclude.conf"

# Seed admin creds (test-e2e REFUSES without .env.local). bootstrap creates
# .env.local + a generated admin account; test-e2e stashes+restores the creds
# across its own clean-all. Pass the e2e values as make-tunables so bootstrap
# persists them into .env (standard provision; .env.template untouched).
echo "==> make bootstrap (seed .env/.env.local for $CLONE, port $E2E_PORT)"
make bootstrap KB_HOST="$E2E_KB_HOST" KB_HOST_PORT="$E2E_PORT" OLLAMA_HOST="$OLLAMA_HOST"

rc=0
echo "==> destructive e2e (in $CLONE, project $COMPOSE_PROJECT_NAME, port $E2E_PORT)"
# Inlined from the former scripts/test-e2e.sh (removed: the destructive logic
# lives ONLY in this isolated wrapper now -- there is no standalone `make
# test-e2e` footgun that wipes the live stack). Run it in a subshell with its
# own set -e and capture rc via the set +e / ( ... ) / rc=$? / set -e pattern: a
# `set -e` body on the LEFT of `||` has errexit silently disabled (the bash
# gotcha), so `|| rc=$?` cannot catch it; a bare subshell + rc=$? does.
set +e
(
  set -euo pipefail
  # DESTRUCTIVE clean-state deploy + full integration test suite:
  #   wipe -> bootstrap -> restore admin creds -> [pull OCR model] -> preflight
  #   -> [build markitdown-ocr] -> start -> wait healthy -> admin-signup ->
  #   api-keys -> rag-config -> gdrive-index-bootstrap -> gdrive-sync (rclone +
  #   POST /index) -> test -> test_09 (full real-gdrive drain).
  # OCR is provisioned BEFORE the gdrive set ingests so image-bearing documents
  # are OCR'd (non-empty), not orphaned. Extraction + embedding drain async, so
  # test_09 polls GET /status for the real drain terminal state
  # (pending+processing=0 AND completed+failed>=source). The cold first
  # extraction runs per-figure OCR through deepseek-ocr, so the pending-drain
  # budget is raised (E2E_INDEXER_WAIT, default 2400s -> GDRIVE_TEST_WAIT).
  # Stashes OPENWEBUI_FIRST_USER/PASSWORD (+OPENWEBUI_USER) before the wipe and
  # restores them after bootstrap (clean-all deletes .env.local). No fallback:
  # any step failing aborts (set -e); an empty extraction result orphans the file
  # (by design -- the operator sees the outage).
  echo "==> DESTRUCTIVE: wipes all data and re-provisions from scratch."
  test -f .env.local || { echo "REFUSING: no .env.local (no admin creds to stash) — run make bootstrap + fill OPENWEBUI_FIRST_USER/PASSWORD first" >&2; exit 1; }
  _OCR_OVR="${OCR_ENABLED:-}"
  set -a; . ./.env; . ./.env.local; set +a
  if [ -n "$_OCR_OVR" ]; then export OCR_ENABLED="$_OCR_OVR"; fi
  # Capture KB_HOST / KB_HOST_PORT / OLLAMA_HOST from the just-sourced .env
  # (after the source, so the isolated e2e reads the persisted
  # localhost:<E2E_PORT> values, not the empty shell env). Re-forwarded to the
  # internal `make bootstrap` as make-tunables, so the freshly recreated .env
  # keeps the e2e port + host + Ollama URL instead of reverting to the
  # .env.template default.
  _E2E_KB_HOST="${KB_HOST:-}"; _E2E_KB_HOST_PORT="${KB_HOST_PORT:-}"; _E2E_OLLAMA_HOST="${OLLAMA_HOST:-}"
  [ -n "${OPENWEBUI_FIRST_USER:-}" ] && [ -n "${OPENWEBUI_FIRST_PASSWORD:-}" ] \
    || { echo "REFUSING: OPENWEBUI_FIRST_USER/PASSWORD not set in .env.local (admin account) — fill them first" >&2; exit 1; }

  stash=$(mktemp); chmod 600 "$stash"
  { printf 'OPENWEBUI_FIRST_USER=%s\nOPENWEBUI_FIRST_PASSWORD=%s\n' "$OPENWEBUI_FIRST_USER" "$OPENWEBUI_FIRST_PASSWORD"
    [ -n "${OPENWEBUI_USER:-}" ] && printf 'OPENWEBUI_USER=%s\n' "$OPENWEBUI_USER" || true; } > "$stash"
  trap 'rm -f "$stash"' EXIT

  make clean-all
  unset GDRIVE_KB_ID
  # Re-forward the captured host/port/Ollama values as make-tunables so the
  # recreated .env keeps them (bootstrap.sh force-persists them into .env).
  make bootstrap KB_HOST="$_E2E_KB_HOST" KB_HOST_PORT="$_E2E_KB_HOST_PORT" OLLAMA_HOST="$_E2E_OLLAMA_HOST"
  ./scripts/e2e-restore-creds.sh "$stash"
  # Pull the OCR vision model before preflight (preflight hard-fails on a
  # missing OCR model when OCR_ENABLED=true). Pull only the OCR model, NOT full
  # `make pull-models` (that `ollama rm`s + recreates GRAPHITI_MODEL, disrupting
  # the assumed-present base LLM). Honors OCR_ENABLED=false.
  if [ "${OCR_ENABLED:-true}" = "true" ]; then
    echo "==> pulling OCR vision model: ${OCR_MODEL:-deepseek-ocr}"
    ollama pull "${OCR_MODEL:-deepseek-ocr}"
  fi
  make preflight
  # Rebuild locally-built images whose code changed since the last run (clean-all
  # wipes volumes/data, NOT images; `up -d` without --build reuses the existing
  # image). api-gateway is stdlib-only so this is fast. markitdown-ocr is rebuilt
  # here (gated on OCR_ENABLED) so e2e runs current OCR code.
  docker compose build api-gateway
  if [ "${OCR_ENABLED:-true}" = "true" ]; then
    docker compose build markitdown-ocr
  fi
  make start

  H="${KB_HOST:-http://localhost:${KB_HOST_PORT:-3000}}"
  i=0
  until curl -sf "$H/health" >/dev/null; do
    i=$((i+1))
    [ "$i" -lt 60 ] || { echo "stack did not become healthy in 120s ($H/health)" >&2; exit 1; }
    sleep 2
  done
  echo "stack healthy ($H/health)"

  make admin-signup
  make api-keys
  make projects-bootstrap
  make rag-config
  make gdrive-index-bootstrap
  make gdrive-sync
  GDRIVE_TEST_WAIT="${E2E_INDEXER_WAIT:-2400}" make test
  # test_09 (full real-gdrive drain) is not in the `make test` glob (it is slow
  # and coupled to the live rclone-synced corpus); run it explicitly here,
  # where the gdrive KB is provisioned and the corpus is freshly synced above.
  echo "==> full real-gdrive drain (test_09)"
  GDRIVE_TEST_WAIT="${E2E_INDEXER_WAIT:-2400}" bash tests/test_09_gdrive_index.sh
  echo "==> test-e2e PASS"
)
rc=$?
set -e

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