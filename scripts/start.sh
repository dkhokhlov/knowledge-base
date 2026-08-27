#!/usr/bin/env bash
# Start the stack detached, adding --profile ocr when the markitdown-ocr
# sidecar is enabled:
#   - --profile ocr     when OCR_ENABLED=true (default; set false in .env to
#                       disable; overridable via `make start OCR_ENABLED=<val>`).
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

# Capture a `make start OCR_ENABLED=<val>` override before sourcing .env (which
# would clobber it). Restored so preflight (called below) inherits it too.
_OCR_ENABLED_OVR="${OCR_ENABLED:-}"
set -a; . ./.env; . ./.env.local; set +a
if [ -n "$_OCR_ENABLED_OVR" ]; then export OCR_ENABLED="$_OCR_ENABLED_OVR"; fi
# Preflight: docker, secrets, ./data tree, Ollama running + required models.
# Fails fast before `docker compose up` if Ollama is down or a required model
# is missing (a stack that can't reach Ollama is worse than refusing to start).
./scripts/preflight.sh

PROFILES=""
if [ "${OCR_ENABLED:-true}" = "true" ]; then
  echo "OCR_ENABLED=true — adding --profile ocr (markitdown-ocr built + started)"
  PROFILES="$PROFILES --profile ocr"
else
  echo "OCR_ENABLED=false — not starting markitdown-ocr"
fi
# shellcheck disable=SC2086  # PROFILES is intentionally word-split into flags.
docker compose $PROFILES up -d