#!/usr/bin/env bash
# Isolated e2e: clone the repo to a throwaway, gitignored .test-e2e/ IN the repo
# tree and run the destructive e2e there under a SEPARATE compose project
# (`kb-e2e`) so the LIVE stack on this host keeps running (different container
# names, different host port, different project/volumes/networks). The
# destructive e2e (clean-state wipe + re-provision + rclone + full suite +
# test_09 drain) is inlined below -- there is NO standalone `make test-e2e`
# target that would wipe the live stack; this wrapper is the only entry point.
#
# The isolation (clone + compose project + generated container-rename override
# + OLLAMA_HOST resolve + bootstrap + teardown) is the REUSABLE
# scripts/e2e-env.sh library, shared with tests/test_12_kb_check.sh. This file
# holds only the gdrive-specific setup (gdrive-exclude.conf copy) + the
# destructive body. See e2e-env.sh for the isolation mechanics.
#
# Why a clone is not enough on its own: compose.yml hardcodes
# `container_name: kb-*` (project-name-independent), so a second stack would
# collide with the live `kb-*` containers. e2e_isolate GENERATES an override
# that renames every container to `kb-e2e-*` (merged via COMPOSE_FILE), so the
# live stack (which does NOT set COMPOSE_FILE) keeps the `kb-*` names. A new
# service added to compose.yml is covered automatically (the override is
# generated from compose.yml's service list).
#
# The clone lives in .test-e2e/<stamp>/ (on disk, NOT /tmp shmem -- the e2e
# ./data + ./gdrive corpus are too large for tmpfs). It is gitignored
# (`/.test-*/`). Clones are NOT auto-removed: each run leaves a datetime-stamped
# snapshot (docker stopped, GPU freed). Remove ONE with `make clean-test STAMP=`
# or flush ALL with `make clean-tests` (manual hygiene).
#
# Costs vs an in-place (un-isolated) clean-state run:
#  - a second full stack runs alongside the live one (RAM + GPU contention on
#    the shared external Ollama);
#  - ./gdrive is gitignored, so the clone re-rclone-downloads the corpus.
#
# On success: docker is STOPPED but the clone is KEPT at .test-e2e/<stamp>/
# (proliferation -- a commit-in-clone-first workflow may hold unmerged commits),
# unless E2E_KEEP=1 (leave the stack running too). On failure: the stack + clone
# are LEFT for debugging (`make clean-test STAMP=<stamp>`).
#
# Usage: make test-e2e-iso [E2E_PORT=3010] [OCR_ENABLED=false] [E2E_KEEP=1]
#   E2E_PORT  - host port for the e2e Caddy (default 3010; must not collide with
#               the live stack's Caddy port, default 3000).
#   E2E_KEEP  - 1 = leave the e2e stack running + .test-e2e on success too.
# Requires: OLLAMA_HOST resolvable (shell env, live .env, or live stack up),
# rclone `gdrive` remote configured, and the locally-built openwebui overlay
# image present.
set -euo pipefail

E2E_PORT="${E2E_PORT:-3010}"
E2E_KEEP="${E2E_KEEP:-0}"
NAME="e2e"
# OCR is honored by e2e_isolate (passed to `make bootstrap`, which persists it
# into .env). Default unset -> the clone uses .env.template's OCR_ENABLED.
OCR_OVR="${OCR_ENABLED:-}"

# Reusable isolation lib (sets E2E_SRC + the e2e_* functions; sourced, not run).
. "$(cd "$(dirname "$0")" && pwd)/e2e-env.sh"

# --- 1. isolate + bootstrap the throwaway clone ------------------------------
e2e_resolve_ollama || { echo "FAIL  OLLAMA_HOST resolution failed" >&2; exit 1; }
# e2e_isolate: clone -> generated container-rename override -> COMPOSE_* env ->
# `make bootstrap` (seeds .env/.env.local + admin account). Refuses to clobber a
# leftover .test-e2e. Leaves cwd inside the clone for the destructive body.
if [ -n "$OCR_OVR" ]; then
  e2e_isolate "$NAME" "$E2E_PORT" "$OCR_OVR"
else
  e2e_isolate "$NAME" "$E2E_PORT"
fi

# ./gdrive: the clone has only the tracked .gitkeep + .tests fixture; the
# standard `make test-e2e-iso` runs a REAL rclone sync (make gdrive-sync) to
# download the live corpus into ./gdrive/<drive>/. No symlink, no reuse -- the
# e2e exercises the real rclone path (the point of the at-scale run). The live
# $E2E_SRC/gdrive mirror is untouched (the clone rclones from the gdrive
# remote, not from $E2E_SRC).
#
# gdrive-exclude.conf is gitignored (PII: Drive file paths) so the clone has no
# copy; without it rclone hits non-downloadable paths and aborts fail-fast. Copy
# the live one so the clone's rclone uses the same exclusions as the live stack.
# The clone is throwaway (clean-test wipes it), so the PII file is discarded
# with it -- it is never committed and never leaves this host.
[ -f "$E2E_SRC/gdrive-exclude.conf" ] && cp "$E2E_SRC/gdrive-exclude.conf" "$E2E_CLONE/gdrive-exclude.conf"

rc=0
echo "==> destructive e2e (in $E2E_CLONE, project $COMPOSE_PROJECT_NAME, port $E2E_PORT)"
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
  # Capture KB_HOST / OLLAMA_HOST from the just-sourced .env (after the source,
  # so the isolated e2e reads the persisted localhost:<E2E_PORT> KB_HOST, not
  # the empty shell env). Re-forwarded to the internal `make bootstrap` as
  # make-tunables, so the freshly recreated .env keeps the e2e host + Ollama
  # URL instead of reverting to the .env.template default. KB_HOST_PORT is NOT
  # captured -- bootstrap derives it from KB_HOST.
  _E2E_KB_HOST="${KB_HOST:-}"; _E2E_OLLAMA_HOST="${OLLAMA_HOST:-}"
  [ -n "${OPENWEBUI_FIRST_USER:-}" ] && [ -n "${OPENWEBUI_FIRST_PASSWORD:-}" ] \
    || { echo "REFUSING: OPENWEBUI_FIRST_USER/PASSWORD not set in .env.local (admin account) — fill them first" >&2; exit 1; }

  stash=$(mktemp); chmod 600 "$stash"
  { printf 'OPENWEBUI_FIRST_USER=%s\nOPENWEBUI_FIRST_PASSWORD=%s\n' "$OPENWEBUI_FIRST_USER" "$OPENWEBUI_FIRST_PASSWORD"
    [ -n "${OPENWEBUI_USER:-}" ] && printf 'OPENWEBUI_USER=%s\n' "$OPENWEBUI_USER" || true; } > "$stash"
  trap 'rm -f "$stash"' EXIT

  make clean-all
  unset GDRIVE_KB_ID
  # Re-forward the captured host/Ollama values as make-tunables so the
  # recreated .env keeps them (bootstrap.sh force-persists KB_HOST; derives
  # KB_HOST_PORT from it).
  make bootstrap KB_HOST="$_E2E_KB_HOST" OLLAMA_HOST="$_E2E_OLLAMA_HOST"
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

  H="${KB_HOST:?KB_HOST not set -- export KB_HOST=http://host:port}"
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
  e2e_keep_or_down "$NAME" "$E2E_KEEP"
else
  echo "==> test-e2e-iso FAIL (rc=$rc). E2e stack + clone LEFT for debugging." >&2
  echo "    port: $E2E_PORT   project: $COMPOSE_PROJECT_NAME   clone: $E2E_CLONE" >&2
  echo "    tear down + remove:  make clean-test NAME=$NAME STAMP=$E2E_STAMP" >&2
fi
exit "$rc"