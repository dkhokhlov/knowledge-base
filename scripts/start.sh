#!/usr/bin/env bash
# Start the stack detached, adding --profile ocr only when the markitdown-ocr
# sidecar is provisioned:
#   - --profile ocr     when MARKITDOWN_OCR_PROVISIONED=1 (run: make ocr-bootstrap).
#
# gdrive indexing is no longer a sidecar: kb-gateway serves POST /index (manual,
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

set -a; . ./.env; . ./.env.local; set +a
# Preflight: docker, secrets, ./data tree, Ollama running + required models.
# Fails fast before `docker compose up` if Ollama is down or a required model
# is missing (a stack that can't reach Ollama is worse than refusing to start).
./scripts/preflight.sh

PROFILES=""
if [ "${MARKITDOWN_OCR_PROVISIONED:-0}" = "1" ]; then
  echo "markitdown-ocr provisioned (MARKITDOWN_OCR_PROVISIONED=1) — adding --profile ocr"
  PROFILES="$PROFILES --profile ocr"
else
  echo "markitdown-ocr not provisioned (MARKITDOWN_OCR_PROVISIONED!=1) — not starting it (run: make ocr-bootstrap to add it)"
fi
# shellcheck disable=SC2086  # PROFILES is intentionally word-split into flags.
docker compose $PROFILES up -d