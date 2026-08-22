#!/usr/bin/env bash
# Create .env (from .env.example, if missing) and .env.local (gitignored),
# generate WEBUI_SECRET_KEY, lock .env.local to 0600, and ensure the ./data
# bind-mount tree exists. Idempotent: existing non-empty values are kept.
#
# Agents authenticate with KB_API_KEY (an Open Web UI per-account key); the
# kb-gateway validates it against Open Web UI and authorizes per call. See
# README "KB_API_KEY & the kb-gateway".
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  if [ ! -f .env.example ]; then
    printf 'FAIL  .env.example missing (cannot scaffold .env)\n' >&2
    exit 1
  fi
  cp .env.example .env
  printf '  created .env from .env.example — set OLLAMA_HOST (shell env or .env) before `make start`\n'
fi

gen_hex() {
  openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))'
}

# secret_present <file> <key>: exit 0 if sourcing <file> yields a non-empty
# value for <key>. Unsets any inherited value first (so a key exported in the
# shell env cannot make an absent/malformed .env.local look valid), sources in
# a subshell so bash itself strips surrounding quotes, inline comments and
# whitespace (the same parse the stack uses), and treats a source failure as
# not-present. So "", '', "   ", '"" # comment' and a broken file all read as
# empty (rejected/regenerated).
secret_present() {
  local file="$1" key="$2"
  (
    unset "$key"
    . "$file" 2>/dev/null || exit 1
    [ -n "${!key:-}" ]
  )
}

# ensure_secret <file> <key>: guarantee a non-empty <key>=<value> line in <file>.
# Replaces an empty/quoted-empty/whitespace line in place; appends if the key is
# absent. Generates a fresh hex value via gen_hex. Idempotent (kept if the sourced
# value is non-empty); verifies it landed.
ensure_secret() {
  local file="$1" key="$2" val
  if secret_present "$file" "$key"; then
    printf '  %s already present in %s (kept)\n' "$key" "$file"
    return
  fi
  val="$(gen_hex)"
  if grep -qE "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$file"    # replace empty/quoted-empty/whitespace line in place
  else
    printf '%s=%s\n' "$key" "$val" >> "$file"       # key absent -> append
  fi
  printf '  generated %s into %s\n' "$key" "$file"
  secret_present "$file" "$key" \
    || { printf 'FAIL  %s not set in %s\n' "$key" "$file" >&2; exit 1; }
}

if [ ! -f .env.local ]; then
  if [ ! -f .env.local.example ]; then
    printf 'FAIL  .env.local.example missing (cannot scaffold .env.local)\n' >&2
    exit 1
  fi
  cp .env.local.example .env.local
  printf '  created .env.local from .env.local.example\n'
fi

ensure_secret .env.local WEBUI_SECRET_KEY

chmod 600 .env.local
printf '  set .env.local permissions to 0600\n'

mkdir -p data/neo4j/data data/neo4j/logs data/openwebui data/oikb
printf '  ensured ./data/{neo4j/data,neo4j/logs,openwebui,oikb} exist\n'

printf '\nBootstrap done. Next: make preflight && make start\n'
printf 'After start + admin signup: make api-keys (admin + shared-agent keys),\n'
printf 'then provision accounts via the kb-gateway (see README KB_API_KEY).\n'