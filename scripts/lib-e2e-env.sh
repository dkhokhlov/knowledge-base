#!/usr/bin/env bash
# Reusable isolated e2e stack management. Sourced (NOT executed) by the conftest
# iso fixtures (tests/conftest.py) and tests/test_*.sh that need a throwaway stack
# that does NOT touch the live `kb-*` containers.
#
# What "isolated" means here: clone the repo to a throwaway, gitignored
# .test-env/<stamp>-<NAME>/ tree and run a SEPARATE compose project
# (`kb-<NAME>-<stamp>`) with container names `kb-<NAME>-<stamp>-*` (a generated
# override merged via COMPOSE_FILE), so the live `kb-*` stack on this host keeps
# running untouched. The <stamp> (date +%Y%m%d-%H%M%S) makes every run unique --
# no clobber of a prior (possibly commit-bearing) clone, no container-name
# collision. The clone's .env.template is NEVER mutated -- it stays the tracked
# default, so the isolated stack provisions exactly the way a live
# `make bootstrap KB_HOST=...` would (KB_HOST_PORT is derived from KB_HOST by
# bootstrap, not passed).
#
# Why a clone is not enough on its own: compose.yml hardcodes
# `container_name: kb-*` (project-name-independent), so a second stack would
# collide with the live `kb-*` containers. The generated override renames every
# container to `kb-<NAME>-<stamp>-*`; it is merged only via COMPOSE_FILE, so the
# live stack (which does NOT set COMPOSE_FILE) keeps the `kb-*` names.
#
# Proliferation: a run leaves docker STOPPED (GPU freed) but the clone KEPT at
# .test-env/<stamp>-<NAME>/ -- a commit-in-clone-first workflow may hold unmerged
# commits, so clones are NOT auto-removed. The host PORT auto-picks a free port
# (3011..3099, skip 3000 + 3010) when the caller passes none; an explicit port override
# is the caller's collision risk, and a failed run still holding it must be
# cleaned before a re-run on that port (make start failing on the bind is the
# clear signal). `make clean-test STAMP=<stamp>`
# removes ONE run; `make clean-tests` is the manual hygiene flush (every stamp +
# orphan docker). The clone lives on disk (NOT /tmp
# shmem -- the ./data + ./gdrive corpus are too large for tmpfs); gitignored
# (`/.test-*/`).
#
# Costs: a second stack runs alongside the live one (RAM + GPU contention on
# the shared external Ollama). ./gdrive is gitignored, so a clone that needs
# the corpus re-rclone-downloads it (the at-scale e2e does; the kb_check test
# does NOT -- it uploads synthetic files directly).
#
# Usage (source this file, then call the functions):
#   . scripts/lib-e2e-env.sh
#   e2e_resolve_ollama            # sets OLLAMA_HOST (env > live .env > kb-graphiti)
#   e2e_isolate <NAME> [PORT] [OCR_ENABLED]   # stamped clone + isolation env + bootstrap
#                                              # (PORT omitted -> auto-pick a free port)
#   e2e_provision                 # make start + wait healthy + admin-signup + api-keys
#                                 # + projects-bootstrap + rag-config (prod-exact) + ephemeral user
#   e2e_provision_at_scale        # at-scale: re-bootstrap + image
#                                 # rebuild + preflight + gdrive (rclone) + make ci (test_09)
#   e2e_stop_docker <NAME> <STAMP> # stop+remove docker, KEEP the clone (success path)
#   e2e_down <NAME> [STAMP]        # stop docker + remove the clone (quick tests' EXIT
#                                 # trap, make clean-test; latest stamp if no STAMP)
#   e2e_clean_tests [NAME]         # flush ALL stamps + orphans (make clean-tests)
#
# Globals set by e2e_isolate (for the caller): E2E_NAME, E2E_PORT, E2E_CLONE,
# E2E_KB_HOST, E2E_STAMP, plus exported COMPOSE_PROJECT_NAME, COMPOSE_FILE,
# OWUI_CONTAINER, MARKITDOWN_CONTAINER, POSTGRES_CONTAINER, KB_HOST, OLLAMA_HOST. The caller runs its
# test body inside $E2E_CLONE; the quick tests call e2e_down in an EXIT trap.
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

# Pick a free host port in [lo..hi] (default 3011..3099) for an isolated stack.
# Skips 3000 (the live stack's Caddy). Prints the port; returns 1 if none free.
# One Python invocation scans the whole range (bind-test each port). A small
# TOCTOU gap is inherent -- the probe socket closes before `make start` binds the
# port in a separate process -- so `make start` failing on the bind is still the
# clear signal; a re-run picks a different free port. This is part of env setup
# (e2e_isolate), run before the test body.
_e2e_free_port() {
  local lo="${1:-3011}" hi="${2:-3099}"
  python3 - "$lo" "$hi" <<'PY'
import socket, sys
lo, hi = int(sys.argv[1]), int(sys.argv[2])
SKIP = {3000}                    # never the live stack's Caddy
for p in range(lo, hi + 1):
    if p in SKIP:
        continue
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("", p))
    except OSError:
        s.close(); continue
    s.close(); print(p); sys.exit(0)
sys.exit("FAIL  no free host port in %d..%d (3000 skipped)" % (lo, hi))
PY
}

# Clone the repo to .test-env/<stamp>-<NAME>/, set up the isolation env (stamped
# compose project, generated container-rename override, OWUI_CONTAINER,
# KB_HOST), unset the operator's shell profile leaks (BASH_ENV/KB_HOST), and run
# `make bootstrap` (seed .env/.env.local
# for the clone). The stamp makes the clone unique, so a leftover clone never
# blocks a re-run. Does NOT start the stack -- the caller calls e2e_provision
# (or its own destructive re-provision). The host PORT auto-picks a free port
# (_e2e_free_port, skip 3000 + 3010) when the caller passes none; an explicit port
# (TEST08_PORT / KBCHECK_PORT / E2E_PORT) overrides and owns its collision risk.
e2e_isolate() {
  local name="${1:-e2e}" port="${2:-}" ocr="${3:-}"
  [ -n "$port" ] || port="$(_e2e_free_port)" || return 1
  local kb_host="http://localhost:$port"
  local parent="$E2E_SRC/.test-env"
  mkdir -p "$parent"
  # Datetime-stamped clone + project: each run gets a unique
  # .test-env/<stamp>-<name>/ clone and a kb-<name>-<stamp> compose project, so
  # re-runs never clobber a prior (possibly failed, commit-bearing) clone and
  # never collide on container names. The host PORT auto-picks a free port when
  # none is passed (above); an explicit override is the caller's collision risk.
  # A failed run still holding its port must be cleaned (make clean-test
  # STAMP=<stamp>). The leaf is reserved atomically with mkdir (fails if a
  # same-second same-name run already took it -> retry with a fresh stamp),
  # closing the check/create TOCTOU. The reserved leaf stays EMPTY across the
  # dirty-tree check below; a dirty-tree failure rmdirs it.
  local stamp clone
  stamp="$(date +%Y%m%d-%H%M%S)"; clone="$parent/$stamp-$name"
  until mkdir "$clone" 2>/dev/null; do
    stamp="$(date +%Y%m%d-%H%M%S)"; clone="$parent/$stamp-$name"
  done

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
    rmdir "$clone" 2>/dev/null || true   # drop the empty reserved leaf
    return 1
  fi

  # Isolate the clone tree from the operator's shell profile. The operator's
  # ~/.bash_env (sourced by BASH_ENV in EVERY non-interactive child bash) re-
  # exports KB_HOST=<live stack> + OLLAMA_HOST for the live deployment, and
  # ~/.api_keys exports KB_API_KEY (the operator's live user key). Re-sourcing
  # ~/.bash_env in the clone clobbers the KB_HOST make-tunable at bootstrap-
  # CAPTURE time. Unsetting BASH_ENV stops the re-source; OLLAMA_HOST reaches
  # children via the export above + normal inheritance, and KB_HOST via the
  # make-tunable persisted to .env. unset KB_HOST drops the live value inherited
  # from the wrapper's own startup source of ~/.bash_env. unset KB_API_KEY drops
  # the operator's live key -- a leaked key would satisfy the clone's
  # ${KB_API_KEY:?} with the WRONG identity (401 against the clone DB); the clone
  # gets its own ephemeral user key via e2e_ephemeral_user (written to .env.local).
  unset BASH_ENV KB_HOST KB_API_KEY

  # The stamped clone is unique per run (above), so a leftover clone from a
  # prior run does NOT block this one -- it lives at a different stamp. A prior
  # run's docker, if still up, holds the host port (make start fails on the
  # bind); clean it with `make clean-test NAME=<name> STAMP=<stamp>` (or
  # `make clean-tests`).

  # Separate compose project + a GENERATED override (rename every container to
  # kb-<NAME>-*) so the live kb-* stack is untouched. COMPOSE_FILE/COMPOSE_PROJECT_NAME
  # are honored by every bare `docker compose` in the Makefile + scripts.
  export COMPOSE_PROJECT_NAME="kb-$name-$stamp"
  local override="$clone/compose.$name.override.yml"
  export COMPOSE_FILE="compose.yml:compose.$name.override.yml"
  export OWUI_CONTAINER="kb-$name-$stamp-openwebui"
  export MARKITDOWN_CONTAINER="kb-$name-$stamp-markitdown-ocr"
  export POSTGRES_CONTAINER="kb-$name-$stamp-postgres"
  export KB_HOST="$kb_host"

  # Clone from the LOCAL repo (origin may be behind; this repo's HEAD is
  # current). --no-local forces the transport (no hardlinks) so it works across
  # filesystems. Clone into the reserved empty leaf (git clones into an empty
  # existing dir); drop the reservation if cloning fails.
  echo "==> clone $E2E_SRC -> $clone"
  if ! git clone --no-local "$E2E_SRC" "$clone"; then
    rmdir "$clone" 2>/dev/null || true
    return 1
  fi
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
    echo "# Generated by scripts/lib-e2e-env.sh -- rename every container so a second"
    echo "# stack (compose project kb-$name-$stamp) does NOT collide with the live kb-* names."
    echo "# Auto-generated; do not edit. Regenerated on every e2e_isolate."
    echo "services:"
    for s in $svcs; do
      printf '  %s:\n    container_name: kb-%s-%s-%s\n' "$s" "$name" "$stamp" "$s"
    done
  } > "$override"

  # Provision the isolated values the STANDARD way: `make bootstrap` make-
  # tunables (KB_HOST / OLLAMA_HOST / OCR_ENABLED), which bootstrap.sh force-
  # persists into .env. KB_HOST_PORT is NOT passed -- bootstrap derives it from
  # KB_HOST=http://localhost:<port> (exercises the derivation path). The clone's
  # .env.template is NEVER mutated, so it provisions like a live
  # `make bootstrap KB_HOST=...`. OCR is passed ONLY when the caller provides it
  # (empty = leave .env.template's default; matches bootstrap's "no tunable =
  # no change" idempotency).
  local ocr_arg=()
  [ -n "$ocr" ] && ocr_arg=(OCR_ENABLED="$ocr")

  # Use the LIVE repo's .env as the clone's TEMPLATE (the operator's current
  # config: model + RAG knobs, image tags, OCR_ENABLED, KB_DOMAIN, ...) so the
  # iso env is prod-exact, not .env.template defaults. The live .env is
  # gitignored (git clone does NOT bring it), so seed it from $E2E_SRC here.
  # `make bootstrap` (next) is idempotent on an EXISTING .env: it keeps the
  # live values and force-persists ONLY the iso de-confliction overrides --
  # KB_HOST (the auto-picked port) + the re-derived KB_HOST_PORT -- so the
  # clone's port never collides with the live stack's. The container-prefix
  # de-confliction is compose-level (COMPOSE_PROJECT_NAME + the generated
  # override file above; never in .env), so it is untouched here. A missing
  # live .env (fresh source repo) -> bootstrap falls back to .env.template.
  if [ -f "$E2E_SRC/.env" ]; then
    cp "$E2E_SRC/.env" "$clone/.env"
    echo "==> seeded clone .env from the live $E2E_SRC/.env (template); bootstrap applies the iso port override"
  fi
  echo "==> make bootstrap (seed .env/.env.local for $clone, port $port${ocr:+, OCR=$ocr})"
  make bootstrap KB_HOST="$kb_host" OLLAMA_HOST="$OLLAMA_HOST" \
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
  E2E_STAMP="$stamp"
  export E2E_NAME E2E_PORT E2E_CLONE E2E_KB_HOST E2E_STAMP
}

# Non-destructive provision of the isolated stack: start, wait for /health, then
# create the admin account + provision the admin API key + one ephemeral
# throwaway user (its key written to the clone .env.local as KB_API_KEY by
# e2e_ephemeral_user). Call AFTER e2e_isolate. Does NOT run rag-config,
# projects-bootstrap, or gdrive (a test that needs those calls them itself). OCR
# is honored per e2e_isolate's bootstrap.
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
  # Match `make provision` steps 6-8 so the iso env is prod-exact (the ONLY diffs
  # from prod are KB_HOST + the ephemeral KB_API_KEY): projects-bootstrap enables
  # workspace.knowledge + sharing.public_knowledge (test_05 user-key KB create
  # 401s without it), rag-config sets the strict-grounding RAG template + syncs
  # rag.ollama.base_url, kb-bm25-init creates the ParadeDB pg_search extension +
  # BM25 index the patch-10 FTS arm queries (without it the ||| arm errors ->
  # langchain per-query full-collection fallback; document_chunk exists by the
  # /health wait). Both hit the OWUI admin API via Caddy at KB_HOST (not the
  # api-gateway), so the stack-healthy wait above covers them. kb-bootstrap is
  # deliberately excluded here: the at-scale path runs it (needs the gdrive KB
  # before gdrive-sync); the deterministic path uses synthetic fixtures only.
  make projects-bootstrap || return 1
  make rag-config || return 1
  make kb-bm25-init || return 1
  e2e_ephemeral_user || return 1
}

# AT-SCALE provision of the isolated stack: the comprehensive path
# (re-bootstrap + [OCR model pull] + preflight + image rebuild (api-gateway +
# postgres + openwebui + [markitdown-ocr]) + start + wait healthy + admin-signup
# + api-keys + ephemeral user + projects-bootstrap + rag-config + kb-bm25-init +
# kb-bootstrap KB=gdrive + gdrive-sync + make ci). This is the
# at-scale variant of e2e_provision: it rebuilds the locally-built images and
# syncs the REAL gdrive corpus (rclone). test_09_gdrive_index (the comprehensive
# at-scale e2e) uses it via the iso_env_named(..., at_scale=True) fixture.
#
# Call AFTER e2e_isolate (which seeded .env from the live template + .env.local
# with the admin account). The caller's deny-list (.kb-ignore chain, or the
# old .exclude.conf/gdrive-exclude.conf which the conftest fixture translates)
# must ALREADY be copied into the clone (the conftest fixture does this -- the
# provision bash has no E2E_SRC to reach the source repo). No clean-all: a fresh stamped clone
# (e2e_isolate reserves a new empty leaf each run) has no prior .env/./data to
# wipe, so the within-clone wipe was redundant; the live-.env template +
# .env.local (admin creds + secrets) survive straight through. Every bare
# `docker compose` / `make` honors COMPOSE_PROJECT_NAME/COMPOSE_FILE from the
# clean child env (set by e2e_isolate), so build/start target THIS iso project,
# never the live stack. rclone uses the host ~/.config/rclone/rclone.conf via
# HOME (carried in the child env). No fallback: any step failing aborts (return 1).
e2e_provision_at_scale() {
  echo "==> at-scale provision: image rebuild + preflight + real rclone gdrive corpus (fresh clone)."
  test -f .env.local || { echo "FAIL  no .env.local (no admin creds) -- run e2e_isolate first" >&2; return 1; }
  set -a; . ./.env; . ./.env.local; set +a
  # Capture KB_HOST / OLLAMA_HOST from the just-sourced .env (the iso port +
  # Ollama URL e2e_isolate persisted) and re-forward them to the internal
  # `make bootstrap` as make-tunables. bootstrap is idempotent on the live-.env
  # template e2e_isolate seeded: it keeps the operator config + force-persists
  # ONLY KB_HOST (+ re-derives KB_HOST_PORT) + OLLAMA_HOST -- the iso port
  # override, never the live stack's. No clean-all: .env (live template) +
  # .env.local (admin creds + secrets) survive straight through.
  _E2E_KB_HOST="${KB_HOST:-}"; _E2E_OLLAMA_HOST="${OLLAMA_HOST:-}"
  [ -n "${OPENWEBUI_FIRST_USER:-}" ] && [ -n "${OPENWEBUI_FIRST_PASSWORD:-}" ] \
    || { echo "FAIL  OPENWEBUI_FIRST_USER/PASSWORD not set in .env.local -- fill them first" >&2; return 1; }
  make bootstrap KB_HOST="$_E2E_KB_HOST" OLLAMA_HOST="$_E2E_OLLAMA_HOST" || return 1
  # Pull the OCR vision model before preflight (preflight hard-fails on a missing
  # OCR model when OCR_ENABLED=true). Pull only the OCR model, NOT full
  # `make pull-models` (that `ollama rm`s + recreates GRAPHITI_MODEL, disrupting the
  # base LLM). Honors OCR_ENABLED=false.
  if [ "${OCR_ENABLED:-true}" = "true" ]; then
    echo "==> pulling OCR vision model: ${OCR_MODEL:-deepseek-ocr}"
    ollama pull "${OCR_MODEL:-deepseek-ocr}" || return 1
  fi
  make preflight || return 1
  # Rebuild locally-built images whose code changed since the last run (`up -d`
  # without --build reuses the existing image). api-gateway is stdlib-only
  # (fast). markitdown-ocr is rebuilt (gated on OCR_ENABLED) so the at-scale run
  # uses current OCR code. postgres (kb-postgres: pg_search baked in; tag encodes
  # PG_SEARCH_VERSION) + openwebui (patch-10 BM25 FTS arm; tag encodes the
  # patch-suffix) are rebuilt so the at-scale run uses current code, not a stale
  # image from a prior run.
  docker compose build api-gateway || return 1
  docker compose build postgres openwebui || return 1
  if [ "${OCR_ENABLED:-true}" = "true" ]; then
    docker compose build markitdown-ocr || return 1
  fi
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
  e2e_ephemeral_user || return 1
  make projects-bootstrap || return 1
  make rag-config || return 1
  make kb-bm25-init || return 1
  make kb-bootstrap KB=gdrive || return 1
  make gdrive-sync || return 1
  echo "==> make ci (provision the clone .venv for the in-clone suite)"
  make ci || return 1
}

# Create ONE ephemeral throwaway user for this clone env (admin key -> gateway
# POST /admin/users via scripts/e2e_user.py) and write its key as KB_API_KEY into
# the clone's .env.local. Tests read KB_API_KEY via load_env (tests/lib.sh
# sources .env.local), so the key MUST be on disk -- an export alone is fragile
# across the pytest->bash subprocess boundary + the nested clones test_08/12
# load_env from. The user is destroyed with the clone (teardown root-rms the
# clone + its DB); no per-user clean-delete is needed. One role=user ephemeral
# user satisfies every test (test_05 needs role=user + a *-granted KB it creates
# itself with the admin key; test_06 cross-user is admin-vs-user; test_07
# mints/deletes its own temp users). Call AFTER `make api-keys` (needs
# OPENWEBUI_ADMIN_API_KEY in .env.local). Idempotent on .env.local (upsert).
e2e_ephemeral_user() {
  local domain email
  domain="$(grep -E '^KB_DOMAIN=' .env | head -1 | cut -d= -f2- || true)"
  [ -n "$domain" ] || { echo "FAIL  KB_DOMAIN not set in the clone .env (ephemeral user email)" >&2; return 1; }
  email="temp-${E2E_STAMP}@${domain}"
  # `make api-keys` recreates the api-gateway at its tail (ocr-config.sh when
  # OCR_ENABLED=true, then `docker compose up -d api-gateway` for the admin
  # key) and returns once the container Starts, before the gateway app binds
  # its port. Wait for /health before POSTing /admin/users, else the create
  # races the gateway startup (a transient connection-refused the prior
  # 2>/dev/null hid, surfacing only as "ephemeral user create failed").
  local h="${KB_HOST:?}" i=0
  until curl -sf "$h/health" >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -lt 60 ] || { echo "FAIL  gateway not healthy in 120s ($h/health) before ephemeral user create" >&2; return 1; }
    sleep 2
  done
  echo "==> create ephemeral test user $email (admin key -> gateway POST /admin/users)"
  # The POST + JSON parse + kb_api_key extract + .env.local atomic upsert run in
  # Python (scripts/e2e_user.py), NOT a shell capture of `make users-create`:
  # under `make test-iso-*` a recursive make prints "Entering/Leaving
  # directory" --print-directory banners to stdout, which a `$(make users-create
  # ...)` capture pulls into the JSON and breaks json.load at char 0. Shell only
  # sources the clone .env/.env.local (so Python sees KB_HOST +
  # OPENWEBUI_ADMIN_API_KEY) and gates on the exit code.
  ( set -a; . ./.env; . ./.env.local; set +a; \
    python3 scripts/e2e_user.py --email "$email" --name "E2E" --role user --env-local .env.local ) \
    || { echo "FAIL  ephemeral user create failed for $email (see e2e_user.py output above)" >&2; return 1; }
}

# --- teardown model (proliferation) ----------------------------------------
# A run leaves a datetime-stamped clone at .test-env/<stamp>-<name>/ with docker
# STOPPED (GPU freed) but the clone KEPT -- a commit-in-clone-first workflow may
# hold unmerged commits, so clones are NOT auto-removed on success. The host PORT
# is serial: a failed run still holding E2E_PORT must be cleaned before a re-run
# on the same port. Removal is manual hygiene:
#   make clean-test NAME=<name> [STAMP=<stamp>]  -- ONE run (latest stamp if no
#                                                   STAMP); stops docker + removes clone.
#   make clean-tests [NAME=<name>]               -- flush EVERY
#                                                   .test-env/<stamp>-<name>/ clone
#                                                   + orphan docker.
# compose.yml uses only ./data bind mounts (no named volumes), so `down
# --remove-orphans` suffices (no --volumes needed). Sweeps use the compose
# PROJECT LABEL (exact match), never a container-name prefix: live services like
# kb-api-gateway / kb-markitdown-ocr contain hyphens, so a kb-<name>-* NAME prefix
# would match the live stack. The live project is the dir basename
# (`knowledgebase`), never kb-*, so label sweeps are safe.

# Resolve the stamp for e2e_down: explicit arg > the in-process E2E_STAMP set by
# e2e_isolate > the newest .test-env/<stamp>-<name>/ leaf for this name (by STAMP
# PREFIX, not mtime -- a dir's mtime changes when a direct child is added, so an
# older clone that later gained a child would win on mtime and the wrong run
# would be torn down) > "" (no clone found). Returns the 15-char stamp
# (empty == not found).
_e2e_resolve_stamp() {
  local name="$1" stamp="${2:-}" parent newest=""
  [ -n "$stamp" ] && { printf '%s' "$stamp"; return 0; }
  [ -n "${E2E_STAMP:-}" ] && { printf '%s' "$E2E_STAMP"; return 0; }
  parent="$E2E_SRC/.test-env"
  if [ -d "$parent" ]; then
    # Leaves are <stamp>-<name> (stamp = YYYYMMDD-HHMMSS, 15 chars). Filter by
    # the name suffix (chars 16+), then pick the highest stamp prefix (chars
    # 0-14) -- chronological, since the stamp sorts lexicographically under C.
    newest="$(
      while IFS= read -r d; do
        [ -n "$d" ] || continue
        local b="$(basename "$d")"
        [ "${b:16}" = "$name" ] || continue
        printf '%s\n' "${b:0:15}"
      done < <(find "$parent" -maxdepth 1 -mindepth 1 -type d \
        -name '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]-*' 2>/dev/null) \
      | LC_ALL=C sort -r | head -1)"
  fi
  printf '%s' "$newest"   # empty == no clone found
}

# Stop + remove the kb-<name>-<stamp> docker project (compose down + label sweep),
# KEEP the clone dir. Safe anytime (no-op if no containers). The success path: GPU
# freed, clone kept for inspection / commit landing. Stamp is mandatory (fail
# loud on empty -- no caller passes an un-stamped project anymore).
e2e_stop_docker() {
  local name="$1" stamp="$2" clone_path="${3:-}" proj clone
  [ -n "$stamp" ] || { echo "FAIL  e2e_stop_docker: empty stamp for name=$name" >&2; return 1; }
  proj="kb-$name-$stamp"
  # An explicit clone_path (passed by the sweeps, which know the exact dir) is
  # used as-is; otherwise derive the new-layout path. `compose down` runs from
  # the clone dir so the compose NETWORK is removed (the label sweep below
  # removes containers only, not the network).
  clone="${clone_path:-$E2E_SRC/.test-env/$stamp-$name}"
  local orphans had
  had="$(docker ps -aq --filter label=com.docker.compose.project="$proj" 2>/dev/null | wc -l)"
  if [ -d "$clone" ]; then
    (
      cd "$clone" || exit 0
      # Detect the override file e2e_isolate wrote instead of reconstructing its
      # name from $name: the filesystem is the single source of truth, so a
      # naming-convention drift between e2e_isolate and here cannot make
      # `compose down` load the wrong COMPOSE_FILE (which would skip the
      # override, leave the renamed containers' network, and leak it). A missing
      # override (legacy clone) -> bare compose.yml, same as before.
      cf="compose.yml"
      ov="$(ls compose.*.override.yml 2>/dev/null | head -1)"
      [ -n "$ov" ] && cf="compose.yml:$ov"
      COMPOSE_PROJECT_NAME="$proj" COMPOSE_FILE="$cf" \
        docker compose down --remove-orphans 2>/dev/null || true
    )
  fi
  # Label-based orphan sweep (exact project label) -- works even if the clone dir
  # is gone. Safe: the live project is `knowledgebase`, never kb-<name>[-<stamp>].
  orphans="$(docker ps -aq --filter label=com.docker.compose.project="$proj" 2>/dev/null || true)"
  [ -n "$orphans" ] && docker rm -f $orphans >/dev/null 2>&1 || true
  if [ "$had" -gt 0 ] || [ -n "$orphans" ]; then
    echo "==> stopped $proj (clone kept at $clone)."
  fi
  return 0
}

# Root-remove a clone dir. Its ./data is root/neo4j-owned (OWUI/Neo4j write
# bind-mount files the host user cannot delete), so a host rm -rf fails midway --
# the caller downs the project first (e2e_stop_docker), then this root-rms the
# tree via an alpine container (same pattern clean-all uses for ./data).
_e2e_rm_clone() {
  local clone="$1"
  [ -e "$clone" ] || { echo "==> no $clone to remove."; return 0; }
  docker run --rm -v "$clone:/data" alpine sh -c "rm -rf /data/* /data/.[!.]* /data/..?*" 2>/dev/null || true
  rm -rf "$clone" 2>/dev/null || true
  if [ ! -e "$clone" ]; then
    echo "==> removed $clone."
  else
    echo "WARN  $clone still on disk (partial); recover with: make clean-tests" >&2
  fi
}

# Stop docker (e2e_stop_docker) + root-remove the clone. Used by the quick tests'
# EXIT traps (their clones hold no commits) and `make clean-test`. Without a
# stamp, resolves E2E_STAMP or the newest .test-env/<stamp>-<name>/ leaf for this
# name; an empty resolution prints "no clone found" and returns (nothing to tear
# down).
e2e_down() {
  local name="$1" stamp clone
  stamp="$(_e2e_resolve_stamp "$name" "${2:-}")"
  cd "$E2E_SRC" || true
  if [ -z "$stamp" ]; then
    echo "==> no .test-env/<stamp>-${name}/ clone found for name=${name}."
    return 0
  fi
  clone="$E2E_SRC/.test-env/$stamp-$name"
  e2e_stop_docker "$name" "$stamp" "$clone"
  _e2e_rm_clone "$clone"
  return 0
}

# Print a clone's HEAD + any commits not on main (so the operator sees what
# would be lost before clean-tests removes it). Best-effort; never fails the flush.
_e2e_warn_commits() {
  local clone="$1"
  [ -d "$clone/.git" ] || return 0
  local head unmerged
  head="$(git -C "$clone" rev-parse --short HEAD 2>/dev/null || true)"
  unmerged="$(git -C "$clone" rev-list --count main..HEAD 2>/dev/null || true)"
  [ -n "$head" ] || return 0
  if [ -n "$unmerged" ] && [ "$unmerged" -gt 0 ]; then
    echo "    HEAD $head ($unmerged commit(s) not on main):"
    git -C "$clone" log --oneline main..HEAD 2>/dev/null | sed 's/^/      /' | head -10
  else
    echo "    HEAD $head (0 commits not on main; safe to remove)."
  fi
}

# Flush ALL stamped clones + orphan docker (manual hygiene; `make clean-tests`).
# For each clone, print HEAD + unmerged commits BEFORE removing (a warning, not a
# hard refuse -- clean-tests is the explicit "I'm done with these" flush).
# NAME=<name> flushes only that name's clones; else every clone under .test-env/.
# The orphan sweep targets only STAMPED projects (kb-*-[stamp]) whose clone dir
# is GONE (truly stranded) -- the live project `knowledgebase` never matches, and
# a project whose clone still exists is left to its run.
e2e_clean_tests() {
  local name="${1:-}" d s b n proj
  cd "$E2E_SRC" || return 1

  # `env` is the shared parent (.test-env/), not a clone name; refuse it so
  # `make clean-tests NAME=env` does not treat the shared parent as a clone.
  if [ "$name" = "env" ]; then
    echo "FAIL  'env' is the shared .test-env/ parent, not a clone name." >&2
    return 1
  fi

  # .test-env/<stamp>-<name>/ leaves (stamp = YYYYMMDD-HHMMSS, 15 chars; name =
  # chars 16+). Pass the exact path so e2e_stop_docker runs `compose down` from
  # the clone dir (network removal), not just the label sweep.
  local env_parent="$E2E_SRC/.test-env"
  if [ -d "$env_parent" ]; then
    while IFS= read -r d; do
      [ -n "$d" ] || continue
      b="$(basename "$d")"
      s="${b:0:15}"; n="${b:16}"
      if [ -n "$name" ] && [ "$n" != "$name" ]; then continue; fi
      echo "==> flush $d"
      _e2e_warn_commits "$d"
      e2e_stop_docker "$n" "$s" "$d"
      _e2e_rm_clone "$d"
    done < <(find "$env_parent" -maxdepth 1 -mindepth 1 -type d \
      -name '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]-*' 2>/dev/null | sort)
  fi

  # Orphan sweep: stranded STAMPED projects whose clone dir is GONE (interrupted
  # runs). Inspect only compose-labeled containers; match the STAMP pattern on
  # the project label. The live project `knowledgebase` has no stamp, so it is
  # never matched. When NAME= is given, restrict to that name's projects (a live
  # run of a DIFFERENT name is not touched). Skip a project whose clone still
  # exists -- it is not stranded (its clone was flushed above, or it is a live
  # run). proj = kb-<name>-<stamp>; the trailing 15 chars are the stamp, the rest
  # after `kb-` and before `-<stamp>` is the name.
  local orphan_ids=""
  while IFS= read -r cid; do
    [ -n "$cid" ] || continue
    proj="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$cid" 2>/dev/null || true)"
    case "$proj" in
      kb-*-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) : ;;
      *) continue ;;
    esac
    s="${proj: -15}"
    n="${proj#kb-}"; n="${n%-$s}"
    if [ -n "$name" ] && [ "$n" != "$name" ]; then continue; fi
    if [ -d "$E2E_SRC/.test-env/$s-$n" ]; then continue; fi
    orphan_ids="$orphan_ids $cid"
  done < <(docker ps -aq --filter label=com.docker.compose.project 2>/dev/null)
  if [ -n "${orphan_ids# }" ]; then
    docker rm -f $orphan_ids >/dev/null 2>&1 || true
    echo "==> swept stranded stamped e2e containers (no matching clone)."
  fi
  return 0
}