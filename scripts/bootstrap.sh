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
# read-only ./gdrive bind mount (owner-only from rclone) is readable. Written to
# .env (compose requires it; .env is read by every `docker compose` command, so
# `:?` holds for stop/clean/logs/ps too -- unlike .env.local, which only
# reach-parse targets that source it). Kept if already set (operator override);
# clean-all wipes .env so a fresh bootstrap re-derives from the current user.
ensure_value .env HOST_UID "$(id -u)"
ensure_value .env HOST_GID "$(id -g)"

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

# COMPOSE_PROFILES (the ocr sidecar compose profile) is DERIVED from OCR_ENABLED
# and force-synced into .env every bootstrap (non-editable; re-synced so manual
# drift is repaired). `docker compose` reads COMPOSE_PROFILES from .env for EVERY
# command, so the sidecar is always in the project when enabled -- no --profile
# flag, no shell export. To disable OCR: `make clean-all && make provision
# OCR_ENABLED=false` (this persists OCR_ENABLED=false + an empty COMPOSE_PROFILES).
_CP=""; [ "$OCR_ENABLED_VAL" = "true" ] && _CP="ocr"
if grep -qE '^COMPOSE_PROFILES=' .env; then
  sed -i "s|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=${_CP}|" .env
else
  printf 'COMPOSE_PROFILES=%s\n' "$_CP" >> .env
fi
# Persist a `make bootstrap OCR_ENABLED=<val>` override into .env so the whole
# chain (pull-models, api-keys, start) and lifecycle read it durably -- the
# override DEFINES the profile, it is not transient. Without an override the
# existing .env OCR_ENABLED is the source of truth (idempotent, not rewritten).
if [ -n "${OCR_ENABLED:-}" ]; then
  if grep -qE '^OCR_ENABLED=' .env; then
    sed -i "s|^OCR_ENABLED=.*|OCR_ENABLED=${OCR_ENABLED}|" .env
  else
    printf 'OCR_ENABLED=%s\n' "$OCR_ENABLED" >> .env
  fi
  printf '  persisted OCR_ENABLED=%s into .env (defines the compose profile)\n' "$OCR_ENABLED"
fi

# KB_HOST is the single source for the public URL (shell env or make-tunable;
# see .env.template). KB_HOST_PORT (the Caddy host bind) is DERIVED from
# KB_HOST's URL port here and persisted into .env (compose cannot parse a URL,
# so it still reads KB_HOST_PORT from .env). Changing KB_HOST alone moves the
# bind too -- one var, not two. KB_HOST_PORT is re-derived on EVERY bootstrap
# (overwrites any prior .env value). Pass KB_HOST_PORT=... as a make-tunable
# ONLY for the tunnel case (client URL port != bind port, e.g.
# KB_HOST=http://tunnel:443 KB_HOST_PORT=3000); an explicit tunable wins over
# the derivation. The isolated e2e (test-e2e-iso) pins KB_HOST (+ OLLAMA_HOST)
# the same way so they survive test-e2e's internal clean-all (rm .env) ->
# bootstrap; it no longer passes KB_HOST_PORT (the port is derived from
# KB_HOST=http://localhost:<e2e-port>).
#
# Resolve KB_HOST: process env (shell/make-tunable) first, then a prior
# persisted value in the existing .env (so a CLEAN-SHELL re-bootstrap -- no
# operator profile -- reuses the last persisted KB_HOST instead of failing).
# Above, .env was cp'd from .env.template only when missing, so at this point
# .env exists and may hold a prior KB_HOST.
_kb_host="${KB_HOST:-}"
[ -z "$_kb_host" ] && _kb_host="$(grep -E '^KB_HOST=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
if [ -z "$_kb_host" ]; then
  printf 'FAIL  KB_HOST not set -- export KB_HOST=http://<host>:<port> (bootstrap derives KB_HOST_PORT from it)\n' >&2
  exit 1
fi
_kb_host_port_ovr="${KB_HOST_PORT:-}"   # explicit make-tunable (tunnel) ONLY
# Derive the bind port from KB_HOST's AUTHORITY port (default 3000 = OWUI's
# natural port when the URL has no explicit port, e.g. a TLS proxy fronting
# Caddy on :3000). Parse the authority, not "last colon in the string" -- a
# naive ${_kb_host##*:} misbinds http://h:3010/path:999 -> 999 and drops the
# port on http://h:3010?x=1 / http://h:3010#frag.
_derived=3000
_auth="${_kb_host#*://}"                # strip scheme
_auth="${_auth%%[/?#]*}"                # cut path/query/fragment at first delimiter
case "$_auth" in
  \[*\]:*)  _p="${_auth#*]:}";;          # [ipv6]:port  -> after ']:'
  *:*)      _p="${_auth##*:}";;          # host:port    -> after last ':'
  *)        _p="";;                      # host (no port)
esac
case "$_p" in *[!0-9]*|"") : ;; *) _derived="$_p" ;; esac
_bind="${_kb_host_port_ovr:-$_derived}"  # explicit tunnel override wins
# Persist KB_HOST_PORT (always -- compose needs it; re-derived each bootstrap).
if grep -qE '^KB_HOST_PORT=' .env; then
  sed -i "s|^KB_HOST_PORT=.*|KB_HOST_PORT=${_bind}|" .env
else
  printf 'KB_HOST_PORT=%s\n' "$_bind" >> .env
fi
printf '  persisted KB_HOST_PORT=%s into .env (derived from KB_HOST=%s)\n' "$_bind" "$_kb_host"
# KB_HOST + OLLAMA_HOST: keep the existing force-persist-when-non-empty behavior
# (unchanged) -- KB_HOST persists for the e2e clone; OLLAMA_HOST as today. The
# live operator, who sets these in the shell env, gets them persisted on the
# first bootstrap (.env wins thereafter, same as OLLAMA_HOST).
for _k in KB_HOST OLLAMA_HOST; do
  _v="${!_k:-}"
  [ -n "$_v" ] || continue
  if grep -qE "^${_k}=" .env; then
    sed -i "s|^${_k}=.*|${_k}=${_v}|" .env
  else
    printf '%s=%s\n' "$_k" "$_v" >> .env
  fi
  printf '  persisted %s=%s into .env\n' "$_k" "$_v"
done

chmod 600 .env.local
printf '  set .env.local permissions to 0600\n'

mkdir -p data/neo4j/data data/neo4j/logs data/openwebui
printf '  ensured ./data/{neo4j/data,neo4j/logs,openwebui} exist\n'

printf '\nBootstrap done. Next: make preflight && make start\n'
printf 'After start: make admin-signup (admin %s; password in .env.local OPENWEBUI_FIRST_PASSWORD),\n' "admin@${KB_DOMAIN}"
printf 'then make api-keys (admin + shared-agent keys), then provision accounts\n'
printf 'via the api-gateway (see README KB_API_KEY).\n'