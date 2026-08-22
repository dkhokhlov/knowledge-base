#!/usr/bin/env bash
# Provision the markitdown-ocr external extraction engine for Open WebUI:
#   1. ensure OCR_SERVICE_TOKEN is in .env.local (generate one if missing);
#   1b. ensure the OCR vision model (OCR_MODEL, default deepseek-ocr) is pulled
#       on the Ollama host (idempotent; a failed pull aborts so the marker is
#       NOT written — no half-provisioned engine that silently empties image
#       docs);
#   2. build the markitdown-ocr image;
#   3. (re)create the markitdown-ocr compose service (--profile ocr);
#   4. wait for its /health (via the compose healthcheck) to go green;
#   5. run `make ocr-config` (set CONTENT_EXTRACTION_ENGINE=external + URL +
#      API key in the OWUI DB);
#   6. write MARKITDOWN_OCR_PROVISIONED=1 to .env.local ONLY on success, so
#      `make start`/`make restart` bring the service back with --profile ocr.
#
# Preconditions:
#   - Stack running and healthy (`make start`).
#   - OPENWEBUI_ADMIN_API_KEY in .env.local (provisioned by `make api-keys`).
#
# Idempotent: re-running rebuilds + re-asserts the config + keeps the marker.
#
# No fallback: if any step fails, the marker is NOT written and OWUI keeps its
# default loaders (the external engine is global + all-or-nothing; a half-
# provisioned engine would orphan every ingest).
set -euo pipefail
cd "$(dirname "$0")/.."

test -f .env.local || { echo "MISSING .env.local — run: make bootstrap" >&2; exit 1; }

set -a
# shellcheck source=/dev/null
. ./.env
# shellcheck source=/dev/null
. ./.env.local
set +a

: "${OPENWEBUI_ADMIN_API_KEY:?FAIL  OPENWEBUI_ADMIN_API_KEY not set in .env.local (run: make api-keys)}"

# Step 1: ensure OCR_SERVICE_TOKEN (generate + persist if missing).
update_env_local() {
  local key="$1" val="$2"
  if grep -q "^${key}=" .env.local; then
    python3 - "$key" "$val" <<'PY'
import sys, os
key, val = sys.argv[1], sys.argv[2]
f = ".env.local"
out = []; seen = False
for ln in open(f).read().splitlines():
    if ln.startswith(key + "="):
        out.append(key + "=" + val); seen = True
    else:
        out.append(ln)
if not seen:
    out.append(key + "=" + val)
open(f, "w").write("\n".join(out) + "\n")
os.chmod(f, 0o600)
PY
  else
    printf '%s=%s\n' "$key" "$val" >> .env.local
  fi
  chmod 600 .env.local
}

if [ -z "${OCR_SERVICE_TOKEN:-}" ]; then
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  update_env_local OCR_SERVICE_TOKEN "$TOKEN"
  # Re-export so the ocr-config step (same process) sees it.
  export OCR_SERVICE_TOKEN="$TOKEN"
  printf 'OK    generated OCR_SERVICE_TOKEN and wrote it to .env.local\n'
else
  printf 'OK    OCR_SERVICE_TOKEN already set (kept)\n'
fi

# Step 1b: ensure the OCR vision model is present on the Ollama host. The
# service fail-opens on a missing/unreachable model (returns an empty OCR
# result -> an image-only file orphans with no text). `ollama pull` is
# idempotent (a manifest re-fetch when already present); a failed pull aborts
# provisioning so the marker is NOT written (no half-provisioned engine that
# silently empties image docs). OLLAMA_HOST is sourced from .env.
: "${OCR_MODEL:?FAIL  OCR_MODEL not set in .env (expected deepseek-ocr)}"
: "${OLLAMA_HOST:?FAIL  OLLAMA_HOST not set in .env (Ollama base URL for the CLI)}"
printf '==> ensuring OCR vision model %s is present on Ollama (%s)\n' "$OCR_MODEL" "$OLLAMA_HOST"
ollama pull "$OCR_MODEL"
printf 'OK    OCR model %s present\n' "$OCR_MODEL"

# Step 2: build the image.
printf '==> building markitdown-ocr image\n'
docker compose --profile ocr build markitdown-ocr

# Step 3: (re)create the service. --no-deps: openwebui is already running healthy;
# do NOT recreate it (or any other service) just to satisfy markitdown-ocr's
# depends_on. Re-source .env.local so compose interpolation sees the token.
set -a; . ./.env; . ./.env.local; set +a
printf '==> (re)creating markitdown-ocr service\n'
docker compose --profile ocr up -d --no-deps --force-recreate markitdown-ocr

# Step 4: wait for the compose healthcheck to go green (no published port; the
# healthcheck probes /health inside the container).
printf '==> waiting for markitdown-ocr /health\n'
i=0
until [ "$(docker inspect -f '{{.State.Health.Status}}' markitdown-ocr 2>/dev/null || echo none)" = "healthy" ]; do
  i=$((i+1))
  [ "$i" -lt 60 ] || { echo "FAIL  markitdown-ocr did not become healthy in 120s" >&2; exit 1; }
  sleep 2
done
printf 'OK    markitdown-ocr healthy\n'

# Step 5: point OWUI at the engine (set the retrieval-config keys).
printf '==> configuring Open WebUI external extraction engine\n'
./scripts/ocr-config.sh enable

# Step 6: write the marker so `make start`/`make restart` add --profile ocr.
update_env_local MARKITDOWN_OCR_PROVISIONED 1
printf '\nDone. markitdown-ocr is OWUI'\''s external extraction engine.\n'
printf 'Logs: docker logs -f markitdown-ocr   |   Disable: make ocr-disable\n'