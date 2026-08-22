#!/usr/bin/env bash
# Pull the base chat LLM (OLLAMA_MODEL_BASE), create the ctx-baked variant
# (MODEL_NAME with num_ctx=OLLAMA_MODEL_CONTEXT), and pull the embedder
# (nomic-embed-text). Refuses if OLLAMA_MODEL_CONTEXT is not a positive
# integer.
#
# NOTE: this does NOT pull the OCR vision model (deepseek-ocr) — that is
# handled by `make ocr-bootstrap` (which runs `ollama pull $OCR_MODEL` as a
# provisioning step). Run `make ocr-bootstrap` separately to provision OCR.
#
# Usage: make pull-models   (or: scripts/pull-models.sh)
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
: "${OLLAMA_MODEL_BASE:?OLLAMA_MODEL_BASE not set in .env}"
: "${MODEL_NAME:?MODEL_NAME not set in .env}"
case "${OLLAMA_MODEL_CONTEXT:-}" in ''|*[!0-9]*)
  echo "REFUSING: OLLAMA_MODEL_CONTEXT must be a positive integer (got '${OLLAMA_MODEL_CONTEXT:-<unset>}')" >&2
  exit 1;;
esac
[ "$OLLAMA_MODEL_CONTEXT" -gt 0 ] || { echo "REFUSING: OLLAMA_MODEL_CONTEXT must be > 0" >&2; exit 1; }

echo "Pulling base LLM: $OLLAMA_MODEL_BASE"
ollama pull "$OLLAMA_MODEL_BASE"
mf=$(mktemp)
printf 'FROM %s\nPARAMETER num_ctx %s\n' "$OLLAMA_MODEL_BASE" "$OLLAMA_MODEL_CONTEXT" > "$mf"
echo "Creating ctx variant: $MODEL_NAME (num_ctx=$OLLAMA_MODEL_CONTEXT)"
ollama rm "$MODEL_NAME" >/dev/null 2>&1 || true
ollama create "$MODEL_NAME" -f "$mf"
rm -f "$mf"
echo "Pulling embedder: nomic-embed-text"
ollama pull nomic-embed-text
echo "Done. If the stack is running, restart it so Ollama reloads the new manifest: make restart"