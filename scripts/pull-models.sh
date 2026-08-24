#!/usr/bin/env bash
# Pull the base chat LLM (OLLAMA_MODEL_BASE), create the ctx-baked variant
# (MODEL_NAME with num_ctx=OLLAMA_MODEL_CONTEXT), and pull the embedder
# (nomic-embed-text). Refuses if OLLAMA_MODEL_CONTEXT is not a positive
# integer.
#
# When OCR_ENABLED=true (default; overridable via `make pull-models
# OCR_ENABLED=false`), also pulls the OCR vision model (OCR_MODEL, default
# deepseek-ocr) — a first-class prereq alongside the base LLM + embedder.
#
# Usage: make pull-models   (or: scripts/pull-models.sh)
set -euo pipefail
cd "$(dirname "$0")/.."

# Capture a `make pull-models OCR_ENABLED=<val>` override before sourcing .env
# (which would clobber it).
_OCR_ENABLED_OVR="${OCR_ENABLED:-}"
set -a; . ./.env; set +a
if [ -n "$_OCR_ENABLED_OVR" ]; then export OCR_ENABLED="$_OCR_ENABLED_OVR"; fi
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
if [ "${OCR_ENABLED:-true}" = "true" ]; then
  OCR_MODEL="${OCR_MODEL:-deepseek-ocr}"
  echo "Pulling OCR vision model: $OCR_MODEL"
  ollama pull "$OCR_MODEL"
fi
echo "Done. If the stack is running, restart it so Ollama reloads the new manifest: make restart"