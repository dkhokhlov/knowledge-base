#!/usr/bin/env bash
# Common helpers for the system integration tests. Sourced by tests/test_*.sh.
# The stack must be running (make start) and .env.local must be populated.
set -u

# Repo root is one level above tests/.
KB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$KB_ROOT"

# Color output only when stdout is a terminal.
if [ -t 1 ]; then
  C_PASS=$'\033[32m'; C_FAIL=$'\033[31m'; C_INFO=$'\033[36m'; C_RST=$'\033[0m'
else
  C_PASS=''; C_FAIL=''; C_INFO=''; C_RST=''
fi

PASS=0
FAIL=0

pass() { printf '%sPASS%s  %s\n' "$C_PASS" "$C_RST" "$1"; PASS=$((PASS + 1)); }
fail() { printf '%sFAIL%s  %s\n' "$C_FAIL" "$C_RST" "$1"; FAIL=$((FAIL + 1)); }
section() { printf '\n%s== %s ==%s\n' "$C_INFO" "$1" "$C_RST"; }

# Load the config-of-record (.env) and the gitignored secrets (.env.local).
# Capture a `make test KB_DOMAIN=<d>` override before `set -a; . ./.env` (which
# would clobber it with .env's KB_DOMAIN); restore it after sourcing.
load_env() {
  local _kb_domain_ovr="${KB_DOMAIN:-}"
  set -a
  # shellcheck source=/dev/null
  . ./.env
  # shellcheck source=/dev/null
  . ./.env.local
  set +a
  if [ -n "$_kb_domain_ovr" ]; then export KB_DOMAIN="$_kb_domain_ovr"; fi
}

# Record a failure for each named env var that is empty. Return 1 if any missing.
require_env() {
  local v missing=0
  for v in "$@"; do
    if [ -z "${!v:-}" ]; then fail "env $v not set in .env.local"; missing=1; fi
  done
  [ "$missing" -eq 0 ]
}

# HTTP status code for a URL. Extra args are passed to curl.
http_code() {
  local url="$1"; shift
  curl -s -o /dev/null -w '%{http_code}' "$@" "$url"
}

# Resolve the single public URL (KB_HOST). Caddy fronts OWUI at the root and
# the api-gateway at /memory/*, /admin/users, /health. Falls back to synth from
# KB_HOST_PORT. Call after load_env.
kb_host() {
  printf '%s' "${KB_HOST:-http://localhost:${KB_HOST_PORT:-3000}}"
}

# Bail early if the stack is not up and healthy. Exits with status 2.
# Retries: Caddy returns a transient 502 when an upstream (OWUI/api-gateway) is
# briefly unavailable -- e.g. under the async gdrive extraction/embedding drain
# load right after `make gdrive-index`. A single non-retried probe would flake
# on that blip; a genuinely-down stack still fails after the retries (5 probes,
# --max-time 5 each + 2s sleep -> ~30s worst case; a 200 returns on the first).
require_stack_up() {
  local h code try
  h="$(kb_host)"
  for try in 1 2 3 4 5; do
    code=$(http_code "$h/health" --connect-timeout 2 --max-time 5)
    [ "$code" = 200 ] && return 0
    sleep 2
  done
  printf 'Stack not healthy (KB_HOST=%s /health=%s). Run: make start && make health\n' \
    "$h" "$code" >&2
  exit 2
}

# Print a summary and exit non-zero if anything failed. Call once at script end.
finish() {
  printf '\n%s%d passed, %d failed%s\n' "$C_INFO" "$PASS" "$FAIL" "$C_RST"
  [ "$FAIL" -eq 0 ]
}