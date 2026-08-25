#!/usr/bin/env bash
# DESTRUCTIVE clean-state deploy + full integration test suite:
#   wipe -> bootstrap -> restore admin creds -> [pull OCR model] -> preflight
#   -> [build markitdown-ocr] -> start -> wait healthy -> admin-signup ->
#   api-keys (auto: points OWUI at markitdown-ocr) -> rag-config ->
#   gdrive-index-bootstrap -> gdrive-sync (rclone + POST /index) -> test.
#
# OCR is provisioned BEFORE the gdrive set ingests so image-bearing documents
# are OCR'd (non-empty), not orphaned. gdrive-sync runs rclone then POSTs
# /index (synchronous: sync/diff + upload + re-trigger failed; the OWUI
# per-upload background task links after extract+embed). Extraction + embedding
# drain async, so test_09 polls GET /status for the real drain terminal state
# (pending+processing=0 AND completed+failed>=source). The cold first extraction
# runs per-figure OCR through deepseek-ocr, so the pending-drain budget is
# raised (E2E_INDEXER_WAIT, default 2400s -> GDRIVE_TEST_WAIT).
#
# Stashes OPENWEBUI_FIRST_USER/PASSWORD (+OPENWEBUI_USER) before the wipe and
# restores them after bootstrap (clear-all deletes .env.local).
#
# No fallback / no workaround: any step failing aborts the run (set -e). The
# gdrive set is ingested through the external engine; an empty extraction
# result orphans the file (by design — the operator sees the outage).
#
# Usage: make test-e2e   (or: scripts/test-e2e.sh)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> DESTRUCTIVE: wipes all data and re-provisions from scratch."
test -f .env.local || { echo "REFUSING: no .env.local (no admin creds to stash) — run make bootstrap + fill OPENWEBUI_FIRST_USER/PASSWORD first" >&2; exit 1; }
_OCR_OVR="${OCR_ENABLED:-}"
set -a; . ./.env; . ./.env.local; set +a
if [ -n "$_OCR_OVR" ]; then export OCR_ENABLED="$_OCR_OVR"; fi
[ -n "${OPENWEBUI_FIRST_USER:-}" ] && [ -n "${OPENWEBUI_FIRST_PASSWORD:-}" ] \
  || { echo "REFUSING: OPENWEBUI_FIRST_USER/PASSWORD not set in .env.local (admin account) — fill them first" >&2; exit 1; }

stash=$(mktemp); chmod 600 "$stash"
{ printf 'OPENWEBUI_FIRST_USER=%s\nOPENWEBUI_FIRST_PASSWORD=%s\n' "$OPENWEBUI_FIRST_USER" "$OPENWEBUI_FIRST_PASSWORD"
  [ -n "${OPENWEBUI_USER:-}" ] && printf 'OPENWEBUI_USER=%s\n' "$OPENWEBUI_USER" || true; } > "$stash"
trap 'rm -f "$stash"' EXIT

make clean-all
unset GDRIVE_KB_ID
make bootstrap
./scripts/e2e-restore-creds.sh "$stash"
# Pull the OCR vision model before preflight (preflight hard-fails on a missing
# OCR model when OCR_ENABLED=true). Pull only the OCR model, NOT full
# `make pull-models` (that `ollama rm`s + recreates GRAPHITI_MODEL, disrupting the
# assumed-present base LLM). Honors a `make test-e2e OCR_ENABLED=false` override.
if [ "${OCR_ENABLED:-true}" = "true" ]; then
  echo "==> pulling OCR vision model: ${OCR_MODEL:-deepseek-ocr}"
  ollama pull "${OCR_MODEL:-deepseek-ocr}"
fi
make preflight
# Rebuild locally-built images whose code changed since the last run, so e2e
# tests current code (clean-all wipes volumes/data, NOT images; `up -d` without
# --build reuses the existing image). kb-gateway is stdlib-only so this is fast.
# markitdown-ocr is rebuilt here (gated on OCR_ENABLED) so e2e runs current OCR
# code; openwebui (patched) is rebuilt only when its patches change (manual:
# `docker compose build openwebui`).
docker compose build kb-gateway
if [ "${OCR_ENABLED:-true}" = "true" ]; then
  docker compose --profile ocr build markitdown-ocr
fi
make start

H="${KB_HOST:-http://localhost:${KB_HOST_PORT:-3000}}"
i=0
until curl -sf "$H/health" >/dev/null; do
  i=$((i+1))
  [ "$i" -lt 60 ] || { echo "stack did not become healthy in 120s ($H/health)" >&2; exit 1; }
  sleep 2
done
echo "stack healthy ($H/health)"

make admin-signup
make api-keys
make projects-bootstrap
make rag-config
make gdrive-index-bootstrap
make gdrive-sync
GDRIVE_TEST_WAIT="${E2E_INDEXER_WAIT:-2400}" make test
# test_09 (full real-gdrive drain) is not in the `make test` glob (it is slow
# and coupled to the live rclone-synced corpus); run it explicitly here, where
# the corpus is freshly synced and the gdrive KB is provisioned.
echo "==> full real-gdrive drain (test_09)"
GDRIVE_TEST_WAIT="${E2E_INDEXER_WAIT:-2400}" bash tests/test_09_gdrive_index.sh
echo "==> test-e2e PASS"