#!/usr/bin/env bash
# Create .env (from .env.example, if missing) and .env.local (gitignored),
# generate both secrets (WEBUI_SECRET_KEY and GRAPHITI_API_TOKEN), lock .env.local
# to 0600, ensure the ./data bind-mount tree exists, and print the
# GRAPHITI_API_TOKEN so it can be copied into MCP clients.
# Idempotent: existing non-empty values are kept.
set -eu

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  if [ ! -f .env.example ]; then
    printf 'FAIL  .env.example missing (cannot scaffold .env)\n' >&2
    exit 1
  fi
  cp .env.example .env
  printf '  created .env from .env.example — set OLLAMA_BASE_URL before `make start`\n'
fi

gen_hex() {
  openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))'
}

if [ ! -f .env.local ]; then
  if [ ! -f .env.local.example ]; then
    printf 'FAIL  .env.local.example missing (cannot scaffold .env.local)\n' >&2
    exit 1
  fi
  cp .env.local.example .env.local
  printf '  created .env.local from .env.local.example\n'
fi

if ! grep -qE '^WEBUI_SECRET_KEY=.+$' .env.local; then
  KEY="$(gen_hex)"
  sed -i "s|^WEBUI_SECRET_KEY=.*|WEBUI_SECRET_KEY=${KEY}|" .env.local
  printf '  generated WEBUI_SECRET_KEY into .env.local\n'
else
  printf '  WEBUI_SECRET_KEY already present in .env.local (kept)\n'
fi

if ! grep -qE '^GRAPHITI_API_TOKEN=.+$' .env.local; then
  TOK="$(gen_hex)"
  sed -i "s|^GRAPHITI_API_TOKEN=.*|GRAPHITI_API_TOKEN=${TOK}|" .env.local
  printf '  generated GRAPHITI_API_TOKEN into .env.local\n'
else
  printf '  GRAPHITI_API_TOKEN already present in .env.local (kept)\n'
fi

chmod 600 .env.local
printf '  set .env.local permissions to 0600\n'

mkdir -p data/neo4j/data data/neo4j/logs data/openwebui
printf '  ensured ./data/{neo4j/data,neo4j/logs,openwebui} exist\n'

TOK="$(grep -E '^GRAPHITI_API_TOKEN=' .env.local | cut -d= -f2-)"
printf '\n  GRAPHITI_API_TOKEN (copy into your MCP clients):\n    %s\n' "$TOK"
printf '\nBootstrap done. Next: make preflight && make start\n'