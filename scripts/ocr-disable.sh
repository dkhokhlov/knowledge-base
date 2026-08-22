#!/usr/bin/env bash
# Disable the markitdown-ocr external extraction engine (no KB reset):
#   1. scripts/ocr-config.sh disable — clears CONTENT_EXTRACTION_ENGINE +
#      EXTERNAL_DOCUMENT_LOADER_URL/API_KEY/HEADERS in the OWUI DB AND removes
#      the MARKITDOWN_OCR_PROVISIONED marker from .env.local;
#   2. recreate openwebui so it reloads the cleared extraction config.
#
# New uploads then use OWUI's default loaders. Existing OCR'd members keep
# their content until re-ingested; re-ingested image-only files return to
# orphans (the pre-OCR state). No full KB reset.
#
# Usage: make ocr-disable   (or: scripts/ocr-disable.sh)
set -euo pipefail
cd "$(dirname "$0")/.."

test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$' .env.local \
  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }

# Step 1: clear the OWUI DB keys + drop the provisioned marker.
./scripts/ocr-config.sh disable

# Step 2: recreate openwebui so it reloads the cleared extraction config.
# --profile ocr is passed explicitly (a one-off) so compose loads the ocr
# services; --no-deps + the openwebui arg recreate only openwebui (the
# markitdown-ocr container is left running until the next make start/clear,
# which — without the marker — will not bring it back).
set -a; . ./.env; . ./.env.local; set +a
echo "recreating openwebui so it reloads the cleared extraction config"
docker compose --profile ocr up -d --no-deps --force-recreate openwebui
echo "Done. New uploads use OWUI's default loaders. Existing OCR'd members are unchanged until re-ingested."