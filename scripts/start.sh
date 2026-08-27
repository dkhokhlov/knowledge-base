#!/usr/bin/env bash
# Start the stack detached. The markitdown-ocr sidecar is included
# automatically: COMPOSE_PROFILES=ocr in .env (baked once by `make bootstrap`
# from OCR_ENABLED) is read by `docker compose` for every command, so `up -d`
# builds + starts it when enabled. No --profile flag, no per-command override.
# To disable OCR: `make clean-all && make provision OCR_ENABLED=false` (bakes
# OCR_ENABLED=false + an empty COMPOSE_PROFILES into .env). The OCR_ENABLED
# override is NOT honored here -- the sidecar is governed by .env, not start;
# use provision to change it.
#
# gdrive indexing is no longer a sidecar: api-gateway serves POST /index (manual,
# driven by `make gdrive-sync`). No gdrive profile.
#
# Refuses to start without .env.local + a WEBUI_SECRET_KEY, and fails fast if
# OLLAMA_HOST is unset (compose `${OLLAMA_HOST:?...}` + `./scripts/preflight.sh`).
#
# Usage: make start   (or: scripts/start.sh)
set -euo pipefail
cd "$(dirname "$0")/.."

test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
# Unset first so WEBUI_SECRET_KEY is not inherited from the calling environment;
# it must come from .env.local (written by make bootstrap).
unset WEBUI_SECRET_KEY
. ./.env.local 2>/dev/null || { echo "MISSING secret — run: make bootstrap"; exit 1; }
[ -n "${WEBUI_SECRET_KEY:-}" ] || { echo "MISSING secret — run: make bootstrap"; exit 1; }

# Source .env + .env.local so OLLAMA_HOST and OCR_ENABLED reach preflight. The
# .env sourcing also sets COMPOSE_PROFILES, which `docker compose` reads (it is
# NOT a runtime toggle: change it via `make provision OCR_ENABLED=<val>`).
set -a; . ./.env; . ./.env.local; set +a
# Preflight: docker, secrets, ./data tree, Ollama running + required models.
# Fails fast before `docker compose up` if Ollama is down or a required model is
# missing (a stack that can't reach Ollama is worse than refusing to start).
# Reads OCR_ENABLED from .env (the provisioned decision) to check the OCR model.
./scripts/preflight.sh

docker compose up -d