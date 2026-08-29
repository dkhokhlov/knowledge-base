#!/usr/bin/env bash
# Reusable isolated e2e stack management. Sourced (NOT executed) by
# scripts/test-e2e-iso.sh and tests/test_*.sh that need a throwaway stack that
# does NOT touch the live `kb-*` containers.
#
# What "isolated" means here: clone the repo to a throwaway, gitignored
# .test-<NAME>/ tree and run a SEPARATE compose project (`kb-<NAME>`) with
# container names `kb-<NAME>-*` (a generated override merged via COMPOSE_FILE),
# so the live `kb-*` stack on this host keeps running untouched. The clone's
# .env.template is NEVER mutated -- it stays the tracked default, so the
# isolated stack provisions exactly the way a live `make bootstrap KB_HOST=...
# KB_HOST_PORT=...` would.
#
# Why a clone is not enough on its own: compose.yml hardcodes
# `container_name: kb-*` (project-name-independent), so a second stack would
# collide with the live `kb-*` containers. The generated override renames every
# container to `kb-<NAME>-*`; it is merged only via COMPOSE_FILE, so the live
# stack (which does NOT set COMPOSE_FILE) keeps the `kb-*` names.
#
# The clone lives in .test-<NAME>/ (on disk, NOT /tmp shmem -- the ./data +
# ./gdrive corpus are too large for tmpfs). It is gitignored (`/.test-*/`).
#
# Costs: a second stack runs alongside the live one (RAM + GPU contention on
# the shared external Ollama). ./gdrive is gitignored, so a clone that needs
# the corpus re-rclone-downloads it (the at-scale e2e does; the kb_check test
# does NOT -- it uploads synthetic files directly).
#
# Usage (source this file, then call the functions):
#   . scripts/e2e-env.sh
#   e2e_resolve_ollama            # sets OLLAMA_HOST (env > live .env > kb-graphiti)
#   e2e_isolate <NAME> <PORT> [OCR_ENABLED]   # clone + isolation env + bootstrap
#   e2e_provision                 # make start + wait healthy + admin-signup + api-keys
#   e2e_down <NAME>               # tear down the kb-<NAME> stack + remove .test-<NAME>/
#   e2e_keep_or_down <NAME> <KEEP> # same, but KEEP=1 leaves it running (debugging)
#
# Globals set by e2e_isolate (for the caller): E2E_NAME, E2E_PORT, E2E_CLONE,
# E2E_KB_HOST, plus exported COMPOSE_PROJECT_NAME, COMPOSE_FILE, OWUI_CONTAINER,
# MARKITDOWN_CONTAINER, KB_HOST, KB_HOST_PORT, OLLAMA_HOST. The caller runs its
# test body inside $E2E_CLONE; call e2e_down in an EXIT trap.
#
# Requires: OLLAMA_HOST resolvable (shell env, the live .env, or the live
# kb-graphiti container up), and the locally-built open-webui overlay image
# present (a clone builds/pulls nothing new).

# Guard against double-sourcing.
if [ -n "${_E2E_ENV_SH:-}" ]; then return 0; fi
_E2E_ENV_SH=1

# The source repo root (one level above this script). Captured once, before any
# cd, so teardown can return here regardless of where a caller left the cwd.
E2E_SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"

# Resolve OLLAMA_HOST for the isolated stack. Precedence: shell env > the live
# .env > the running kb-graphiti container (its OPENAI_BASE_URL = $OLLAMA_HOST/v1,
# already translated by the shim; strip /v1). The clone needs it (compose :?
# fails without it); it is passed to e2e_isolate's `make bootstrap` as a
# make-tunable that bootstrap.sh persists into .env. The live .env usually has
# it commented (the operator keeps it in the shell env), so the container
# fallback makes this work from any shell as long as the live stack is up.
e2e_resolve_ollama() {
  if [ -z "${OLLAMA_HOST:-}" ]; then
    OLLAMA_HOST="$(grep -E '^OLLAMA_HOST=' "$E2E_SRC/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  fi
  if [ -z "${OLLAMA_HOST:-}" ] && docker inspect kb-graphiti >/dev/null 2>&1; then
    # strip the "OPENAI_BASE_URL=" prefix (sub, not awk -F= $2, so a URL containing
    # '=' is not truncated), drop a trailing slash, then strip "/v1".
    local base
    base="$(docker inspect kb-graphiti --format '{{range .Config.Env}}{{println .}}{{end}}' \
      | awk '/^OPENAI_BASE_URL=/{sub(/^OPENAI_BASE_URL=/,""); print}')"
    base="${base%/}"
    OLLAMA_HOST="${base%/v1}"
  fi
  if [ -z "${OLLAMA_HOST:-}" ]; then
    echo "FAIL  OLLAMA_HOST not set (export it, set it in $E2E_SRC/.env, or run with the live stack up)" >&2
    return 1
  fi
  export OLLAMA_HOST
}

# Clone the repo to .test-<NAME>/, set up the isolation env (compose project,
# generated container-rename override, OWUI_CONTAINER, KB_HOST/PORT), unset the
# operator's shell profile leaks (BASH_ENV/KB_HOST -- see the comment in
# test-e2e-iso.sh), refuse to clobber a leftover clone, and run `make bootstrap`
# (seed .env/.env.local for the clone). Does NOT start the stack -- the caller
# calls e2e_provision (or its own destructive re-provision).
e2e_isolate() {
  local name="$1" port="$2" ocr="${3:-}"
  local clone="$E2E_SRC/.test-$name"
  local kb_host="http://localhost:$port"

  # The clone is `git clone` of this repo, which materializes COMMITTED state
  # only -- uncommitted working-tree changes (modified OR untracked-not-ignored
  # files) do NOT reach the clone, so the e2e would silently test a DIFFERENT
  # (stale) tree than the one being developed. Fail loud if the source tree is
  # not clean; commit (or stash) first. (Ignored files -- .test-*/, data/,
  # gdrive/ -- are excluded by `git status --porcelain` and do not count.)
  local dirty
  dirty="$(git -C "$E2E_SRC" status --porcelain 2>/dev/null || true)"
  if [ -n "$dirty" ]; then
    echo "FAIL  $E2E_SRC has uncommitted changes; the e2e clone only contains committed code." >&2
    echo "       The e2e would test stale (HEAD) code, not your working tree. Commit or stash first." >&2
    echo "       Dirty files (modified + untracked, ignored excluded):" >&2
    printf '       %s\n' "$dirty" | head -20 >&2
    return 1
  fi

  # Isolate the clone tree from the operator's shell profile. The operator's
  # ~/.bash_env (sourced by BASH_ENV in EVERY non-interactive child bash) re-
  # exports KB_HOST=<live stack> + OLLAMA_HOST for the live deployment. Re-
  # sourcing it in the clone clobbers the KB_HOST make-tunable at bootstrap-
  # CAPTURE time. Unsetting BASH_ENV stops the re-source; OLLAMA_HOST reaches
  # children via the export above + normal inheritance, and KB_HOST via the
  # make-tunable persisted to .env. unset KB_HOST drops the live value inherited
  # from the wrapper's own startup source of ~/.bash_env.
  unset BASH_ENV KB_HOST

  # Refuse to clobber a leftover clone (a prior failed run left it for
  # debugging). Tear it down first.
  if [ -e "$clone" ]; then
    echo "FAIL  $clone already exists (a prior run left it for debugging). Run: make clean-test (or e2e_down $name)" >&2
    return 1
  fi

  # Separate compose project + a GENERATED override (rename every container to
  # kb-<NAME>-*) so the live kb-* stack is untouched. COMPOSE_FILE/COMPOSE_PROJECT_NAME
  # are honored by every bare `docker compose` in the Makefile + scripts.
  export COMPOSE_PROJECT_NAME="kb-$name"
  local override="$clone/compose.$name.override.yml"
  export COMPOSE_FILE="compose.yml:compose.$name.override.yml"
  export OWUI_CONTAINER="kb-$name-openwebui"
  export MARKITDOWN_CONTAINER="kb-$name-markitdown-ocr"
  export KB_HOST="$kb_host"
  export KB_HOST_PORT="$port"

  # Clone from the LOCAL repo (origin may be behind; this repo's HEAD is
  # current). --no-local forces the transport (no hardlinks) so it works across
  # filesystems.
  echo "==> clone $E2E_SRC -> $clone"
  git clone --no-local "$E2E_SRC" "$clone" || return 1
  cd "$clone" || return 1

  # Generate the container-rename override from compose.yml's services, so a new
  # service added to compose.yml is covered automatically (no per-name override
  # file to keep in sync). A service present in compose.yml but missing here
  # would collide loudly on `up` with the live stack's container name.
  #
  # awk caveat: do NOT mutate field $1 with sub() -- that rebuilds $0 without its
  # leading indentation, so the same record would then match the `^[^ ]` exit
  # rule and stop after the FIRST service (leaving the rest colliding with the
  # live stack). Copy $1 into a local var, strip the colon there, print, and
  # `next` so the exit rule never sees a service-decl line.
  local svcs
  svcs="$(awk '/^services:/{f=1;next} f&&/^  [A-Za-z0-9_-]+:$/{s=$1;sub(/:$/,"",s);print s;next} f&&/^[^ ]/{exit}' compose.yml)"
  {
    echo "# Generated by scripts/e2e-env.sh -- rename every container so a second"
    echo "# stack (compose project kb-$name) does NOT collide with the live kb-* names."
    echo "# Auto-generated; do not edit. Regenerated on every e2e_isolate."
    echo "services:"
    for s in $svcs; do
      printf '  %s:\n    container_name: kb-%s-%s\n' "$s" "$name" "$s"
    done
  } > "$override"

  # Provision the isolated values the STANDARD way: `make bootstrap` make-
  # tunables (KB_HOST / KB_HOST_PORT / OLLAMA_HOST / OCR_ENABLED), which
  # bootstrap.sh force-persists into .env. The clone's .env.template is NEVER
  # mutated, so it provisions like a live `make bootstrap KB_HOST=...`. OCR is
  # passed ONLY when the caller provides it (empty = leave .env.template's
  # default; matches bootstrap's "no tunable = no change" idempotency).
  local ocr_arg=()
  [ -n "$ocr" ] && ocr_arg=(OCR_ENABLED="$ocr")
  echo "==> make bootstrap (seed .env/.env.local for $clone, port $port${ocr:+, OCR=$ocr})"
  make bootstrap KB_HOST="$kb_host" KB_HOST_PORT="$port" OLLAMA_HOST="$OLLAMA_HOST" \
    "${ocr_arg[@]}" || return 1

  # Safety net: the generated override must rename EVERY service, or a missed
  # service keeps its hardcoded `kb-*` container name and collides with the live
  # stack on `make start` (the awk bug this guards against once dropped every
  # service after the first). `docker compose config --services` is compose's own
  # authoritative service list (independent of the awk); .env now exists so the
  # :? vars parse. Fail loud if the override is missing any service.
  local miss
  miss="$(comm -23 \
    <(docker compose config --services 2>/dev/null | sort) \
    <(grep -E '^  [A-Za-z0-9_-]+:$' "$override" | sed 's/:$//;s/^  //' | sort))"
  if [ -n "$miss" ]; then
    echo "FAIL  generated override missing services (would collide with live kb-*): $miss" >&2
    return 1
  fi

  E2E_NAME="$name"; E2E_PORT="$port"; E2E_CLONE="$clone"; E2E_KB_HOST="$kb_host"
  export E2E_NAME E2E_PORT E2E_CLONE E2E_KB_HOST
}

# Non-destructive provision of the isolated stack: start, wait for /health, then
# create the admin account + provision admin/agent API keys. Call AFTER
# e2e_isolate. Does NOT run rag-config, projects-bootstrap, or gdrive (a test
# that needs those calls them itself). OCR is honored per e2e_isolate's bootstrap.
e2e_provision() {
  echo "==> make start (isolated project $COMPOSE_PROJECT_NAME, port $E2E_PORT)"
  make start || return 1
  local h="$E2E_KB_HOST" i=0
  until curl -sf "$h/health" >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -lt 60 ] || { echo "FAIL  isolated stack did not become healthy in 120s ($h/health)" >&2; return 1; }
    sleep 2
  done
  echo "stack healthy ($h/health)"
  make admin-signup || return 1
  make api-keys || return 1
}

# Tear down the kb-<NAME> compose project + remove the .test-<NAME>/ clone. Safe
# anytime (no-op if absent). The clone's ./data is root/neo4j-owned (OWUI/Neo4j
# write bind-mount files the host user cannot delete), so a host `rm -rf` fails
# midway -- down the project first, then root-rm the tree via an alpine container
# (same pattern clean-all uses for ./data). Called from an EXIT trap.
e2e_down() {
  local name="$1" proj="kb-$name"
  local clone="$E2E_SRC/.test-$name"
  local had_clone=0
  [ -d "$clone" ] && had_clone=1
  cd "$E2E_SRC" || true
  if [ "$had_clone" = "1" ]; then
    # compose down (best-effort) -- stops + removes project containers/volumes.
    ( cd "$clone" && COMPOSE_PROJECT_NAME="$proj" \
        COMPOSE_FILE="compose.yml:compose.$name.override.yml" \
        docker compose down --remove-orphans 2>/dev/null || true )
  fi
  # Label-based orphan sweep: removes any kb-<name> project containers that
  # `down` missed (e.g. down failed, or the clone was already wiped leaving
  # stranded containers with no project files to re-tear-down). Uses docker
  # labels, not compose, so it works even if the clone dir is gone -- this is
  # what `make clean-test` falls back to when the clone was partially cleaned.
  local orphans
  orphans="$(docker ps -aq --filter label=com.docker.compose.project="$proj" 2>/dev/null || true)"
  [ -n "$orphans" ] && docker rm -f $orphans >/dev/null 2>&1 || true
  if [ "$had_clone" = "1" ]; then
    docker run --rm -v "$clone:/data" alpine sh -c "rm -rf /data/* /data/.[!.]* /data/..?*" 2>/dev/null || true
    rm -rf "$clone" 2>/dev/null || true
  fi
  # Report the ACTUAL outcome -- do not print "removed" if nothing was removed.
  if [ "$had_clone" = "1" ]; then
    if [ ! -e "$clone" ]; then
      echo "==> removed $clone (isolated stack $proj)."
    else
      echo "WARN  $clone still on disk (cleanup partial); recover with: make clean-test NAME=$name" >&2
    fi
  elif [ -n "$orphans" ]; then
    echo "==> swept orphan $proj containers (clone was already gone)."
  else
    echo "==> no $clone to tear down."
  fi
}

# test-e2e-iso semantics: KEEP=1 leaves the stack + clone in place on success
# (for debugging / re-use); KEEP=0 tears it down. Returns 0 either way.
e2e_keep_or_down() {
  local name="$1" keep="${2:-0}"
  if [ "$keep" = "1" ]; then
    echo "==> PASS (stack left running on port ${E2E_PORT:-?}; clone at $E2E_SRC/.test-$name; tear down with: e2e_down $name)"
  else
    e2e_down "$name"
  fi
}