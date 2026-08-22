#!/usr/bin/env bash
# DESTRUCTIVE clean-state deploy + full integration test suite:
#   wipe -> bootstrap -> restore admin creds -> preflight -> start -> wait
#   healthy -> admin-signup -> api-keys -> rag-config -> ocr-bootstrap
#   (engine ON before ingest) -> gdrive-index-bootstrap -> wait indexer ->
#   test.
#
# OCR is provisioned BEFORE the gdrive set ingests so image-bearing documents
# are OCR'd (non-empty), not orphaned. The indexer wait is given a longer
# budget (E2E_INDEXER_WAIT, default 2400s) because the cold first sync now
# runs per-figure OCR through deepseek-ocr.
#
# Stashes OPENWEBUI_TEST_USER/PASSWORD (+OPENWEBUI_USER) before the wipe and
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
test -f .env.local || { echo "REFUSING: no .env.local (no admin creds to stash) — run make bootstrap + fill OPENWEBUI_TEST_USER/PASSWORD first" >&2; exit 1; }
set -a; . ./.env; . ./.env.local; set +a
[ -n "${OPENWEBUI_TEST_USER:-}" ] && [ -n "${OPENWEBUI_TEST_PASSWORD:-}" ] \
  || { echo "REFUSING: OPENWEBUI_TEST_USER/PASSWORD not set in .env.local (admin account) — fill them first" >&2; exit 1; }

stash=$(mktemp); chmod 600 "$stash"
{ printf 'OPENWEBUI_TEST_USER=%s\nOPENWEBUI_TEST_PASSWORD=%s\n' "$OPENWEBUI_TEST_USER" "$OPENWEBUI_TEST_PASSWORD"
  [ -n "${OPENWEBUI_USER:-}" ] && printf 'OPENWEBUI_USER=%s\n' "$OPENWEBUI_USER" || true; } > "$stash"
trap 'rm -f "$stash"' EXIT

make clear-all
unset GDRIVE_KB_ID OIKB_API_KEY
make bootstrap
./scripts/e2e-restore-creds.sh "$stash"
make preflight
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
make rag-config
make ocr-bootstrap
make gdrive-index-bootstrap
E2E_INDEXER_WAIT="${E2E_INDEXER_WAIT:-2400}" ./scripts/e2e-wait-indexer.sh
make test
echo "==> test-e2e PASS"