#!/usr/bin/env bash
# Resolve compose profile flags so make stop/logs/config target the same service
# set as make start (which adds --profile ocr when OCR_ENABLED=true). Mirrors the
# override-capture idiom in scripts/start.sh: a `make <target> OCR_ENABLED=<val>`
# override is captured before sourcing .env (which would clobber it) and restored
# after. Prints `--profile ocr` when enabled; prints nothing when disabled.
# OCR_ENABLED=false stays a supported escape hatch (skip the deepseek-ocr pull +
# inference load), so the profile is conditional — never unconditional — to avoid
# asking compose to stop/show a sidecar that was never started on a disabled stack.
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1
_OVR="${OCR_ENABLED:-}"
set -a; . ./.env 2>/dev/null || true; set +a
[ -n "$_OVR" ] && export OCR_ENABLED="$_OVR"
if [ "${OCR_ENABLED:-true}" = "true" ]; then
  printf '%s\n' "--profile ocr"
fi