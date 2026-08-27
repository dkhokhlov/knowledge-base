#!/usr/bin/env bash
# Create .env (from .env.template, if missing) and .env.local (gitignored),
# generate WEBUI_SECRET_KEY, lock .env.local to 0600, and ensure the ./data
# bind-mount tree exists. Idempotent: existing non-empty values are kept.
#
# Agents authenticate with KB_API_KEY (an Open Web UI per-account key); the
# api-gateway validates it against Open Web UI and authorizes per call. See
# README "KB_API_KEY & the api-gateway".
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  if [ ! -f .env.template ]; then
    printf 'FAIL  .env.template missing (cannot scaffold .env)\n' >&2
    exit 1
  fi
  cp .env.template .env
  printf '  created .env from .env.template — set OLLAMA_HOST (shell env or .env) before `make start`\n'
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

# ensure_value <file> <key> <value>: guarantee a non-empty <key>=<value> line in
# <file>. Like ensure_secret, but with a caller-supplied value (not random).
# Replaces an empty/quoted-empty/whitespace line in place; appends if the key is
# absent. Idempotent (kept if the sourced value is non-empty); verifies it landed.
ensure_value() {
  local file="$1" key="$2" val="$3"
  if secret_present "$file" "$key"; then
    printf '  %s already present in %s (kept)\n' "$key" "$file"
    return
  fi
  if grep -qE "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$file"
  else
    printf '%s=%s\n' "$key" "$val" >> "$file"
  fi
  printf '  set %s in %s\n' "$key" "$file"
  secret_present "$file" "$key" \
    || { printf 'FAIL  %s not set in %s\n' "$key" "$file" >&2; exit 1; }
}

if [ ! -f .env.local ]; then
  if [ ! -f .env.local.template ]; then
    printf 'FAIL  .env.local.template missing (cannot scaffold .env.local)\n' >&2
    exit 1
  fi
  cp .env.local.template .env.local
  printf '  created .env.local from .env.local.template\n'
fi

ensure_secret .env.local WEBUI_SECRET_KEY

# First (admin) account: email admin@<KB_DOMAIN> (KB_DOMAIN is in .env,
# default local.test) + a generated password. A `make bootstrap KB_DOMAIN=<d>`
# override wins; else read KB_DOMAIN from the .env bootstrap just created (plain
# KEY=VALUE; source in a subshell, default if unset or source fails). ensure_value
# keeps an existing non-empty value (operator edits / e2e-restore-creds survive a
# re-bootstrap); clean-all wipes .env.local so a fresh bootstrap recomputes for a
# new KB_DOMAIN.
KB_DOMAIN="${KB_DOMAIN:-$(. ./.env 2>/dev/null; printf '%s' "${KB_DOMAIN:-local.test}")}"
ensure_value .env.local OPENWEBUI_FIRST_USER "admin@${KB_DOMAIN}"
ensure_secret .env.local OPENWEBUI_FIRST_PASSWORD

# api-gateway run user: derive from the current user (id -u/id -g) so the
# read-only ./gdrive bind mount (owner-only from rclone) is readable. Kept if
# already set (operator override); clean-all wipes .env.local so a fresh
# bootstrap re-derives from the current user.
ensure_value .env.local HOST_UID "$(id -u)"
ensure_value .env.local HOST_GID "$(id -g)"

# markitdown-ocr service token (SECRET -> .env.local). Generated only when
# OCR_ENABLED=true (default; read from the .env bootstrap just created, or a
# `make bootstrap OCR_ENABLED=<val>` override which wins). Kept if already set;
# clean-all wipes .env.local so a fresh bootstrap regenerates. Skipped on an
# explicit OCR_ENABLED=false (no token -> the markitdown-ocr sidecar is not
# provisioned by the chain).
OCR_ENABLED_VAL="${OCR_ENABLED:-$(. ./.env 2>/dev/null; printf '%s' "${OCR_ENABLED:-true}")}"
if [ "$OCR_ENABLED_VAL" = "true" ]; then
  ensure_secret .env.local OCR_SERVICE_TOKEN
else
  printf '  OCR_ENABLED=%s — not generating OCR_SERVICE_TOKEN (markitdown-ocr disabled)\n' "$OCR_ENABLED_VAL"
fi

chmod 600 .env.local
printf '  set .env.local permissions to 0600\n'

mkdir -p data/neo4j/data data/neo4j/logs data/openwebui
printf '  ensured ./data/{neo4j/data,neo4j/logs,openwebui} exist\n'

printf '\nBootstrap done. Next: make preflight && make start\n'
printf 'After start: make admin-signup (admin %s; password in .env.local OPENWEBUI_FIRST_PASSWORD),\n' "admin@${KB_DOMAIN}"
printf 'then make api-keys (admin + shared-agent keys), then provision accounts\n'
printf 'via the api-gateway (see README KB_API_KEY).\n'