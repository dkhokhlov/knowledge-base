#!/usr/bin/env bash
# Create .env.local (gitignored) with a freshly generated WEBUI_SECRET_KEY,
# ensure the ./data bind-mount tree exists, and remind the user to set
# GRAPHITI_API_TOKEN. Idempotent.
set -eu

cd "$(dirname "$0")/.."

if [ ! -f .env.local ]; then
  if [ ! -f .env.local.example ]; then
    printf 'FAIL  .env.local.example missing (cannot scaffold .env.local)\n' >&2
    exit 1
  fi
  cp .env.local.example .env.local
  printf '  created .env.local from .env.local.example\n'
fi

if ! grep -qE '^WEBUI_SECRET_KEY=.+$' .env.local; then
  KEY="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))')"
  sed -i "s|^WEBUI_SECRET_KEY=.*|WEBUI_SECRET_KEY=${KEY}|" .env.local
  printf '  generated WEBUI_SECRET_KEY into .env.local\n'
else
  printf '  WEBUI_SECRET_KEY already present in .env.local (kept)\n'
fi

if ! grep -qE '^GRAPHITI_API_TOKEN=.+$' .env.local; then
  printf '\n  ACTION NEEDED: set GRAPHITI_API_TOKEN in .env.local before `make start`.\n' >&2
else
  printf '  GRAPHITI_API_TOKEN already set in .env.local\n'
fi

mkdir -p data/neo4j/data data/neo4j/logs data/openwebui
printf '  ensured ./data/{neo4j/data,neo4j/logs,openwebui} exist\n'
printf '\nBootstrap done. Next: edit .env.local (set GRAPHITI_API_TOKEN), then make preflight && make start\n'