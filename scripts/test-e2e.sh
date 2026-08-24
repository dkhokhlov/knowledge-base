#!/usr/bin/env bash
# DESTRUCTIVE clean-state deploy + full integration test suite:
#   wipe -> bootstrap -> restore admin creds -> preflight -> start -> wait
#   healthy -> admin-signup -> api-keys -> rag-config -> ocr-bootstrap
#   (engine ON before ingest) -> gdrive-index-bootstrap -> gdrive-sync
#   (rclone + POST /index) -> test.
#
# OCR is provisioned BEFORE the gdrive set ingests so image-bearing documents
# are OCR'd (non-empty), not orphaned. gdrive-sync runs rclone then POSTs
# /index (synchronous: sync/diff + upload + link); extraction + embedding run
# in OWUI's per-upload background task and drain async, so test_09 polls GET
# /status pending. The cold first extraction runs per-figure OCR through
# deepseek-ocr, so the pending-drain
# budget is raised (E2E_INDEXER_WAIT, default 2400s -> GDRIVE_TEST_WAIT).
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

make clean-all
unset GDRIVE_KB_ID
make bootstrap
./scripts/e2e-restore-creds.sh "$stash"
make preflight
# Rebuild locally-built images whose code changed since the last run, so the
# e2e tests current code (clean-all wipes volumes/data, NOT images; `up -d`
# without --build reuses the existing image). kb-gateway is stdlib-only so this
# is fast. openwebui (patched) + markitdown-ocr are heavy; markitdown-ocr is
# rebuilt by `make ocr-bootstrap` below, openwebui is rebuilt only when its
# patches change (manual: `docker compose build openwebui`).
docker compose build kb-gateway
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
make gdrive-sync
GDRIVE_TEST_WAIT="${E2E_INDEXER_WAIT:-2400}" make test
echo "==> test-e2e PASS"