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
load_env() {
  set -a
  # shellcheck source=/dev/null
  . ./.env
  # shellcheck source=/dev/null
  . ./.env.local
  set +a
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

# Bail early if the stack is not up and healthy. Exits with status 2.
require_stack_up() {
  local g o
  g=$(http_code "http://localhost:${GRAPHITI_HOST_PORT:-8000}/health" --connect-timeout 2)
  o=$(http_code "http://localhost:${OPENWEBUI_HOST_PORT:-3000}/health" --connect-timeout 2)
  if [ "$g" != "200" ] || [ "$o" != 200 ]; then
    printf 'Stack not healthy (graphiti=%s openwebui=%s). Run: make start && make health\n' \
      "$g" "$o" >&2
    exit 2
  fi
}

# Print a summary and exit non-zero if anything failed. Call once at script end.
finish() {
  printf '\n%s%d passed, %d failed%s\n' "$C_INFO" "$PASS" "$FAIL" "$C_RST"
  [ "$FAIL" -eq 0 ]
}